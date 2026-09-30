"""Contract G's network half: the relay is the ONLY way from a Raven to another agent.

Item enterpriseaiframework-692 (agents-raven.md); rebuilt by -d7b to EVALUATE what the shipped
NetworkPolicies admit (tests/netpol_eval.py, Kubernetes ingress + egress semantics) instead of
pattern-matching their spelling. The first version read only `port` and skipped `endPort`, and
accepted `to: []`, `{}` selectors and empty `ports` in the raven rules.

  * Raven pod EGRESS, from EVERY policy that selects it (68 `raven-isolation`, and 63/60/66
    if one ever re-selects it: policies are additive): exactly DNS, gateway:4000,
    freerouter:8080, control-plane:8000 and no external address.
  * hermes API :8642 INGRESS, on a hermes pod and on a Raven pod: the control-plane pod only.

Expected values are hand-written predicates over peers (from the design record), not read back
from the files. Each control poisons the policy FILES (yaml written to disk and reloaded) in
every equivalent language form, plus a second, different mutation. The live measurement of the
same fences, from a real Raven pod, is tests-live/test_agent_relay_live.py.
"""

import copy
from pathlib import Path

import pytest
import yaml

from netpol_eval import (admitted_egress_peers, admitted_peers, build_universe,
                         dest_from_template, load_policies, probe_ports)

K8S = Path(__file__).resolve().parent.parent / "deploy" / "k8s"
NS = "enterprise-ai"
DEPLOY_VALUES = {"__LAN_CIDR__": "192.168.0.0/16", "__GATEWAY_LAN_IP__": "192.168.2.42"}

RAVEN = dest_from_template(K8S / "69-agent-raven.template.yaml")
HERMES = dest_from_template(K8S / "65-agent-hermes.template.yaml")

# What a Raven pod may reach, by hand from agents-raven.md Contract E/G: (namespace, labels,
# {(protocol, port)}). Also the seeds that put these peers in the evaluator's universe even if
# a drifted policy stops naming them.
RAVEN_MAY_REACH = [
    ("kube-system", {"k8s-app": "kube-dns"}, {("TCP", 53), ("UDP", 53)}),
    (NS, {"app": "gateway"}, {("TCP", 4000)}),
    (NS, {"app": "freerouter"}, {("TCP", 8080)}),
    (NS, {"app": "control-plane"}, {("TCP", 8000)}),
]
SEEDS = [(ns, labels) for ns, labels, _ in RAVEN_MAY_REACH]
CP_SEED = [(NS, {"app": "control-plane"})]


def _raven_may_reach(pod, port, proto) -> bool:
    labels = dict(pod.labels)
    return any(pod.namespace == ns and all(labels.get(k) == v for k, v in sel.items())
               and (proto, port) in ports for ns, sel, ports in RAVEN_MAY_REACH)


def _policy_files():
    return sorted(p for p in K8S.glob("*.yaml") if "kind: NetworkPolicy" in p.read_text())


def _shipped() -> list[dict]:
    return load_policies(_policy_files(), DEPLOY_VALUES)


def raven_egress_violations(policies) -> list:
    """Empty iff, on every probe port and protocol, a Raven pod reaches EXACTLY the hand-written
    allow-list among the peer universe, and no external address at all."""
    bad = []
    universe = {p for p, _ in build_universe(policies, NS, "egress", SEEDS)[0]}
    for proto in ("TCP", "UDP"):
        for port in probe_ports(policies, extra=(53, 4000, 8080, 8000, 8642, 443)):
            pods, ips = admitted_egress_peers(policies, RAVEN, port, proto, SEEDS)
            got = set(pods)
            want = {p for p in universe if _raven_may_reach(p, port, proto)}
            if got != want or ips:
                bad.append((proto, port, "extra:" + str(sorted(map(str, got - want))[:2]),
                            "missing:" + str(sorted(map(str, want - got))[:2]), ips[:2]))
    return bad


def _control_plane_only(pod) -> bool:
    return pod.namespace == NS and dict(pod.labels).get("app") == "control-plane"


def hermes_api_violations(policies) -> list:
    """Empty iff :8642 on a hermes pod and on a Raven pod admits exactly the control-plane pod
    (in the platform namespace) and no external address."""
    bad = []
    universe = {p for p, _ in build_universe(policies, NS, "ingress", CP_SEED)[0]}
    want = {p for p in universe if _control_plane_only(p)}
    assert want, "the peer universe has no control-plane pod: the pin would be vacuous"
    for kind, dest in (("hermes", HERMES), ("raven", RAVEN)):
        pods, ips = admitted_peers(policies, dest, 8642, "TCP", CP_SEED)
        got = set(pods)
        if got != want or ips:
            bad.append((kind, sorted(map(str, got - want))[:2], sorted(map(str, want - got))[:2],
                        ips[:2]))
    return bad


