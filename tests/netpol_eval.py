"""A small evaluator of Kubernetes NetworkPolicy INGRESS and EGRESS semantics
(enterpriseaiframework-017c, egress added by -d7b).

Question it answers: given a set of NetworkPolicy documents, a destination pod and a destination
port, which source peers are admitted (`admitted_peers`)? Or, mirrored, given a source pod and a
destination port, which destination peers may it reach (`admitted_egress_peers`)? Peers are enumerated from a finite universe built out of
every label/namespace/CIDR the policies mention plus "somebody else" values, so a widening
expressed in ANY equivalent form (omitted vs empty `ports`/`from`, `{}` selectors,
matchExpressions, namespaceSelector, ipBlock, endPort ranges, named ports) is caught by what it
ADMITS, not by how it is spelled.

Rules implemented (k8s NetworkPolicy v1):
  * a policy applies to a pod iff same namespace and spec.podSelector matches; a pod is
    ingress-isolated iff some applying policy has Ingress in policyTypes (default: Ingress is
    always implied). Not isolated => everything admitted. Isolated => union of applying rules.
  * a rule with `from` omitted, null or [] admits every peer; likewise `ports`.
  * peer = podSelector and/or namespaceSelector (both present => AND; podSelector alone =>
    the policy's own namespace) or ipBlock. `{}` selects all. matchLabels AND matchExpressions.
  * egress mirrors ingress: `egress`/`to` in place of `ingress`/`from`; a pod is
    egress-isolated iff an applying policy lists Egress in policyTypes (default: Egress only if the
    policy has an `egress` key). Rules of all applying policies are unioned. A STRING (named)
    port in an egress rule cannot be resolved against the peer, so it is treated as admitting
    (worst case) unless `named_ports` says otherwise.
  * IPv6: `0.0.0.0/0` covers no IPv6 address (only `::/0`, `2000::/3`... do), except that an
    IPv4-mapped address is also tested as its embedded IPv4. build_universe seeds public, ULA,
    link-local, loopback, multicast and IPv4-mapped IPv6 addresses plus each ipBlock's endpoints.
    Callers iterate PROTOCOLS (TCP, UDP, SCTP), never TCP alone.
  * port entry: protocol defaults TCP; `port` omitted => every port of that protocol;
    `port` int with optional endPort => range; `port` string => named container port.
"""
import ipaddress
import itertools
from dataclasses import dataclass, field

import yaml

OTHER = "__someone-else__"

# NetworkPolicy ports carry a protocol; a fence asserted on TCP alone says nothing about SCTP/UDP.
PROTOCOLS = ("TCP", "UDP", "SCTP")

# IPv6 (and v4-mapped) peers every universe contains, whatever the policies name (-1ed): an
# ipBlock `0.0.0.0/0` does not cover any of these, `::/0` or `2000::/3` covers the public one.
IPV6_SEEDS = (
    "2001:4860:4860::8888",   # public global unicast (2000::/3)
    "2606:4700:4700::1111",   # public global unicast, second /16
    "fd00::1",                # ULA fc00::/7
    "fdaa:1234::5",           # ULA, non-fd00 /16
    "fe80::1",                # link-local fe80::/10
    "::1",                    # loopback
    "::",                     # unspecified
    "ff02::1",                # multicast
    "::ffff:8.8.8.8",         # IPv4-mapped ::ffff:0:0/96, public embedded v4
    "::ffff:10.0.0.1",        # IPv4-mapped, private embedded v4
    "::ffff:100.100.100.100",  # IPv4-mapped, tailnet embedded v4
)


@dataclass(frozen=True)
class Pod:
    namespace: str
    labels: tuple  # sorted (k, v) pairs


@dataclass(frozen=True)
class IP:
    addr: str


@dataclass
class Dest:
    """The pod under protection: labels + namespace + its named container ports."""
    namespace: str
    labels: dict
    named_ports: dict = field(default_factory=dict)


def load_policies(paths, substitutions=None):
    """NetworkPolicy docs from yaml files. `substitutions` fills deploy-time __PLACEHOLDERS__."""
    out = []
    for p in paths:
        text = open(p).read()
        for k, v in (substitutions or {}).items():
            text = text.replace(k, v)
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy":
                out.append(doc)
    return out


