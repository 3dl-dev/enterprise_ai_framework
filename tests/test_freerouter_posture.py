"""Treasury-posture guard for the bundled freerouter spoke (item enterpriseaiframework-a2f).

The bundled freerouter runs with FREEROUTER_SIGNUP=open so the control plane can broker
tenant/key provisioning (item 757). That is safe ONLY because the spoke is reachable in the
cluster and on the LAN/tailnet NodePort — never from the internet. If a future edit ever
published the open-signup spoke through the internet-facing Caddy ingress, strangers could
sign up tenants against it; combined with any float-granting slip that is the door to
spending the operator's treasury (the failure mode behind freerouter-0d4).

The inference API IS published (enterpriseaiframework-1b4, approved 2026-09-29) so a user
can call it from off-platform with their portal-issued key — the key is the boundary there,
metered against the user's own sub-account. What stays sealed is everything else the spoke
serves: /api/v1/* (open signup, key minting, sub-accounts) and /v1/operator/* (treasury).
So the invariant is no longer "freerouter absent from the Caddyfile" but "the freerouter
NodePort is reachable ONLY through an exact-path inference allowlist". It is a pure file
check — no running bundle.
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


# The ONLY spoke paths the internet may reach. Exact paths — a wildcard such as /v1/* would
# also publish /v1/operator/*. Adding a path here is a posture decision, not a refactor.
PUBLIC_INFERENCE_PATHS = {
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/embeddings",
    "/v1/embeddings/models",  # the embedding-model listing (freerouter-095), same envelope
    "/v1/models",
    "/v1/audio/speech",  # streamed TTS, billed by characters against the key (d26)
    "/v1/audio/transcriptions",  # multipart STT, billed by seconds against the key (d26)
}

MATCHER_RE = re.compile(r"^\s*@inference\s+path\s+(.+?)\s*$", re.MULTILINE)
HANDLE_RE = re.compile(r"handle\s+@inference\s*\{\s*reverse_proxy\s+(\S+)\s*\}")
# The internet-facing origin blocks are the ones serving the portal.
ORIGIN_BLOCK_RE = re.compile(r"^(\S[^\n]*)\{\n(.*?)^\}", re.MULTILINE | re.DOTALL)


def _node_port() -> str:
    m = re.search(r"nodePort:\s*(\d+)", FREEROUTER_MANIFEST.read_text())
    assert m, "31-freerouter.yaml has no NodePort — update this guard if the surface changed"
    return m.group(1)


def test_freerouter_is_reached_only_through_the_inference_allowlist():
    """Every route to the spoke's NodePort is a `handle @inference` — nothing else proxies it."""
    caddy = CADDYFILE.read_text()
    port = _node_port()
    proxied = [line for line in caddy.splitlines()
               if "reverse_proxy" in line and f":{port}" in line]
    allowed = [up for up in HANDLE_RE.findall(caddy) if up.endswith(f":{port}")]
    assert proxied, "the inference API is not routed to freerouter at all (item 1b4)"
    assert len(proxied) == len(allowed), (
        f"freerouter's NodePort {port} is reverse-proxied outside `handle @inference` — "
        "that can publish open signup or the operator panels to the internet (a2f / 0d4)."
    )


def test_the_inference_allowlist_is_exact_paths_only():
    caddy = CADDYFILE.read_text()
    matchers = MATCHER_RE.findall(caddy)
    assert matchers, "no @inference matcher in the Caddyfile"
    for m in matchers:
        paths = set(m.split())
        assert paths == PUBLIC_INFERENCE_PATHS, (
            f"@inference publishes {sorted(paths)}; the allowlist is "
            f"{sorted(PUBLIC_INFERENCE_PATHS)}. A wildcard or extra path can expose "
            "/v1/operator/* or /api/v1/* — change PUBLIC_INFERENCE_PATHS deliberately if meant."
        )


def test_signup_keys_and_operator_routes_never_appear_in_the_ingress():
    # Directives only: the comments explaining the allowlist name these paths on purpose.
    caddy = "\n".join(line for line in CADDYFILE.read_text().splitlines()
                      if not line.lstrip().startswith("#"))
    for forbidden in ("/api/v1", "/v1/operator", "/v1/*", "signup", "subaccounts"):
        assert forbidden not in caddy, (
            f"{forbidden!r} appears in the internet-facing Caddyfile — the spoke's signup, "
            "key-minting and operator routes stay in-cluster / LAN only (a2f)."
        )


def test_every_internet_origin_block_serves_the_inference_api():
    """Both origin listeners (Funnel :8081 and the TLS hostname) must carry the route, or the
    base_url the portal shows works on one path to the box and 404s on the other."""
    origins = [body for _, body in ORIGIN_BLOCK_RE.findall(CADDYFILE.read_text())
               if "handle /portal/*" in body]
    assert len(origins) == 2, f"expected 2 portal-serving origin blocks, found {len(origins)}"
    for body in origins:
        assert "handle @inference" in body and "@inference path" in body


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
