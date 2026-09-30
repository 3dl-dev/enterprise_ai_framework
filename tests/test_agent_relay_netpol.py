"""Contract G's network half: the relay is the ONLY way from a Raven to another agent.

Item enterpriseaiframework-692 (agents-raven.md). Two policies make it so, and this file
reads both from deploy/k8s as shipped:

  * 68-raven-common.yaml (`raven-isolation`): a Raven pod's egress. Besides DNS and the two
    model routes (gateway:4000, freerouter:8080) its ONLY destination is the control plane
    on :8000, the relay's front door. No ipBlock, no namespace-wide selector, no agent pods.
  * 66-agent-console-common.yaml: the hermes API server :8642 (the relay's target) is
    admitted from the control-plane pod ONLY, so a Raven cannot reach it directly.

The expected allow-list is written out by hand from the design record, not read back from
the files. Each check is run against the real file AND against realistic drifts of it (the
way an edit would reach the cluster: a widened selector, a pod-CIDR ipBlock, a dropped type
selector, a new port) and must reject every drift. The live measurement of the same fences,
from a real Raven pod, is tests-live/test_agent_relay_live.py.
"""

import copy
from pathlib import Path

import pytest
import yaml

K8S = Path(__file__).resolve().parent.parent / "deploy" / "k8s"

# (destination selector, ports) a Raven pod may reach, by hand from agents-raven.md
# Contract E/G: DNS, the two model routes, and the control plane's :8000.
RAVEN_EGRESS = {
    (("kube-system", "k8s-app", "kube-dns"), (("TCP", 53), ("UDP", 53))),
    ((None, "app", "gateway"), (("TCP", 4000),)),
    ((None, "app", "freerouter"), (("TCP", 8080),)),
    ((None, "app", "control-plane"), (("TCP", 8000),)),
}


def _raven_policy() -> dict:
    return yaml.safe_load((K8S / "68-raven-common.yaml").read_text())


def _console_policy() -> dict:
    return yaml.safe_load((K8S / "66-agent-console-common.yaml").read_text())


def raven_egress_violations(doc: dict) -> list[str]:
    """Everything about a raven-isolation policy that lets a Raven reach more than
    RAVEN_EGRESS, or that stops it selecting Raven pods alone."""
    bad = []
    spec = doc["spec"]
    sel = spec.get("podSelector") or {}
    if sel.get("matchLabels") != {"app.kubernetes.io/component": "agent",
                                  "agent.enterprise-ai/type": "raven"} or sel.get("matchExpressions"):
        bad.append(f"podSelector is not exactly the raven agent pods: {sel}")
    if "Egress" not in spec.get("policyTypes", []):
        bad.append("the policy does not govern egress")
    seen = set()
    for rule in spec.get("egress") or []:
        tos, ports = rule.get("to"), rule.get("ports")
        if not tos:
            bad.append(f"an egress rule with no `to` reaches everything: {rule}")
            continue
        if not ports:
            bad.append(f"an egress rule with no ports reaches every port: {rule}")
            continue
        port_key = tuple(sorted((p.get("protocol", "TCP"), p["port"]) for p in ports))
        for to in tos:
            if "ipBlock" in to:
                bad.append(f"an ipBlock egress: {to}")
                continue
            ns = to.get("namespaceSelector")
            pod = (to.get("podSelector") or {}).get("matchLabels") or {}
            if ns is not None and ns.get("matchLabels") != {"kubernetes.io/metadata.name": "kube-system"}:
                bad.append(f"a namespace-wide egress: {to}")
                continue
            if len(pod) != 1 or to.get("podSelector", {}).get("matchExpressions"):
                bad.append(f"an egress not naming exactly one app: {to}")
                continue
            ((k, v),) = pod.items()
            key = (("kube-system" if ns else None, k, v), port_key)
            if key not in RAVEN_EGRESS:
                bad.append(f"an egress outside the allow-list: {key}")
            seen.add(key)
    missing = {k for k in RAVEN_EGRESS if k not in seen}
    if ((None, "app", "control-plane"), (("TCP", 8000),)) in missing:
        bad.append("the Raven cannot reach the relay (control-plane:8000)")
    return bad


