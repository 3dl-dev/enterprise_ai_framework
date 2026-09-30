"""Hermetic pin of who may reach the agent API port :8642 and Raven's :18793
(enterpriseaiframework-1d09, rebuilt by -017c after the audit found 'ports: []' slipped past a
syntax pattern-match).

The live fence test (tests-live/test_hermes_api_server_isolation.py) measures the network; this
evaluates the SHIPPED NetworkPolicy DATA with tests/netpol_eval.py (Kubernetes ingress semantics)
so any widening -- however spelled -- fails at `make test` without a cluster. Assertion: for every
agent pod shape (hermes, raven) and each port, the admitted peer set is exactly
{pods labelled app=control-plane in the enterprise-ai namespace}, and no external IP.

Expected value: a hand-written predicate over the peer universe, not read from the files under
test. Controls poison the policy FILES (the yaml the evaluator reads) in every equivalent form.
"""
import copy
from pathlib import Path

import pytest
import yaml

from netpol_eval import Dest, admitted_peers, build_universe, load_policies

K8S = Path(__file__).resolve().parent.parent / "deploy/k8s"
NS = "enterprise-ai"
PORTS = (8642, 18793)
POLICY_66 = "agent-console-isolation"


def _policy_files():
    return sorted(p for p in K8S.glob("*.yaml") if "kind: NetworkPolicy" in p.read_text())


def _dest(template: str) -> Dest:
    text = (K8S / template).read_text()
    for ph in ("USER", "NAME", "MODEL_SOURCE", "CFGSUM", "KEYSUM", "KEY_SECRET", "IMAGE",
               "DISCORDSUM", "EMAILSUM", "SLACKSUM"):
        text = text.replace(f"__{ph}__", "swtest017c")
    for doc in yaml.safe_load_all(text):
        if doc and doc.get("kind") == "Deployment":
            tpl = doc["spec"]["template"]
            named = {p["name"]: p["containerPort"] for c in tpl["spec"]["containers"]
                     for p in c.get("ports", []) if "name" in p}
            return Dest(doc["metadata"]["namespace"], dict(tpl["metadata"]["labels"]), named)
    raise AssertionError(template)


# The pod label sets come from the real provisioning templates, not hand-typed.
DESTS = {"hermes": _dest("65-agent-hermes.template.yaml"),
         "raven": _dest("69-agent-raven.template.yaml")}


def _control_plane_only(pod) -> bool:  # hand-written expectation, independent of the evaluator
    return pod.namespace == NS and dict(pod.labels).get("app") == "control-plane"


def violations(policies) -> list:
    """Empty iff, for every agent pod shape and port, admitted == control-plane-in-ns exactly."""
    universe = {p for p, _ in build_universe(policies, NS)[0]}
    want = {p for p in universe if _control_plane_only(p)}
    assert want, "universe must contain control-plane peers or the pin is vacuous"
    bad = []
    for kind, dest in DESTS.items():
        for port in PORTS:
            pods, ips = admitted_peers(policies, dest, port)
            got = set(pods)
            if got != want or ips:
                bad.append((kind, port, sorted(map(str, got - want))[:2],
                            sorted(map(str, want - got))[:2], ips[:2]))
    return bad


# Deploy-time placeholders in the shipped policies (60-workspace-common.yaml), given sample values.
DEPLOY_VALUES = {"__LAN_CIDR__": "192.168.0.0/16", "__GATEWAY_LAN_IP__": "192.168.2.42"}


def _shipped():
    return load_policies(_policy_files(), DEPLOY_VALUES)


def _mutate(tmp_path, edit):
    """Write the (edited) policies out as yaml FILES and reload them through the loader."""
    docs = copy.deepcopy(_shipped())
    extra = edit(docs) or []
    p = tmp_path / "policies.yaml"
    p.write_text(yaml.safe_dump_all(docs + extra))
    return load_policies([p])


def _by_name(docs, name):
    return next(d for d in docs if d["metadata"]["name"] == name)


def test_shipped_policies_admit_exactly_the_control_plane():
    assert violations(_shipped()) == []


