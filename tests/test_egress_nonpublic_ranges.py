"""Internet egress must not admit non-public space (enterpriseaiframework-bbf).

63-agent-common and 60-workspace-common grant `0.0.0.0/0 except <private>`. Excluding only
RFC1918 + link-local left 100.64.0.0/10 (Tailscale CGNAT: tailnet devices, MagicDNS
100.100.100.100) reachable from hosted agents and workspaces. This asserts, by what the
shipped policies ADMIT (tests/netpol_eval.py egress semantics, not by spelling), that for every
pod shape that gets internet egress:
  * no address in any hand-listed non-public range is reachable on any probed port, and
  * public addresses, including the ones adjacent to each excluded range, still are on 443/80.
The expected sets below are hand-written from the RFCs, independent of the policy files.
Every probe is refused by the ipBlock under test: the subject pods' other egress rules are
podSelector-only, so no other rule in the same policy can be the reason.
"""
import ipaddress
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netpol_eval import admitted_egress_peers, dest_from_template, load_policies  # noqa: E402

K8S = Path(__file__).resolve().parent.parent / "deploy" / "k8s"
NONPUBLIC = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
             "100.64.0.0/10", "198.18.0.0/15", "192.0.0.0/24", "224.0.0.0/4", "240.0.0.0/4"]
# Just outside an excluded range: over-blocking these would break real internet hosts.
PUBLIC = ["8.8.8.8", "1.1.1.1", "100.63.255.255", "100.128.0.1", "198.17.255.255",
          "198.20.0.1", "192.0.1.1", "223.255.255.255", "172.32.0.1", "11.0.0.1"]
PORTS = [22, 53, 80, 443, 8080, 65535]
SUBJECTS = {"agent": "64-agent.template.yaml", "hermes": "65-agent-hermes.template.yaml",
            "openclaw": "67-agent-openclaw.template.yaml",
            "workspace": "61-workspace.template.yaml"}
GATEWAY_LAN_IP = "192.168.2.42"
POLICIES = ["60-workspace-common.yaml", "63-agent-common.yaml"]


def probes():
    out = set()
    for c in NONPUBLIC:
        n = ipaddress.ip_network(c)
        out |= {n[0], n[1], n[n.num_addresses // 2], n[-2], n[-1]}
    return sorted(str(i) for i in out)


def _probe_policy():
    """Inert policy (selects no real pod) whose ipBlocks put every probe into the evaluator's
    address universe, which otherwise only samples the shipped blocks' endpoints."""
    return {"kind": "NetworkPolicy", "metadata": {"name": "probe", "namespace": "enterprise-ai"},
            "spec": {"podSelector": {"matchLabels": {"probe": "nobody"}}, "policyTypes": ["Egress"],
                     "egress": [{"to": [{"ipBlock": {"cidr": f"{ip}/32"}} for ip in probes() + PUBLIC]}]}}


def reachable(policies, subject, port):
    dest = dest_from_template(K8S / SUBJECTS[subject])
    _, ips = admitted_egress_peers([*policies, _probe_policy()], dest, port)
    return set(ips)


def shipped():
    return load_policies([K8S / p for p in POLICIES], {"__GATEWAY_LAN_IP__": GATEWAY_LAN_IP})


def violations(policies):
    bad = []
    nets = [ipaddress.ip_network(c) for c in NONPUBLIC]
    for s in SUBJECTS:
        for port in PORTS:
            for ip in reachable(policies, s, port):
                if (s, port, ip) == ("workspace", 443, GATEWAY_LAN_IP):
                    continue  # the deliberate OIDC-backchannel /32 rule in 60-workspace-common
                if any(ipaddress.ip_address(ip) in n for n in nets):
                    bad.append((s, port, ip))
    return bad


@pytest.mark.parametrize("subject", SUBJECTS)
def test_no_nonpublic_address_is_reachable(subject):
    assert [v for v in violations(shipped()) if v[0] == subject] == []


@pytest.mark.parametrize("subject", SUBJECTS)
def test_public_internet_still_reachable(subject):
    for port in (80, 443):
        got = reachable(shipped(), subject, port)
        missing = [ip for ip in PUBLIC if ip not in got]
        assert missing == [], (subject, port, missing)


def test_tailnet_magicdns_specifically_refused_for_every_subject():
    for s in SUBJECTS:
        for port in PORTS:
            assert "100.100.100.100" not in reachable(shipped(), s, port)


# ---- drifts, injected through the policy YAML the way an edit would reach the cluster ----
def _mut(fn):
    docs = shipped()
    fn(docs)
    return docs


def _blocks(docs):
    for d in docs:
        for r in d["spec"].get("egress") or []:
            for p in r.get("to") or []:
                if p.get("ipBlock", {}).get("cidr") == "0.0.0.0/0":
                    yield p["ipBlock"]


def _drop(cidr):
    def f(docs):
        for b in _blocks(docs):
            b["except"] = [c for c in b["except"] if c != cidr]
    return f


def _replace(old, new):
    def f(docs):
        for b in _blocks(docs):
            b["except"] = [new if c == old else c for c in b["except"]]
    return f


DRIFTS = {
    "cgnat removed (the original defect)": _drop("100.64.0.0/10"),
    "cgnat narrowed to /16 (tailnet 100.100 outside it)": _replace("100.64.0.0/10", "100.64.0.0/16"),
    "cgnat shifted (100.0.0.0/10)": _replace("100.64.0.0/10", "100.0.0.0/10"),
    "benchmarking removed": _drop("198.18.0.0/15"),
    "benchmarking narrowed to /16": _replace("198.18.0.0/15", "198.18.0.0/16"),
    "ietf 192.0.0.0/24 removed": _drop("192.0.0.0/24"),
    "multicast removed": _drop("224.0.0.0/4"),
    "reserved 240/4 removed": _drop("240.0.0.0/4"),
    "except list emptied": lambda docs: [b.__setitem__("except", []) for b in _blocks(docs)],
    "except key deleted": lambda docs: [b.pop("except") for b in _blocks(docs)],
}


@pytest.mark.parametrize("name", DRIFTS)
def test_every_drift_is_caught(name):
    assert violations(_mut(DRIFTS[name])), name


def test_over_blocking_is_caught():
    """The public check bites too: excluding all of 100.0.0.0/8 hides a public neighbour."""
    docs = _mut(_replace("100.64.0.0/10", "100.0.0.0/8"))
    assert "100.63.255.255" not in reachable(docs, "hermes", 443)


def test_a_fresh_ipblock_rule_admitting_cgnat_is_caught():
    def add(docs):
        for d in docs:
            if d["metadata"]["name"] == "agent-isolation":
                d["spec"]["egress"].append({"to": [{"ipBlock": {"cidr": "100.64.0.0/10"}}]})
    assert violations(_mut(add))
