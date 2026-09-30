"""Hermetic pin of the :8642 ingress peers in deploy/k8s/66-agent-console-common.yaml
(enterpriseaiframework-1d09).

The live fence test (tests-live/test_hermes_api_server_isolation.py) measures the network; this
pins the policy DATA so any widening of who may reach the hermes API server fails at `make test`
without a cluster. The only admitted peer is the control-plane pod: no namespaceSelector, no
matchExpressions, no extra peers, no other rule that admits :8642 (or admits every port).
Expected value: a hand-written constant, not read from the file under test.
"""
from pathlib import Path

import yaml

POLICY = Path(__file__).resolve().parent.parent / "deploy/k8s/66-agent-console-common.yaml"
ONLY_PEER = [{"podSelector": {"matchLabels": {"app": "control-plane"}}}]


def peers_admitting_8642(path: Path) -> list:
    """Every ingress rule's `from` that admits :8642, or "ANY" (an absent `from` = everyone)."""
    doc = yaml.safe_load(path.read_text())
    assert doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "agent-console-isolation"
    found = []
    for rule in doc["spec"]["ingress"]:
        ports = rule.get("ports")
        if ports is None or any(p.get("port") == 8642 for p in ports):
            found.append(rule.get("from", "ANY"))
    return found


def test_8642_peers_are_exactly_the_control_plane():
    assert peers_admitting_8642(POLICY) == [ONLY_PEER]


ANCHOR = "            matchLabels: { app: control-plane }\n"


def _mutated(tmp_path, old: str, new: str) -> Path:
    text = POLICY.read_text()
    assert text.count(old) == 1, old
    p = tmp_path / "66.yaml"
    p.write_text(text.replace(old, new))
    return p


def test_pin_goes_red_on_part_of_widening(tmp_path):
    p = _mutated(tmp_path, ANCHOR, ANCHOR
                 + "        - podSelector:\n"
                 "            matchLabels: { app.kubernetes.io/part-of: enterprise-ai-framework }\n")
    assert peers_admitting_8642(p) != [ONLY_PEER]


def test_pin_goes_red_on_match_expressions_widening(tmp_path):
    p = _mutated(tmp_path, ANCHOR, ANCHOR + "            matchExpressions:\n"
                 "              - { key: nobody, operator: NotIn, values: [x] }\n")
    assert peers_admitting_8642(p) != [ONLY_PEER]


def test_pin_goes_red_on_namespace_selector(tmp_path):
    p = _mutated(tmp_path, ANCHOR, ANCHOR + "        - namespaceSelector: {}\n")
    assert peers_admitting_8642(p) != [ONLY_PEER]


def test_pin_goes_red_on_extra_rule_admitting_8642(tmp_path):
    p = tmp_path / "extra.yaml"
    p.write_text(POLICY.read_text()
                 + "    - from:\n        - podSelector: {}\n      ports:\n        - { protocol: TCP, port: 8642 }\n")
    assert peers_admitting_8642(p) != [ONLY_PEER]
