"""A small evaluator of Kubernetes NetworkPolicy INGRESS semantics (enterpriseaiframework-017c).

Question it answers: given a set of NetworkPolicy documents, a destination pod and a destination
port, which source peers are admitted? Peers are enumerated from a finite universe built out of
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
  * port entry: protocol defaults TCP; `port` omitted => every port of that protocol;
    `port` int with optional endPort => range; `port` string => named container port.
"""
import ipaddress
import itertools
from dataclasses import dataclass, field

import yaml

OTHER = "__someone-else__"


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


def build_universe(policies, dest_namespace):
    """Candidate source pods (with their namespace's labels) and candidate external IPs."""
    pod_sels, ns_sels, blocks = [], [], []
    for pol in policies:
        for rule in pol.get("spec", {}).get("ingress") or []:
            for kind, sel in _selectors_in(rule):
                (pod_sels if kind == "podSelector" else ns_sels).append(sel)
            for peer in rule.get("from") or []:
                if peer.get("ipBlock"):
                    blocks.append(peer["ipBlock"])
    ns_names = {dest_namespace, "other-ns", "kube-system"}
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
    ips = {"8.8.8.8", "10.42.0.99", "127.0.0.1", "192.168.1.1"}
    for b in blocks:
        for c in [b["cidr"], *(b.get("except") or [])]:
            net = ipaddress.ip_network(c)
            if net.version == 4:
                ips.update({str(net.network_address), str(net.broadcast_address)})
    return pods, sorted(ips)


def _port_admits(entries, port: int, proto: str, named: dict) -> bool:
    if not entries:  # omitted, null and [] all mean "all ports"
        return True
    for e in entries:
        if (e.get("protocol") or "TCP") != proto:
            continue
        p = e.get("port")
        if p is None:
            return True
        if isinstance(p, str):
            if named.get(p) == port:
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
        if a not in ipaddress.ip_network(b["cidr"]):
            return False
        return not any(a in ipaddress.ip_network(x) for x in b.get("except") or [])
    if peer.get("ipBlock"):
        return False
    ps, ns = peer.get("podSelector"), peer.get("namespaceSelector")
    if ns is not None:
        if not selector_matches(ns, src_ns_labels):
            return False
    elif src.namespace != policy_ns:
        return False
    return ps is None or selector_matches(ps, dict(src.labels))


def admitted_peers(policies, dest: Dest, port: int, proto: str = "TCP"):
    """(admitted_pods, admitted_ips) for traffic to dest:port. A pod no policy isolates for
    Ingress admits everything."""
    pods, ips = build_universe(policies, dest.namespace)
    applying = [p for p in policies
                if p["metadata"].get("namespace", "default") == dest.namespace
                and selector_matches(p["spec"].get("podSelector") or {}, dest.labels)
                and "Ingress" in (p["spec"].get("policyTypes") or ["Ingress"])]
    if not applying:
        return [p for p, _ in pods], ips
    live = [(p["metadata"].get("namespace", "default"), r)
            for p in applying for r in (p["spec"].get("ingress") or [])
            if _port_admits(r.get("ports"), port, proto, dest.named_ports)]

    def admitted(src, nsl):
        for ns, r in live:
            frm = r.get("from")
            if not frm or any(_peer_admits(x, ns, src, nsl) for x in frm):
                return True
        return False

    return ([pod for pod, nsl in pods if admitted(pod, nsl)],
            [ip for ip in ips if admitted(IP(ip), {})])