def test_a_raven_may_egress_only_to_dns_the_model_routes_and_the_control_plane():
    assert raven_egress_violations(_shipped()) == []


def test_the_hermes_api_port_is_admitted_from_the_control_plane_only():
    assert hermes_api_violations(_shipped()) == []


def test_the_expectation_is_not_vacuous():
    """The hand-written allow-list is really reachable: the evaluator finds each peer."""
    pods, ips = admitted_egress_peers(_shipped(), RAVEN, 8000, "TCP", SEEDS)
    assert any(_control_plane_only(p) for p in pods) and ips == []
    assert admitted_egress_peers(_shipped(), RAVEN, 9999, "TCP", SEEDS) == ([], [])


# --- controls: poison the FILES, in every equivalent form ---------------------------------


def _by_name(docs, name):
    return next(d for d in docs if d["metadata"]["name"] == name)


def _mutate(tmp_path, edit):
    """Edit the loaded policies, write them out as a yaml FILE, reload through the loader."""
    docs = copy.deepcopy(_shipped())
    extra = edit(docs)
    extra = extra if isinstance(extra, list) else []
    p = tmp_path / "policies.yaml"
    p.write_text(yaml.safe_dump_all(docs + extra))
    return load_policies([p])


def _raven(docs):
    return _by_name(docs, "raven-isolation")


def _cp_rule(docs):
    return next(r for r in _raven(docs)["spec"]["egress"]
                if r["to"][0].get("podSelector", {}).get("matchLabels") == {"app": "control-plane"})


def _gw_rule(docs):
    return next(r for r in _raven(docs)["spec"]["egress"]
                if r["to"][0].get("podSelector", {}).get("matchLabels") == {"app": "gateway"})


def _egress_rule(rule):
    return lambda docs: _raven(docs)["spec"]["egress"].append(rule)


def _extra_policy(spec, sel=None, name="extra-d7b"):
    def edit(docs):
        return [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                 "metadata": {"name": name, "namespace": NS},
                 "spec": {"podSelector": sel if sel is not None else
                          {"matchLabels": {"agent.enterprise-ai/type": "raven"}}, **spec}}]
    return edit


def _set_ports(getter, ports):
    def edit(docs):
        rule = getter(docs)
        if ports is ...:
            rule.pop("ports")
        else:
            rule["ports"] = ports
    return edit


def _set_to(getter, to):
    def edit(docs):
        rule = getter(docs)
        if to is ...:
            rule.pop("to")
        else:
            rule["to"] = to
    return edit


ANY_APP = {"matchExpressions": [{"key": "app", "operator": "Exists"}]}

