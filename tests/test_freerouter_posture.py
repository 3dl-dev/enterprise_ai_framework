"""Treasury-posture guard for the bundled freerouter spoke (item enterpriseaiframework-a2f).

The bundled freerouter runs with FREEROUTER_SIGNUP=open so the control plane can broker
tenant/key provisioning (item 757). That is safe ONLY because the spoke is reachable in the
cluster and on the LAN/tailnet NodePort — never from the internet. If a future edit ever
published the open-signup spoke through the internet-facing Caddy ingress, strangers could
sign up tenants against it; combined with any float-granting slip that is the door to
spending the operator's treasury (the failure mode behind freerouter-0d4).

This test solders the invariant shut: the freerouter service and its NodePort must NOT
appear in the internet-facing Caddyfile. It is a pure file check — no running bundle.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CADDYFILE = REPO / "deploy" / "caddy" / "Caddyfile"
FREEROUTER_MANIFEST = REPO / "deploy" / "k8s" / "31-freerouter.yaml"
COMPOSE = REPO / "bundle" / "docker-compose.yml"


def test_freerouter_runs_with_open_signup_in_the_bundle():
    """Guard's premise: the spoke really does run signup=open (so exposure would matter)."""
    compose = COMPOSE.read_text()
    assert "FREEROUTER_SIGNUP: open" in compose, (
        "premise changed — if the bundled freerouter no longer runs signup=open, revisit "
        "this guard; it exists precisely because open signup must never meet the internet"
    )


def test_freerouter_is_not_published_through_the_internet_facing_caddy():
    caddy = CADDYFILE.read_text()
    assert "freerouter" not in caddy.lower(), (
        "the open-signup freerouter spoke is reverse-proxied in the internet-facing "
        "Caddyfile — that exposes tenant signup to the internet. Remove it: the spoke is "
        "in-cluster / LAN-NodePort only (guardrail enterpriseaiframework-a2f)."
    )


def test_freerouter_nodeport_is_not_reverse_proxied_to_the_internet():
    manifest = FREEROUTER_MANIFEST.read_text()
    m = re.search(r"nodePort:\s*(\d+)", manifest)
    assert m, "31-freerouter.yaml has no NodePort — update this guard if the surface changed"
    node_port = m.group(1)
    caddy = CADDYFILE.read_text()
    assert node_port not in caddy, (
        f"freerouter's NodePort {node_port} is routed in the internet-facing Caddyfile — "
        "the open-signup spoke must never be reachable from the internet (a2f / 0d4)."
    )