def hermes_api_violations(doc: dict) -> list[str]:
    """Every way the agent console policy admits something other than the control-plane pod
    to :8642."""
    bad = []
    for rule in doc["spec"].get("ingress") or []:
        ports = [p["port"] for p in rule.get("ports") or []]
        if ports and 8642 not in ports:
            continue
        if rule.get("from") != [{"podSelector": {"matchLabels": {"app": "control-plane"}}}]:
            bad.append(f":8642 admitted from {rule.get('from')!r}")
    admitted = [p["port"] for r in doc["spec"].get("ingress") or [] for p in r.get("ports") or []]
    if 8642 not in admitted:
        bad.append("the control plane cannot reach the relay target :8642")
    return bad


def test_a_raven_may_egress_only_to_dns_the_model_routes_and_the_control_plane():
    assert raven_egress_violations(_raven_policy()) == []


def test_the_hermes_api_port_is_admitted_from_the_control_plane_only():
    assert hermes_api_violations(_console_policy()) == []


def _mutate(doc: dict, fn) -> dict:
    out = copy.deepcopy(doc)
    fn(out)
    return out


def _cp_rule(doc: dict) -> dict:
    return next(r for r in doc["spec"]["egress"]
                if r["to"][0].get("podSelector", {}).get("matchLabels") == {"app": "control-plane"})


RAVEN_DRIFTS = {
    "pod CIDR ipBlock added": lambda d: d["spec"]["egress"].append(
        {"to": [{"ipBlock": {"cidr": "10.42.0.0/16"}}]}),
    "control-plane rule widened to the namespace": lambda d: _cp_rule(d)["to"].__setitem__(
        0, {"namespaceSelector": {}}),
    "control-plane rule widened to every agent pod": lambda d: _cp_rule(d)["to"].append(
        {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "agent"}}}),
    "control-plane rule ports dropped": lambda d: _cp_rule(d).pop("ports"),
    "hermes API port added to the control-plane rule": lambda d: _cp_rule(d)["ports"].append(
        {"protocol": "TCP", "port": 8642}),
    "type selector dropped": lambda d: d["spec"]["podSelector"]["matchLabels"].pop(
        "agent.enterprise-ai/type"),
    "control-plane rule removed": lambda d: d["spec"]["egress"].remove(_cp_rule(d)),
    "egress policyType removed": lambda d: d["spec"].__setitem__("policyTypes", ["Ingress"]),
}


@pytest.mark.parametrize("drift", sorted(RAVEN_DRIFTS))
def test_every_realistic_drift_of_the_raven_egress_is_caught(drift):
    assert raven_egress_violations(_mutate(_raven_policy(), RAVEN_DRIFTS[drift])), drift


HERMES_DRIFTS = {
    "admitted from every agent pod": lambda d: d["spec"]["ingress"][0].__setitem__(
        "from", [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "agent"}}}]),
    "admitted from raven pods": lambda d: d["spec"]["ingress"][0]["from"].append(
        {"podSelector": {"matchLabels": {"agent.enterprise-ai/type": "raven"}}}),
    "from removed (everyone)": lambda d: d["spec"]["ingress"][0].pop("from"),
    "a second rule opening 8642 to the namespace": lambda d: d["spec"]["ingress"].append(
        {"from": [{"namespaceSelector": {}}], "ports": [{"protocol": "TCP", "port": 8642}]}),
}


@pytest.mark.parametrize("drift", sorted(HERMES_DRIFTS))
def test_every_realistic_drift_of_the_hermes_api_fence_is_caught(drift):
    assert hermes_api_violations(_mutate(_console_policy(), HERMES_DRIFTS[drift])), drift
