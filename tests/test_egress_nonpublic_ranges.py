"""Internet egress must not admit non-public space (enterpriseaiframework-bbf).

63-agent-common and 60-workspace-common grant `0.0.0.0/0 except <private>`. Excluding only
RFC1918 + link-local left 100.64.0.0/10 (Tailscale CGNAT: tailnet devices, MagicDNS
100.100.100.100) reachable from hosted agents and workspaces. This asserts, by what the
shipped policies ADMIT (tests/netpol_eval.py egress semantics, not by spelling), that for every
pod shape that gets internet egress:
  * no address in any hand-listed non-public range is reachable on any probed port, and
  * public addresses, including the ones adjacent to each excluded range, still are on 443/80,
  * on TCP, UDP AND SCTP alike (the shipped rules omit `ports`, which means every protocol), and
  * NO native IPv6 address is reachable at all (-1ed): a `0.0.0.0/0` ipBlock covers no IPv6
    address, and no shipped policy names an IPv6 block, so the correct posture for agents and
    workspaces is IPv4-only internet. An IPv4-mapped IPv6 address (::ffff:a.b.c.d) is judged by
    its embedded IPv4, as the CNI's netip/net.IP folding does.
The expected sets below are hand-written from the RFCs, independent of the policy files.
Every probe is refused by the ipBlock under test: the subject pods' other egress rules are
podSelector-only, so no other rule in the same policy can be the reason.
"""
import ipaddress
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netpol_eval import (PROTOCOLS, admitted_egress_peers, dest_from_template,  # noqa: E402
                         load_policies)

K8S = Path(__file__).resolve().parent.parent / "deploy" / "k8s"
NONPUBLIC = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
             "100.64.0.0/10", "198.18.0.0/15", "192.0.0.0/24", "224.0.0.0/4", "240.0.0.0/4"]
# Just outside an excluded range: over-blocking these would break real internet hosts.
PUBLIC = ["8.8.8.8", "1.1.1.1", "100.63.255.255", "100.128.0.1", "198.17.255.255",
          "198.20.0.1", "192.0.1.1", "223.255.255.255", "172.32.0.1", "11.0.0.1"]
PORTS = [22, 53, 80, 443, 8080, 65535]
# Native IPv6 space, by hand from the RFCs: global unicast, ULA, link-local, loopback,
# unspecified, multicast, documentation, NAT64, 6to4, Teredo. None may be reachable.
V6_RANGES = ["2000::/3", "fc00::/7", "fe80::/10", "::1/128", "::/128", "ff00::/8",
             "2001:db8::/32", "64:ff9b::/96", "2002::/16", "2001::/32"]
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


