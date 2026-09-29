"""The per-agent Raven template against the namespace-wide policies that fence it.

Item enterpriseaiframework-f16. Three files must agree, and no single one of them shows the
disagreement: 69-agent-raven.template.yaml labels the pod, 68-raven-common.yaml selects
`type: raven` pods and grants them NO internet, and 63-agent-common.yaml (which DOES grant
internet to every agent) must not select them. A label typo in the template silently gives a
Raven the internet-egress policy. The expected selection is evaluated here from the
manifests' own selectors, with the real label-selector semantics (matchLabels + NotIn), so
there is no second copy of the rule to drift.
"""
from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parent.parent / "deploy" / "k8s"


def _template_pod_labels() -> dict:
    text = (K8S / "69-agent-raven.template.yaml").read_text()
    for ph, val in (("__USER__", "alice"), ("__NAME__", "rv"), ("__MODEL_SOURCE__", "integrated"),
                    ("__IMAGE__", "img"), ("__KEY_SECRET__", "s"), ("__CFGSUM__", "c"),
                    ("__KEYSUM__", "k")):
        text = text.replace(ph, val)
    dep = next(d for d in yaml.safe_load_all(text) if d and d["kind"] == "Deployment")
    return dep["spec"]["template"]["metadata"]["labels"]


def _policy(fname: str, name: str) -> dict:
    return next(d for d in yaml.safe_load_all((K8S / fname).read_text())
                if d and d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == name)


def _selects(selector: dict, labels: dict) -> bool:
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        have, op, vals = labels.get(expr["key"]), expr["operator"], expr.get("values") or []
        if op == "In" and have not in vals:
            return False
        if op == "NotIn" and have in vals:
            return False
        if op == "Exists" and expr["key"] not in labels:
            return False
        if op == "DoesNotExist" and expr["key"] in labels:
            return False
    return True


def test_a_raven_pod_is_selected_by_the_raven_policy_and_not_by_the_internet_granting_one():
    labels = _template_pod_labels()
    raven = _policy("68-raven-common.yaml", "raven-isolation")["spec"]["podSelector"]
    general = _policy("63-agent-common.yaml", "agent-isolation")["spec"]["podSelector"]
    assert _selects(raven, labels), f"raven-isolation does not select the template's pod: {labels}"
    assert not _selects(general, labels), (
        "agent-isolation (public-internet egress for hermes/openclaw) selects a raven pod: "
        f"NetworkPolicies are additive, so the raven would reach EverMind and the internet. {labels}")


def test_the_selector_evaluator_is_not_a_rubber_stamp():
    """Poison the real input, the pod label, two ways: the fence must react to both."""
    labels = _template_pod_labels()
    raven = _policy("68-raven-common.yaml", "raven-isolation")["spec"]["podSelector"]
    general = _policy("63-agent-common.yaml", "agent-isolation")["spec"]["podSelector"]
    for wrong in ("hermes", "Raven"):     # relabel to another type / a near-miss spelling
        bad = {**labels, "agent.enterprise-ai/type": wrong}
        assert not _selects(raven, bad) and _selects(general, bad), wrong
    missing = {k: v for k, v in labels.items() if k != "agent.enterprise-ai/type"}
    assert not _selects(raven, missing) and _selects(general, missing)


def test_the_console_port_the_template_publishes_is_admitted_from_the_control_plane_only():
    tmpl = (K8S / "69-agent-raven.template.yaml").read_text()
    svc = next(d for d in yaml.safe_load_all(tmpl.replace("__USER__", "a").replace("__NAME__", "b"))
               if d and d["kind"] == "Service")
    port = svc["spec"]["ports"][0]["port"]
    for fname, pname in (("66-agent-console-common.yaml", "agent-console-isolation"),
                         ("68-raven-common.yaml", "raven-isolation")):
        rules = _policy(fname, pname)["spec"]["ingress"]
        admitted = [r for r in rules
                    if any(p["port"] == port for p in r["ports"])]
        assert admitted, f"{pname} does not admit :{port}"
        for r in admitted:
            assert r["from"] == [{"podSelector": {"matchLabels": {"app": "control-plane"}}}], (
                f"{pname} admits :{port} from something other than the control-plane pod")