def test_agent_pods_are_isolated_and_unlisted_ports_admit_nobody():
    # Guard against a vacuous pass: an isolated pod admits nobody on a port no rule names.
    for dest in DESTS.values():
        assert admitted_peers(_shipped(), dest, 9999) == ([], [])


def _rule(frm, ports):
    rule = {}
    if frm is not ...:
        rule["from"] = frm
    if ports is not ...:
        rule["ports"] = ports
    return rule


def _add_rule_to_66(rule):
    def edit(docs):
        _by_name(docs, POLICY_66)["spec"]["ingress"].append(rule)
    return edit


def _add_policy(spec_extra, selector=None, name="extra-017c"):
    def edit(docs):
        sel = {"matchLabels": {"app.kubernetes.io/component": "agent"}} if selector is None \
            else selector
        return [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                 "metadata": {"name": name, "namespace": NS},
                 "spec": {"podSelector": sel, **spec_extra}}]
    return edit


WIDE_FROMS = {
    "from-omitted": ...,
    "from-empty-list": [],
    "podSelector-{}": [{"podSelector": {}}],
    "podSelector-NotIn-nobody": [{"podSelector": {"matchExpressions": [
        {"key": "nobody", "operator": "NotIn", "values": ["x"]}]}}],
    "podSelector-DoesNotExist": [{"podSelector": {"matchExpressions": [
        {"key": "nobody", "operator": "DoesNotExist"}]}}],
    "podSelector-part-of": [{"podSelector": {"matchLabels": {
        "app.kubernetes.io/part-of": "enterprise-ai-framework"}}}],
    "podSelector-app-Exists": [{"podSelector": {"matchExpressions": [
        {"key": "app", "operator": "Exists"}]}}],
    "podSelector-app-In-cp-and-gateway": [{"podSelector": {"matchExpressions": [
        {"key": "app", "operator": "In", "values": ["control-plane", "gateway"]}]}}],
    "namespaceSelector-{}": [{"namespaceSelector": {}}],
    "namespaceSelector-kube-system": [{"namespaceSelector": {"matchLabels": {
        "kubernetes.io/metadata.name": "kube-system"}}}],
    "namespaceSelector-NotIn": [{"namespaceSelector": {"matchExpressions": [
        {"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["x"]}]}}],
    "ns{}+pod-app-cp (other-namespace control-plane)": [
        {"namespaceSelector": {}, "podSelector": {"matchLabels": {"app": "control-plane"}}}],
    "ipBlock-0.0.0.0/0": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
    "ipBlock-0.0.0.0/0-except-private": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]}}],
    "ipBlock-pod-cidr": [{"ipBlock": {"cidr": "10.42.0.0/16"}}],
}
ALL_PORT_FORMS = {
    "ports-omitted": ...,
    "ports-empty-list": [],
    "ports-null": None,
    "port-omitted-in-entry": [{"protocol": "TCP"}],
    "empty-entry": [{}],
}
API_PORT_FORMS = {
    "int-8642": [{"protocol": "TCP", "port": 8642}],
    "int-8642-no-protocol": [{"port": 8642}],
    "range-8000-9000": [{"protocol": "TCP", "port": 8000, "endPort": 9000}],
    "range-starting-at-8642": [{"port": 8642, "endPort": 8650}],
    "named-api": [{"protocol": "TCP", "port": "api"}],
    "int-18793": [{"protocol": "TCP", "port": 18793}],
    "range-18000-19000": [{"protocol": "TCP", "port": 18000, "endPort": 19000}],
}


@pytest.mark.parametrize("fname", WIDE_FROMS)
@pytest.mark.parametrize("pname", ALL_PORT_FORMS)
def test_wide_peer_with_all_ports_form_is_red(tmp_path, fname, pname):
    pol = _mutate(tmp_path, _add_rule_to_66(_rule(WIDE_FROMS[fname], ALL_PORT_FORMS[pname])))
    assert violations(pol), (fname, pname)


@pytest.mark.parametrize("fname", WIDE_FROMS)
@pytest.mark.parametrize("pname", API_PORT_FORMS)
def test_wide_peer_with_port_form_is_red(tmp_path, fname, pname):
    pol = _mutate(tmp_path, _add_rule_to_66(_rule(WIDE_FROMS[fname], API_PORT_FORMS[pname])))
    assert violations(pol), (fname, pname)


def test_whole_rule_empty_is_red(tmp_path):
    assert violations(_mutate(tmp_path, _add_rule_to_66({})))


@pytest.mark.parametrize("fname", ["from-omitted", "podSelector-{}", "namespaceSelector-{}",
                                   "ipBlock-0.0.0.0/0"])
@pytest.mark.parametrize("sel", [None, {}, {"matchExpressions": [
    {"key": "app.kubernetes.io/component", "operator": "In", "values": ["agent"]}]}],
    ids=["matchLabels-component", "empty-selector-all-pods", "matchExpressions-In"])
def test_wide_rule_in_a_separate_policy_is_red(tmp_path, fname, sel):
    rule = _rule(WIDE_FROMS[fname], [])
    pol = _mutate(tmp_path, _add_policy({"policyTypes": ["Ingress"], "ingress": [rule]}, sel))
    assert violations(pol)


def test_second_mutation_relabel_control_plane_is_red(tmp_path):
    def edit(docs):
        for name in (POLICY_66, "raven-isolation", "agent-isolation"):
            for r in _by_name(docs, name)["spec"]["ingress"]:
                for peer in r["from"]:
                    peer["podSelector"]["matchLabels"] = {"app": "control-plane-v2"}
    assert violations(_mutate(tmp_path, edit))


def test_second_mutation_drop_port_is_red(tmp_path):
    # the 'remove' direction: control plane no longer admitted => not "exactly" the cp set.
    def edit(docs):
        r = _by_name(docs, POLICY_66)["spec"]["ingress"][0]
        r["ports"] = [p for p in r["ports"] if p["port"] != 8642]
    bad = violations(_mutate(tmp_path, edit))
    assert any(b[0] == "hermes" and b[1] == 8642 for b in bad)


def test_widening_existing_rule_by_adding_a_peer_is_red(tmp_path):
    def edit(docs):
        _by_name(docs, POLICY_66)["spec"]["ingress"][0]["from"].append({"podSelector": {}})
    assert violations(_mutate(tmp_path, edit))


# Controls that must stay GREEN: prove the evaluator is not simply flagging every extra rule.
GREEN = {
    # `ports: []` on the control-plane-only rule leaves the PEER set exact.
    "cp-rule-ports-emptied": lambda docs: _by_name(docs, POLICY_66)["spec"]["ingress"][0]
    .__setitem__("ports", []),
    "wide-peer-but-other-port": _add_rule_to_66(_rule(
        [{"podSelector": {}}], [{"protocol": "TCP", "port": 9000}])),
    "wide-peer-udp-only": _add_rule_to_66(_rule(
        [{"podSelector": {}}], [{"protocol": "UDP", "port": 8642}])),
    "wide-peer-range-misses-both": _add_rule_to_66(_rule(
        [{"podSelector": {}}], [{"port": 8643, "endPort": 9000}])),
    "wide-peer-unrelated-named-port": _add_rule_to_66(_rule(
        [{"podSelector": {}}], [{"port": "dashboard"}])),
    "cp-again-different-form": _add_rule_to_66(_rule(
        [{"podSelector": {"matchExpressions": [
            {"key": "app", "operator": "In", "values": ["control-plane"]}]}}], [])),
    "egress-only-policy-wide-from": _add_policy(
        {"policyTypes": ["Egress"], "ingress": [{"from": [{"podSelector": {}}]}]}),
    "wide-policy-selecting-other-pods": _add_policy(
        {"policyTypes": ["Ingress"], "ingress": [{}]},
        {"matchLabels": {"app": "something-else"}}),
    "wide-policy-in-other-namespace": lambda docs: [{
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "x", "namespace": "other-ns"},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{}]}}],
}


@pytest.mark.parametrize("name", GREEN)
def test_benign_additions_stay_green(tmp_path, name):
    assert violations(_mutate(tmp_path, GREEN[name])) == []