def v6_probes():
    out = set()
    for c in V6_RANGES:
        n = ipaddress.ip_network(c)
        out |= {n[0], n[min(1, n.num_addresses - 1)], n[n.num_addresses // 2], n[-1]}
    # v4-mapped forms of every non-public probe and of the public ones
    for ip in [*probes(), *PUBLIC]:
        out.add(ipaddress.ip_address("::ffff:" + ip))
    return sorted(str(i) for i in out)


def _probe_policy():
    """Inert policy (selects no real pod) whose ipBlocks put every probe into the evaluator's
    address universe, which otherwise only samples the shipped blocks' endpoints."""
    return {"kind": "NetworkPolicy", "metadata": {"name": "probe", "namespace": "enterprise-ai"},
            "spec": {"podSelector": {"matchLabels": {"probe": "nobody"}}, "policyTypes": ["Egress"],
                     "egress": [{"to": [{"ipBlock": {"cidr": f"{ip}/{128 if ':' in ip else 32}"}}
                               for ip in probes() + PUBLIC + v6_probes()]}]}}


def reachable(policies, subject, port, proto="TCP"):
    dest = dest_from_template(K8S / SUBJECTS[subject])
    _, ips = admitted_egress_peers([*policies, _probe_policy()], dest, port, proto)
    return set(ips)


def _view(ip):
    """The IPv4 an address folds to (itself, or the embedded v4 of a v4-mapped v6 address)."""
    a = ipaddress.ip_address(ip)
    return a.ipv4_mapped if a.version == 6 and a.ipv4_mapped else a


def shipped():
    return load_policies([K8S / p for p in POLICIES], {"__GATEWAY_LAN_IP__": GATEWAY_LAN_IP})


def violations(policies):
    bad = []
    nets = [ipaddress.ip_network(c) for c in NONPUBLIC]
    for s in SUBJECTS:
        for proto in PROTOCOLS:
            for port in PORTS:
                for ip in reachable(policies, s, port, proto):
                    if (s, proto, port, ip) == ("workspace", "TCP", 443, GATEWAY_LAN_IP):
                        continue  # the deliberate OIDC-backchannel /32 rule in 60-workspace-common
                    a = ipaddress.ip_address(ip)
                    if a.version == 6 and a.ipv4_mapped is None:
                        bad.append((s, proto, port, ip))  # native IPv6: no policy grants it
                    elif any(_view(ip) in n for n in nets):
                        bad.append((s, proto, port, ip))
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
        for proto in PROTOCOLS:
            for port in PORTS:
                got = reachable(shipped(), s, port, proto)
                assert "100.100.100.100" not in got
                assert "::ffff:100.100.100.100" not in got


@pytest.mark.parametrize("subject", SUBJECTS)
def test_no_shipped_policy_grants_ipv6_to_anyone(subject):
    """The IPv6 posture, stated: agents and workspaces get IPv4 internet only. Asserted from
    every probe (global unicast, ULA, link-local, loopback, unspecified, multicast, documentation,
    NAT64, 6to4, Teredo) on every protocol and port. Only IPv4-mapped forms of PUBLIC v4
    addresses may appear (they fold to the granted IPv4 internet)."""
    for proto in PROTOCOLS:
        for port in PORTS:
            native = [ip for ip in reachable(shipped(), subject, port, proto)
                      if ipaddress.ip_address(ip).version == 6
                      and ipaddress.ip_address(ip).ipv4_mapped is None]
            assert native == [], (subject, proto, port, native)


def test_the_ipv6_probe_universe_is_not_vacuous():
    """Every native-v6 probe is really in the evaluator's universe (else 'unreachable' is trivial)."""
    from netpol_eval import build_universe
    _, ips = build_universe([*shipped(), _probe_policy()], "enterprise-ai", "egress")
    missing = [ip for ip in v6_probes() if ip not in ips]
    assert missing == []
    assert "2001:4860:4860::8888" in ips and "::ffff:10.0.0.1" in ips


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


def _add_rule(rule):
    """Append an egress rule to the policies that select agents (63) and workspaces (60)."""
    def f(docs):
        for d in docs:
            if d["metadata"]["name"] in ("agent-isolation", "workspace-isolation"):
                d["spec"]["egress"].append(rule)
    return f


def _ipb(cidr, ports=None):
    r = {"to": [{"ipBlock": {"cidr": cidr}}]}
    if ports is not None:
        r["ports"] = ports
    return r


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
    # IPv6 / SCTP / UDP forms (-1ed), each injected through the policy data
    "IPv6 ::/0 granted": _add_rule(_ipb("::/0")),
    "IPv6 ::/0 granted on 443": _add_rule(_ipb("::/0", [{"port": 443}])),
    "IPv6 2000::/3 granted": _add_rule(_ipb("2000::/3")),
    "IPv6 ULA fc00::/7 granted": _add_rule(_ipb("fc00::/7")),
    "IPv6 link-local fe80::/10 granted": _add_rule(_ipb("fe80::/10")),
    "IPv6 loopback ::1/128 granted": _add_rule(_ipb("::1/128")),
    "IPv6 v4-mapped ::ffff:0:0/96 granted (folds to all of IPv4 incl. cgnat)":
    _add_rule(_ipb("::ffff:0:0/96")),
    "IPv6 ::/0 except ULA still grants global unicast": _add_rule(
        {"to": [{"ipBlock": {"cidr": "::/0", "except": ["fc00::/7"]}}]}),
    "IPv6 ::/0 SCTP only": _add_rule(_ipb("::/0", [{"protocol": "SCTP"}])),
    "SCTP to cgnat": _add_rule(_ipb("100.64.0.0/10", [{"protocol": "SCTP"}])),
    "UDP to cgnat": _add_rule(_ipb("100.64.0.0/10", [{"protocol": "UDP", "port": 53}])),
    "SCTP to 10/8 :443": _add_rule(_ipb("10.0.0.0/8", [{"protocol": "SCTP", "port": 443}])),
    "SCTP to link-local":
    _add_rule(_ipb("169.254.0.0/16", [{"protocol": "SCTP"}])),
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


@pytest.mark.parametrize("subject", SUBJECTS)
def test_ipv6_drift_names_the_subject_it_breaks(subject):
    """Second mutation, widening instead of removing: a global-unicast IPv6 grant breaks exactly
    the subject the rule selects, over every protocol."""
    bad = violations(_mut(_add_rule(_ipb("2000::/3"))))
    assert any(v[0] == subject and v[3] == "2001:4860:4860::8888" for v in bad), subject
    assert {v[1] for v in bad} == set(PROTOCOLS)
