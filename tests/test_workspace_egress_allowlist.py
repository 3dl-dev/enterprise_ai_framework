"""The workspace pod's in-cluster egress is an explicit allowlist, and stays one.

enterpriseaiframework-784. The terminal agent needs the MCP servers chat uses, so
`deploy/k8s/60-workspace-common.yaml` gained one egress destination. That pod is a shell
the user controls, running code an agent wrote, holding a spendable virtual key — every
destination added to it is an authority grant, so the SHAPE of the grant is checked here
rather than trusted to review.

WHY SHAPE AND NOT JUST CONTENT. The obvious way to make the MCP server reachable is also
the one that breaks isolation: widen the destination to the namespace, or to the pod CIDR.
This cluster's CNI (kube-router) resolves a packet on the DESTINATION pod's ingress rules
WITHOUT consulting the source's egress — a fact already recorded in the manifest and
measured on the live cluster. So a destination broad enough to cover workspace pods
re-opens workspace-to-workspace traffic, which is one shell reaching another shell that
holds somebody else's spendable key, and it does it while the policy still LOOKS like it
forbids exactly that. Every one of those shapes is fault-injected through the same checker
below, so the check is proven to bite rather than merely written down.

WHAT THIS FILE CANNOT SEE. It is hermetic and has no cluster: it reads the checked-in YAML.
Two things it therefore cannot prove, both of which
`tests-live/test_workspace_isolation.py` measures where a cluster exists:

  - the live `workspace-isolation` policy matches this YAML;
  - the pod behaviourally reaches mcp-echo and behaviourally does NOT reach an
    in-namespace service that is absent from the list.

Measured by hand from inside ws-student's ttyd container on 2026-07-30, BEFORE the rule
this file pins was applied (raw TCP connect, 3s timeout):

    gateway:4000        OPEN
    mcp-echo:8080       ConnectionRefusedError
    fakeprovider:8080   ConnectionRefusedError
    control-plane:8000  ConnectionRefusedError
    identity:8080       ConnectionRefusedError
    postgres:5432       ConnectionRefusedError
    valkey:6379         ConnectionRefusedError

`gateway` is the load-bearing line: no NetworkPolicy in the namespace selects the gateway
pod, and workspace -> gateway is nevertheless OPEN. That is the measurement showing an
egress rule naming an unselected destination is sufficient under this CNI, and that the
ingress-side requirement applies to destinations a policy DOES select — i.e.
workspace -> workspace.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
NETPOL_YAML = REPO / "deploy/k8s/60-workspace-common.yaml"

# The label every workspace pod carries, and the one that must never appear on the far end
# of an egress rule or inside a destination broad enough to contain it.
WORKSPACE_LABEL = ("app.kubernetes.io/component", "workspace")

# k3s pod CIDR on this cluster. A workspace pod's address is in here, and so is every other
# workspace pod's.
POD_CIDR = ipaddress.ip_network("10.42.0.0/16")

def _sorted_ports(rule: dict) -> tuple:
    return tuple(sorted((p.get("protocol", "TCP"), p.get("port")) for p in rule.get("ports", [])))


def _in_cluster_destinations(spec: dict) -> set[tuple]:
    """(labels, ports) for every egress destination that selects pods, not IP ranges.

    A `to` entry with a podSelector or a namespaceSelector resolves to pods inside the
    cluster. An ipBlock entry does not and is handled by the CIDR checks below.
    """
    out = set()
    for rule in spec.get("egress", []):
        ports = _sorted_ports(rule)
        for to in rule.get("to", []):
            if "ipBlock" in to:
                continue
            labels = {}
            labels.update((to.get("podSelector") or {}).get("matchLabels") or {})
            labels.update((to.get("namespaceSelector") or {}).get("matchLabels") or {})
            out.add((tuple(sorted(labels.items())), ports))
    return out


def _egress_shape_problems(spec: dict) -> list[str]:
    """Every way the in-cluster egress list can be too broad. Pure, so it is injectable."""
    problems: list[str] = []

    for rule in spec.get("egress", []):
        for to in rule.get("to", []):
            if "ipBlock" in to:
                block = to["ipBlock"]
                net = ipaddress.ip_network(block["cidr"])
                excepts = [ipaddress.ip_network(c) for c in block.get("except", [])]
                overlaps = net.overlaps(POD_CIDR) and not any(
                    e.supernet_of(POD_CIDR) or e == POD_CIDR for e in excepts
                )
                if overlaps:
                    problems.append(
                        f"egress ipBlock {block['cidr']} reaches the pod CIDR {POD_CIDR} "
                        f"without excepting it — that is every other user's workspace, and "
                        f"by this CNI's behaviour a route to their shell"
                    )
                continue

            pod_sel = to.get("podSelector")
            ns_sel = to.get("namespaceSelector")

            if pod_sel is not None and not (pod_sel.get("matchLabels") or
                                            pod_sel.get("matchExpressions")):
                problems.append(
                    "an egress destination has an EMPTY podSelector, which selects every "
                    "pod in the namespace including every other workspace — name the "
                    "service instead"
                )
                continue

            if pod_sel is None and ns_sel is not None:
                problems.append(
                    f"an egress destination is a namespaceSelector with no podSelector "
                    f"({ns_sel.get('matchLabels')}), which selects a whole namespace — "
                    f"name the service instead"
                )
                continue

            labels = (pod_sel or {}).get("matchLabels") or {}
            if WORKSPACE_LABEL in labels.items():
                problems.append(
                    f"an egress destination selects workspace pods themselves ({labels}) — "
                    f"that is one user's shell reaching another user's shell"
                )

    return problems


def _ingress_problems(spec: dict) -> list[str]:
    """The case this item did NOT change, asserted rather than assumed.

    Every ingress rule must name its sources. An ingress rule with no `from` is the exact
    documented CNI trap: kube-router ACCEPTs on the destination's ingress and never
    consults the source's egress, so an unrestricted `from` silently grants every
    workspace a route to every other workspace's ports regardless of the egress section.
    """
    problems: list[str] = []
    for rule in spec.get("ingress", []):
        if not rule.get("from"):
            problems.append(
                f"an ingress rule has no `from` list (ports {rule.get('ports')}). Under "
                f"kube-router that ACCEPTs the packet on this pod's ingress without "
                f"consulting the source's egress, so it opens these ports to every other "
                f"workspace pod — a shell holding somebody else's spendable key"
            )
    return problems


@pytest.fixture(scope="module")
def netpol_spec() -> dict:
    spec = None
    for doc in yaml.safe_load_all(NETPOL_YAML.read_text()):
        if doc and doc.get("kind") == "NetworkPolicy":
            spec = doc["spec"]
    assert spec is not None, f"no NetworkPolicy in {NETPOL_YAML}"
    return spec


def test_the_workspace_can_egress_to_the_mcp_server(netpol_spec):
    """The grant this item makes. Red on the manifest as it stood at df2e84f.

    Named by mcp-echo's own `app` label and its own port — not by a shared
    `component: mcp-server` label, so that the next MCP server costs one reviewed line
    here instead of arriving by inheriting a label somebody set on a Deployment.
    """
    dests = _in_cluster_destinations(netpol_spec)
    assert ((("app", "mcp-echo"),), (("TCP", 8080),)) in dests, (
        "the workspace pod has no egress to mcp-echo:8080, so the terminal agent cannot "
        f"call the tools the chat surface has (enterpriseaiframework-784/-471). Got: {dests}"
    )


def test_the_in_cluster_egress_list_is_exactly_these_three_services(netpol_spec):
    """The allowlist, pinned whole.

    A fourth in-cluster destination must fail here. This is the "verifiable by reading a
    single thing" half of the guard — the reviewer reads one set literal instead of
    reasoning about which pods a selector happens to match today.
    """
    expected = {
        # DNS, in kube-system: pod label and namespace label are AND-ed in one `to` entry.
        ((("k8s-app", "kube-dns"), ("kubernetes.io/metadata.name", "kube-system")),
         (("TCP", 53), ("UDP", 53))),
        # The one route out for model traffic.
        ((("app", "gateway"),), (("TCP", 4000),)),
        # The tool servers chat uses. One line per server, deliberately.
        ((("app", "mcp-echo"),), (("TCP", 8080),)),
    }
    assert _in_cluster_destinations(netpol_spec) == expected, (
        "the workspace pod's in-cluster egress allowlist changed. Every entry is an "
        "authority grant to a shell the user controls holding a spendable key — if the "
        "change is intended, update this set in the same commit and say why in the "
        "manifest."
    )


def test_no_egress_destination_is_broader_than_a_named_service(netpol_spec):
    """The real policy, through the same checker the fault injection below drives."""
    problems = _egress_shape_problems(netpol_spec)
    assert not problems, "\n".join(problems)


def test_every_ingress_rule_still_names_its_sources(netpol_spec):
    """The case NOT changed by this item. Nothing here was touched, and it must stay true:
    the `from` lists are the only thing closing workspace-to-workspace traffic."""
    problems = _ingress_problems(netpol_spec)
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------- fault injection
#
# Each of these is a plausible way to make mcp-echo reachable, and each one also hands
# every workspace a route to every other workspace. They are fed through the same
# functions the real policy goes through, so a checker that had stopped working could not
# keep the tests above green.

WIDENED_TO_THE_NAMESPACE = {
    "egress": [
        {"to": [{"namespaceSelector":
                 {"matchLabels": {"kubernetes.io/metadata.name": "enterprise-ai"}}}],
         "ports": [{"protocol": "TCP", "port": 8080}]},
    ]
}

WIDENED_TO_EVERY_POD_IN_THE_NAMESPACE = {
    "egress": [
        {"to": [{"podSelector": {}}],
         "ports": [{"protocol": "TCP", "port": 8080}]},
    ]
}

WIDENED_TO_THE_POD_CIDR = {
    "egress": [
        {"to": [{"ipBlock": {"cidr": "10.42.0.0/16"}}],
         "ports": [{"protocol": "TCP", "port": 8080}]},
    ]
}

# The subtler one: the internet rule with the private-range exception dropped. It looks
# like "let the pod install packages" and is in fact a route to every pod in the cluster.
INTERNET_RULE_MISSING_THE_PRIVATE_EXCEPTIONS = {
    "egress": [
        {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["169.254.0.0/16"]}}]},
    ]
}

# And the one that names the right label on the wrong side.
POINTED_AT_WORKSPACE_PODS = {
    "egress": [
        {"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "workspace"}}}],
         "ports": [{"protocol": "TCP", "port": 7681}]},
    ]
}


@pytest.mark.parametrize(
    "label,spec,expect_in_message",
    [
        ("namespaceSelector", WIDENED_TO_THE_NAMESPACE, "whole namespace"),
        ("empty podSelector", WIDENED_TO_EVERY_POD_IN_THE_NAMESPACE, "EMPTY podSelector"),
        ("pod CIDR ipBlock", WIDENED_TO_THE_POD_CIDR, "pod CIDR"),
        ("internet rule without private excepts",
         INTERNET_RULE_MISSING_THE_PRIVATE_EXCEPTIONS, "pod CIDR"),
        ("aimed at workspace pods", POINTED_AT_WORKSPACE_PODS, "another user's shell"),
    ],
)
def test_the_checker_rejects_each_way_of_widening_the_grant(label, spec, expect_in_message):
    problems = _egress_shape_problems(spec)
    assert problems, f"the checker accepted {label}, which grants workspace-to-workspace egress"
    assert any(expect_in_message in p for p in problems), (
        f"{label} was rejected, but for the wrong reason: {problems}"
    )


def test_the_checker_accepts_the_narrow_form_it_is_supposed_to(netpol_spec):
    """The other half of the injection. A checker that complained about every input would
    pass all five cases above and prove nothing, so the narrow form must come back clean —
    including the real 0.0.0.0/0 internet rule, whose `except` list is what makes it
    acceptable."""
    narrow = {
        "egress": [
            {"to": [{"podSelector": {"matchLabels": {"app": "mcp-echo"}}}],
             "ports": [{"protocol": "TCP", "port": 8080}]},
            {"to": [{"ipBlock": {"cidr": "0.0.0.0/0",
                                 "except": ["10.0.0.0/8", "172.16.0.0/12",
                                            "192.168.0.0/16", "169.254.0.0/16"]}}]},
        ]
    }
    assert _egress_shape_problems(narrow) == []
    # And the real thing, so "clean" is not only true of a hand-built spec.
    assert _egress_shape_problems(netpol_spec) == []


def test_the_ingress_checker_catches_a_missing_from_list():
    """Fault injection for the case this item did not change.

    This is the defect the manifest's ingress comment records as having actually happened:
    "port 4180 is the front door, let anyone knock". It must still be caught, otherwise
    `test_every_ingress_rule_still_names_its_sources` is decoration.
    """
    open_front_door = {"ingress": [{"ports": [{"protocol": "TCP", "port": 7681}]}]}
    problems = _ingress_problems(open_front_door)
    assert problems and "no `from` list" in problems[0]

    empty_from = {"ingress": [{"from": [], "ports": [{"protocol": "TCP", "port": 7681}]}]}
    assert _ingress_problems(empty_from)

    # The real shape, which names its sources, must be clean.
    named = {"ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "control-plane"}}}],
                          "ports": [{"protocol": "TCP", "port": 7681}]}]}
    assert _ingress_problems(named) == []