def selector_matches(sel, labels: dict) -> bool:
    """`{}` (or an empty matchLabels/matchExpressions) matches everything."""
    for k, v in (sel.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for e in sel.get("matchExpressions") or []:
        k, op, vals = e["key"], e["operator"], e.get("values") or []
        if op == "In":
            ok = k in labels and labels[k] in vals
        elif op == "NotIn":
            ok = k not in labels or labels[k] not in vals
        elif op == "Exists":
            ok = k in labels
        elif op == "DoesNotExist":
            ok = k not in labels
        else:
            raise ValueError(f"unknown operator {op}")
        if not ok:
            return False
    return True


def _selectors_in(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("podSelector", "namespaceSelector") and isinstance(v, dict):
                yield k, v
            yield from _selectors_in(v)
    elif isinstance(obj, list):
        for i in obj:
            yield from _selectors_in(i)


def _kv_universe(selectors, cap=4096):
    """Label assignments over every key the selectors mention: each key takes each mentioned
    value, an unmentioned value, or is absent."""
    vals = {}
    for s in selectors:
        for k, v in (s.get("matchLabels") or {}).items():
            vals.setdefault(k, set()).add(v)
        for e in s.get("matchExpressions") or []:
            vals.setdefault(e["key"], set()).update(e.get("values") or [])
    keys = sorted(vals)
    options = [sorted(vals[k]) + [OTHER, None] for k in keys]
    n = 1
    for o in options:
        n *= len(o)
    if n > cap:
        raise ValueError(f"peer universe too large ({n}); simplify the policy or raise cap")
    for combo in itertools.product(*options):
        yield {k: v for k, v in zip(keys, combo) if v is not None}


NSNAME = "kubernetes.io/metadata.name"
_KEYS = {"ingress": ("ingress", "from"), "egress": ("egress", "to")}


def build_universe(policies, dest_namespace, direction="ingress", seeds=()):
    """Candidate peer pods (with their namespace's labels) and candidate external IPs.

    `seeds` are (namespace, labels) pairs the caller EXPECTS to be reachable: their labels and
    namespace join the universe even if no policy mentions them, so a policy that drifted away
    from naming them is still measured against them."""
    rules_key, peers_key = _KEYS[direction]
    pod_sels, ns_sels, blocks = [], [], []
    for pol in policies:
        for rule in pol.get("spec", {}).get(rules_key) or []:
            for kind, sel in _selectors_in(rule):
                (pod_sels if kind == "podSelector" else ns_sels).append(sel)
            for peer in rule.get(peers_key) or []:
                if peer.get("ipBlock"):
                    blocks.append(peer["ipBlock"])
    ns_names = {dest_namespace, "other-ns", "kube-system"}
    for sns, labels in seeds:
        pod_sels.append({"matchLabels": dict(labels)})
        ns_names.add(sns)
    for s in ns_sels:
        v = (s.get("matchLabels") or {}).get(NSNAME)
        if v:
            ns_names.add(v)
        for e in s.get("matchExpressions") or []:
            if e["key"] == NSNAME:
                ns_names.update(e.get("values") or [])
    ns_extra_sels = [{"matchLabels": {k: v for k, v in (s.get("matchLabels") or {}).items()
                                      if k != NSNAME},
                      "matchExpressions": [e for e in s.get("matchExpressions") or []
                                           if e["key"] != NSNAME]} for s in ns_sels]
    ns_variants = list(_kv_universe(ns_extra_sels))
    pods = []
    for pl in _kv_universe(pod_sels):
        for n in sorted(ns_names):
            for extra in ns_variants:
                pods.append((Pod(n, tuple(sorted(pl.items()))), {NSNAME: n, **extra}))
    ips = {"8.8.8.8", "10.42.0.99", "10.43.0.1", "127.0.0.1", "192.168.1.1", "172.20.0.1",
           "169.254.169.254", "100.100.100.100", *IPV6_SEEDS}
    for b in blocks:
        for c in [b["cidr"], *(b.get("except") or [])]:
            net = ipaddress.ip_network(c)
            ips.update({str(net.network_address), str(net[-1]), str(net[net.num_addresses // 2])})
            if net.version == 6 and net.num_addresses > 2:
                ips.add(str(net[1]))
    return pods, sorted(ips)


def _port_admits(entries, port: int, proto: str, named, unresolved_named=False) -> bool:
    if not entries:  # omitted, null and [] all mean "all ports"
        return True
    for e in entries:
        if (e.get("protocol") or "TCP") != proto:
            continue
        p = e.get("port")
        if p is None:
            return True
        if isinstance(p, str):
            if unresolved_named or named.get(p) == port:
                return True
            continue
        if p <= port <= (e.get("endPort") or p):
            return True
    return False


def _peer_admits(peer, policy_ns, src, src_ns_labels) -> bool:
    if isinstance(src, IP):
        b = peer.get("ipBlock")
        if not b:
            return False
        a = ipaddress.ip_address(src.addr)
        # A v4-mapped address is matched as BOTH its v6 form and its embedded v4 (Go's
        # net.IPNet.Contains, which CNIs build on, folds ::ffff:a.b.c.d to a.b.c.d): worst case.
        views = [a]
        if a.version == 6 and a.ipv4_mapped is not None:
            views.append(a.ipv4_mapped)

        def hit(cidr, v):
            net = ipaddress.ip_network(cidr)
            return net.version == v.version and v in net

        return any(hit(b["cidr"], v) and not any(hit(x, v) for x in b.get("except") or [])
                   for v in views)
    if peer.get("ipBlock"):
        return False
    ps, ns = peer.get("podSelector"), peer.get("namespaceSelector")
    if ns is not None:
        if not selector_matches(ns, src_ns_labels):
            return False
    elif src.namespace != policy_ns:
        return False
    return ps is None or selector_matches(ps, dict(src.labels))


def _admitted(policies, subject: Dest, port, proto, direction, seeds):
    rules_key, peers_key = _KEYS[direction]
    kind = direction.capitalize()
    pods, ips = build_universe(policies, subject.namespace, direction, seeds)
    applying = []
    for p in policies:
        spec = p["spec"]
        types = spec.get("policyTypes") or (["Ingress"] + (["Egress"] if "egress" in spec else []))
        if (p["metadata"].get("namespace", "default") == subject.namespace
                and selector_matches(spec.get("podSelector") or {}, subject.labels)
                and kind in types):
            applying.append(p)
    if not applying:
        return [p for p, _ in pods], ips
    live = [(p["metadata"].get("namespace", "default"), r)
            for p in applying for r in (p["spec"].get(rules_key) or [])
            if _port_admits(r.get("ports"), port, proto, subject.named_ports,
                            unresolved_named=direction == "egress")]

    def admitted(src, nsl):
        for ns, r in live:
            frm = r.get(peers_key)
            if not frm or any(_peer_admits(x, ns, src, nsl) for x in frm):
                return True
        return False

    return ([pod for pod, nsl in pods if admitted(pod, nsl)],
            [ip for ip in ips if admitted(IP(ip), {})])


def admitted_peers(policies, dest: Dest, port: int, proto: str = "TCP", seeds=()):
    """(admitted_pods, admitted_ips) for traffic to dest:port. A pod no policy isolates for
    Ingress admits everything."""
    return _admitted(policies, dest, port, proto, "ingress", seeds)


def admitted_egress_peers(policies, src: Dest, port: int, proto: str = "TCP", seeds=()):
    """(reachable_pods, reachable_ips) for traffic FROM `src` to a destination on `port`. Only
    the source's egress policies are consulted (the destination's ingress is a separate
    question, asked with `admitted_peers`). A pod no policy isolates for Egress reaches everything."""
    return _admitted(policies, src, port, proto, "egress", seeds)


def probe_ports(policies, extra=()):
    """Ports worth asking about: every port/endPort any policy names, its neighbours, the range
    ends and a port nobody names. An admission that differs across the port axis changes at
    one of these."""
    out = {1, 65535, 9999, *extra}
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("port", "endPort") and isinstance(v, int):
                    out.update({v - 1, v, v + 1})
                else:
                    walk(v)
        elif isinstance(o, list):
            for i in o:
                walk(i)
    walk(policies)
    return sorted(p for p in out if 1 <= p <= 65535)


def dest_from_template(path, placeholder_value="swtest"):
    """The pod (labels, namespace, named ports) a provisioning template really stamps out."""
    text = open(path).read()
    import re
    text = re.sub(r"__[A-Z_]+__", placeholder_value, text)
    for doc in yaml.safe_load_all(text):
        if doc and doc.get("kind") == "Deployment":
            tpl = doc["spec"]["template"]
            named = {p["name"]: p["containerPort"] for c in tpl["spec"]["containers"]
                     for p in c.get("ports", []) if "name" in p}
            return Dest(doc["metadata"]["namespace"], dict(tpl["metadata"]["labels"]), named)
    raise AssertionError(f"no Deployment in {path}")
