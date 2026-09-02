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

import yaml

REPO = Path(__file__).resolve().parents[1]
CADDYFILE = REPO / "deploy" / "caddy" / "Caddyfile"
FREEROUTER_MANIFEST = REPO / "deploy" / "k8s" / "31-freerouter.yaml"
COMPOSE = REPO / "bundle" / "docker-compose.yml"


def _freerouter_container_env():
    """The freerouter Deployment container's env list, from the manifest."""
    for doc in yaml.safe_load_all(FREEROUTER_MANIFEST.read_text()):
        if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "freerouter":
            return doc["spec"]["template"]["spec"]["containers"][0]["env"]
    raise AssertionError("no freerouter Deployment in 31-freerouter.yaml")


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


def test_mainnet_settlement_key_is_secret_wired_never_a_manifest_literal():
    """A real-value mainnet settlement key must arrive from a Key Vault secret, never a
    committed manifest literal (item enterpriseaiframework-f06). If a future edit ever pasted
    FREEROUTER_PEER_WALLET_KEY_HEX as a plain `value:`, a spendable private key would be in
    git — the exact custody failure the injected-secret path exists to prevent."""
    env = {e["name"]: e for e in _freerouter_container_env()}
    for key in ("FREEROUTER_PEER_WALLET_KEY_HEX", "FREEROUTER_PEER_MAINNET_ENABLED"):
        assert key in env, f"{key} is not wired in 31-freerouter.yaml — mainnet custody path missing"
        entry = env[key]
        assert "value" not in entry, (
            f"{key} is a plain manifest literal — a mainnet settlement secret must come from "
            "secretKeyRef (enterprise-ai-secrets), never be committed in the manifest (f06 / bd6)."
        )
        ref = entry.get("valueFrom", {}).get("secretKeyRef", {})
        assert ref.get("name") == "enterprise-ai-secrets", (
            f"{key} must be sourced from the enterprise-ai-secrets secret, got {ref!r}"
        )
        assert ref.get("optional") is True, (
            f"{key} must be optional so the shipped base stays a testnet-only air-gap gateway "
            "with no mainnet wiring present (guardrail bd6)."
        )