RAVEN_DRIFTS = {
    # the widenings named in the audit
    "to-empty-list + ports 443 (every destination)": _egress_rule({"to": [], "ports": [{"port": 443}]}),
    "to-omitted + ports 443": _egress_rule({"ports": [{"protocol": "TCP", "port": 443}]}),
    "to namespaceSelector {}": _egress_rule({"to": [{"namespaceSelector": {}}],
                                             "ports": [{"port": 8000}]}),
    "to podSelector {}": _egress_rule({"to": [{"podSelector": {}}], "ports": [{"port": 8000}]}),
    "gateway rule ports: []": _set_ports(_gw_rule, []),
    "gateway rule ports omitted": _set_ports(_gw_rule, ...),
    "gateway rule ports null": _set_ports(_gw_rule, None),
    "gateway rule port entry has no port": _set_ports(_gw_rule, [{"protocol": "TCP"}]),
    "gateway rule port entry {}": _set_ports(_gw_rule, [{}]),
    "namespaceSelector matchExpressions Exists": _egress_rule({
        "to": [{"namespaceSelector": {"matchExpressions": [
            {"key": "kubernetes.io/metadata.name", "operator": "Exists"}]}}],
        "ports": [{"port": 8000}]}),
    "namespaceSelector NotIn nobody": _egress_rule({
        "to": [{"namespaceSelector": {"matchExpressions": [
            {"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["x"]}]}}],
        "ports": [{"port": 8000}]}),
    "any app (podSelector Exists)": _egress_rule({"to": [{"podSelector": ANY_APP}],
                                                  "ports": [{"port": 8000}]}),
    "other agents (component=agent)": _egress_rule({
        "to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "agent"}}}],
        "ports": [{"port": 8642}]}),
    "postgres": _egress_rule({"to": [{"podSelector": {"matchLabels": {"app": "postgres"}}}],
                              "ports": [{"port": 5432}]}),
    # port ranges: the endPort blind spot
    "cp rule endPort 8000-8700": _set_ports(_cp_rule, [{"protocol": "TCP", "port": 8000, "endPort": 8700}]),
    "gateway rule endPort 4000-4100": _set_ports(_gw_rule, [{"port": 4000, "endPort": 4100}]),
    "cp rule + hermes API 8642": lambda d: _cp_rule(d)["ports"].append({"protocol": "TCP", "port": 8642}),
    "cp rule + UDP 8000": lambda d: _cp_rule(d)["ports"].append({"protocol": "UDP", "port": 8000}),
    "cp rule named port": _set_ports(_cp_rule, [{"port": "api"}]),
    "DNS range 1-65535": lambda d: _raven(d)["spec"]["egress"][0]["ports"].append(
        {"port": 1, "endPort": 65535}),
    # cp rule peers
    "cp rule widened to the namespace": _set_to(_cp_rule, [{"namespaceSelector": {}}]),
    "cp rule + every agent pod": lambda d: _cp_rule(d)["to"].append(
        {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "agent"}}}),
    "cp rule to omitted": _set_to(_cp_rule, ...),
    "cp rule to []": _set_to(_cp_rule, []),
    "cp rule ports dropped": _set_ports(_cp_rule, ...),
    "dns rule widened to every kube-system pod": lambda d: _raven(d)["spec"]["egress"][0]["to"][0]
    .pop("podSelector"),
    "dns rule namespaceSelector dropped (kube-dns label in any namespace)":
    lambda d: _raven(d)["spec"]["egress"][0].__setitem__(
        "to", [{"podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}]),
    # the internet, in every spelling
    "ipBlock 0.0.0.0/0": _egress_rule({"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}),
    "ipBlock 0.0.0.0/0 except private": _egress_rule({"to": [{"ipBlock": {
        "cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]}}],
        "ports": [{"port": 443}]}),
    "ipBlock pod CIDR": _egress_rule({"to": [{"ipBlock": {"cidr": "10.42.0.0/16"}}]}),
    "ipBlock public /8": _egress_rule({"to": [{"ipBlock": {"cidr": "8.0.0.0/8"}}],
                                       "ports": [{"port": 443}]}),
    # the rest of the mechanism
    "egress rule {}": _egress_rule({}),
    "egress policyType removed": lambda d: _raven(d)["spec"].__setitem__("policyTypes", ["Ingress"]),
    "egress list emptied to allow-all": lambda d: _raven(d)["spec"].__setitem__("egress", [{}]),
    "raven policy deleted": lambda d: d.remove(_raven(d)),
    "raven policy podSelector matches nothing": lambda d: _raven(d)["spec"]["podSelector"][
        "matchLabels"].__setitem__("agent.enterprise-ai/type", "ravens"),
    # policies are additive: a second policy that selects Ravens
    "second policy, egress [{}], matchLabels type=raven": _extra_policy(
        {"policyTypes": ["Egress"], "egress": [{}]}),
    "second policy, egress [{}], podSelector {}": _extra_policy(
        {"policyTypes": ["Egress"], "egress": [{}]}, {}),
    "second policy, internet by ipBlock, component=agent": _extra_policy(
        {"policyTypes": ["Egress"], "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]},
        {"matchLabels": {"app.kubernetes.io/component": "agent"}}),
    "second policy, no policyTypes, has egress": _extra_policy(
        {"egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]}),
    # 63 `agent-isolation` (public-internet egress) re-selects Ravens
    "63 exclusion dropped": lambda d: _by_name(d, "agent-isolation")["spec"]["podSelector"].pop(
        "matchExpressions"),
    "63 exclusion values misspelled": lambda d: _by_name(d, "agent-isolation")["spec"][
        "podSelector"]["matchExpressions"][0].__setitem__("values", ["ravens"]),
    "63 exclusion operator In": lambda d: _by_name(d, "agent-isolation")["spec"]["podSelector"][
        "matchExpressions"][0].__setitem__("operator", "In"),
    "63 exclusion key misspelled": lambda d: _by_name(d, "agent-isolation")["spec"][
        "podSelector"]["matchExpressions"][0].__setitem__("key", "agent.enterprise-ai/kind"),
    # second-mutation direction "remove": something the Raven needs is gone / relabelled
    "cp rule removed": lambda d: _raven(d)["spec"]["egress"].remove(_cp_rule(d)),
    "cp relabelled control-plane-v2": lambda d: _cp_rule(d)["to"][0]["podSelector"][
        "matchLabels"].__setitem__("app", "control-plane-v2"),
    "gateway port moved 4000 -> 4001": _set_ports(_gw_rule, [{"protocol": "TCP", "port": 4001}]),
}


@pytest.mark.parametrize("drift", sorted(RAVEN_DRIFTS))
def test_every_drift_of_the_raven_egress_is_caught(tmp_path, drift):
    assert raven_egress_violations(_mutate(tmp_path, RAVEN_DRIFTS[drift])), drift


def _hermes_rule(docs):
    return _by_name(docs, "agent-console-isolation")["spec"]["ingress"][0]


def _add_66_rule(rule):
    return lambda docs: _by_name(docs, "agent-console-isolation")["spec"]["ingress"].append(rule)


HERMES_DRIFTS = {
    "from every agent pod": lambda d: _hermes_rule(d).__setitem__(
        "from", [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "agent"}}}]),
    "+ raven pods": lambda d: _hermes_rule(d)["from"].append(
        {"podSelector": {"matchLabels": {"agent.enterprise-ai/type": "raven"}}}),
    "from removed (everyone)": lambda d: _hermes_rule(d).pop("from"),
    "from []": lambda d: _hermes_rule(d).__setitem__("from", []),
    "+ podSelector {}": lambda d: _hermes_rule(d)["from"].append({"podSelector": {}}),
    "second rule 8642 from the namespace": _add_66_rule(
        {"from": [{"namespaceSelector": {}}], "ports": [{"protocol": "TCP", "port": 8642}]}),
    # the audit's named hole: a range, from every namespace
    "second rule 8000-8700 from all namespaces (endPort)": _add_66_rule(
        {"from": [{"namespaceSelector": {}}], "ports": [{"port": 8000, "endPort": 8700}]}),
    "second rule 8642-8650 from all namespaces": _add_66_rule(
        {"from": [{"namespaceSelector": {}}], "ports": [{"port": 8642, "endPort": 8650}]}),
    "second rule 1-65535 podSelector {}": _add_66_rule(
        {"from": [{"podSelector": {}}], "ports": [{"port": 1, "endPort": 65535}]}),
    "second rule ports [] from namespace": _add_66_rule(
        {"from": [{"namespaceSelector": {}}], "ports": []}),
    "second rule ports omitted from namespace": _add_66_rule({"from": [{"namespaceSelector": {}}]}),
    "second rule port entry no port": _add_66_rule(
        {"from": [{"namespaceSelector": {}}], "ports": [{"protocol": "TCP"}]}),
    "second rule named port": _add_66_rule(
        {"from": [{"namespaceSelector": {}}], "ports": [{"port": "api"}]}),
    "second rule matchExpressions Exists": _add_66_rule(
        {"from": [{"podSelector": ANY_APP}], "ports": [{"port": 8642}]}),
    "second rule ipBlock 0.0.0.0/0": _add_66_rule(
        {"from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"port": 8642}]}),
    "second rule {}": _add_66_rule({}),
    "control-plane in every namespace": lambda d: _hermes_rule(d).__setitem__(
        "from", [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"app": "control-plane"}}}]),
    "separate policy opens ingress to all agents": _extra_policy(
        {"policyTypes": ["Ingress"], "ingress": [{}]},
        {"matchLabels": {"app.kubernetes.io/component": "agent"}}, "extra-hermes-d7b"),
    # second mutation direction "remove"/"relabel"
    "8642 dropped from the rule": lambda d: _hermes_rule(d).__setitem__(
        "ports", [p for p in _hermes_rule(d)["ports"] if p["port"] != 8642]),
    "control-plane relabelled": lambda d: _hermes_rule(d).__setitem__(
        "from", [{"podSelector": {"matchLabels": {"app": "control-plane-v2"}}}]),
    "8642 moved to 8643": lambda d: _hermes_rule(d).__setitem__(
        "ports", [{"protocol": "TCP", "port": 8643 if p["port"] == 8642 else p["port"]}
                  for p in _hermes_rule(d)["ports"]]),
}


@pytest.mark.parametrize("drift", sorted(HERMES_DRIFTS))
def test_every_drift_of_the_hermes_api_fence_is_caught(tmp_path, drift):
    assert hermes_api_violations(_mutate(tmp_path, HERMES_DRIFTS[drift])), drift


GREEN = {
    "hermes: wide peer on an unrelated port range": _add_66_rule(
        {"from": [{"namespaceSelector": {}}], "ports": [{"port": 8000, "endPort": 8600}]}),
    "hermes: wide UDP-only rule": _add_66_rule(
        {"from": [{"namespaceSelector": {}}], "ports": [{"protocol": "UDP", "port": 8642}]}),
    "hermes: control-plane restated as matchExpressions": lambda d: _hermes_rule(d).__setitem__(
        "from", [{"podSelector": {"matchExpressions": [
            {"key": "app", "operator": "In", "values": ["control-plane"]}]}}]),
    "policy for other pods": _extra_policy(
        {"policyTypes": ["Egress"], "egress": [{}]}, {"matchLabels": {"app": "something-else"}}),
}


@pytest.mark.parametrize("name", sorted(GREEN))
def test_benign_additions_stay_green(tmp_path, name):
    pol = _mutate(tmp_path, GREEN[name])
    assert raven_egress_violations(pol) == [] and hermes_api_violations(pol) == []
