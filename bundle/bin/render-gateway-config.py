#!/usr/bin/env python3
"""Render the gateway model catalog from Forge's live catalog and price list.

Writes `litellm/config.generated.yaml` from `litellm/config.base.yaml`, replacing the
`# @GENERATED_UPSTREAMS@` marker with one entry per usable Forge model.

Why generated rather than hand-written:

- Forge's docs are explicit that `GET /v1/models` is the source of truth, not a pinned
  list. A hardcoded catalog goes stale silently.
- **Every entry must carry a real price.** A model the gateway cannot price still serves
  traffic and still counts tokens, but records spend as $0 — so budgets never trip and
  the bill under-reports, with nothing anywhere reporting an error. We learned that the
  hard way on the fake provider; doing it against real money across 68 models would be
  considerably worse.

So a model is included only if Forge quotes a real per-token price for it. Anything
unpriced is excluded and listed, loudly. Excluding a model is visible and annoying;
including it at $0 is invisible and wrong.

Prices come from `/v1/pricing`, which is admin-gated. That is a setup-time read on the
operator's machine, not a runtime credential — the admin key is never written to .env and
never reaches a container. The response is cached so re-rendering does not require the
admin key again.

Usage:
    render-gateway-config.py [--offline]

    --offline   use the cached price list and catalog; do not call Forge
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent
BASE = BUNDLE / "litellm" / "config.base.yaml"
OUT = BUNDLE / "litellm" / "config.generated.yaml"
PRICE_CACHE = BUNDLE / "litellm" / "forge-pricing.cache.json"
CATALOG_CACHE = BUNDLE / "litellm" / "forge-catalog.cache.json"
MARKER = "# @GENERATED_UPSTREAMS@"

# Models Forge quotes at exactly zero are served from hardware the operator already owns,
# so their marginal token cost really is zero. That is a different thing from "we do not
# know the price", but our unpriced-model detector cannot tell them apart from the ledger
# alone — it would flag every local model forever. They are left out until the local-model
# story (own hardware, capex not per-token) is built properly.
ZERO_PRICE_IS_REAL_BUT_EXCLUDED = "served from owned hardware; zero marginal cost needs capex accounting, not a token price"


def read_env() -> dict:
    env_path = BUNDLE / ".env"
    out = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    out.update({k: v for k, v in os.environ.items() if k.startswith("FORGE_")})
    return out


def fetch(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def admin_key(env: dict) -> str | None:
    """Setup-time only. Prefer an explicit env var, else ask 1Password."""
    if env.get("FORGE_ADMIN_KEY"):
        return env["FORGE_ADMIN_KEY"]
    try:
        r = subprocess.run(
            ["op", "item", "get", "Forge / enterprise-ai-framework",
             "--vault", "3dl-ops", "--fields", "FORGE_ADMIN_KEY", "--reveal"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def load(offline: bool, env: dict):
    base_url = env.get("FORGE_BASE_URL", "").rstrip("/")
    api_key = env.get("FORGE_API_KEY", "")

    if offline or not (base_url and api_key):
        if not (CATALOG_CACHE.exists() and PRICE_CACHE.exists()):
            return None, None
        return (
            json.loads(CATALOG_CACHE.read_text())["data"],
            json.loads(PRICE_CACHE.read_text())["data"],
        )

    catalog = fetch(f"{base_url}/v1/models", api_key)
    CATALOG_CACHE.write_text(json.dumps(catalog, indent=2))

    ak = admin_key(env)
    if ak:
        try:
            pricing = fetch(f"{base_url}/v1/pricing", ak)
            PRICE_CACHE.write_text(json.dumps(pricing, indent=2))
        except urllib.error.HTTPError as exc:
            print(f"warning: could not read /v1/pricing ({exc.code}); using cache", file=sys.stderr)
            if not PRICE_CACHE.exists():
                return catalog["data"], None
            pricing = json.loads(PRICE_CACHE.read_text())
    elif PRICE_CACHE.exists():
        print("note: no admin key available; using cached price list", file=sys.stderr)
        pricing = json.loads(PRICE_CACHE.read_text())
    else:
        return catalog["data"], None

    return catalog["data"], pricing["data"]


def yaml_entry(model: dict, price: dict, base_url: str) -> str:
    """One LiteLLM model_list entry.

    Written as text rather than dumped from a YAML library so the bundle needs no
    third-party dependency to configure itself.
    """
    mid = model["id"]
    inp = price["input_per_mtok"] / 1_000_000
    out = price["output_per_mtok"] / 1_000_000
    lines = [
        f"  - model_name: {mid}",
        "    litellm_params:",
        # Forge is OpenAI-wire-compatible, so it is reached as a custom OpenAI provider.
        f"      model: openai/{mid}",
        f"      api_base: {base_url}/v1",
        "      api_key: os.environ/FORGE_API_KEY",
        f"      input_cost_per_token: {inp:.12f}",
        f"      output_cost_per_token: {out:.12f}",
        "      extra_headers:",
        # Attribution. Without it Forge records a token bill with no idea which part of
        # the system spent it.
        "        X-Forge-Project: enterprise-ai-framework",
        "    model_info:",
        f"      sovereignty: {model.get('sovereignty', 'unknown')}",
        f"      max_context_window: {model.get('max_context_window', 0)}",
        f"      upstream_path: {model.get('path', 'unknown')}",
    ]
    return "\n".join(lines)


def main(argv) -> int:
    offline = "--offline" in argv
    env = read_env()
    base_url = env.get("FORGE_BASE_URL", "https://forge.3dl.dev").rstrip("/")

    catalog, pricing = load(offline, env)
    base_text = BASE.read_text()

    if not catalog:
        # No Forge configured at all: strip the marker and ship the fakes only, so the
        # bundle still comes up with no provider account (scope item 8).
        OUT.write_text(base_text.replace(MARKER + "\n", "").replace(MARKER, ""))
        print("no Forge configuration found — generated fake-provider-only catalog")
        return 0

    if pricing is None:
        print(
            "error: Forge catalog is reachable but the price list is not.\n"
            "Refusing to add unpriced models: they would meter at $0, budgets would\n"
            "never trip, and the bill would silently under-report. Provide\n"
            "FORGE_ADMIN_KEY (setup-time only) or sign in to 1Password, then re-run.",
            file=sys.stderr,
        )
        return 1

    prices = {p["model_id"]: p for p in pricing}

    included, excluded_unpriced, excluded_zero = [], [], []
    for m in sorted(catalog, key=lambda x: x["id"]):
        p = prices.get(m["id"])
        if p is None:
            excluded_unpriced.append(m["id"])
        elif p["input_per_mtok"] == 0 and p["output_per_mtok"] == 0:
            excluded_zero.append(m["id"])
        else:
            included.append(yaml_entry(m, p, base_url))

    block = "\n".join(included) if included else ""
    OUT.write_text(base_text.replace(MARKER, block))

    print(f"gateway catalog: {len(included)} Forge models included, priced from /v1/pricing")
    if excluded_zero:
        print(f"\n  excluded, quoted at $0 ({ZERO_PRICE_IS_REAL_BUT_EXCLUDED}):")
        print("    " + ", ".join(excluded_zero))
    if excluded_unpriced:
        print(f"\n  excluded, NO PRICE QUOTED BY FORGE ({len(excluded_unpriced)}):")
        for i in range(0, len(excluded_unpriced), 4):
            print("    " + ", ".join(excluded_unpriced[i:i + 4]))
        print(
            "\n  These are unusable until Forge quotes a price. Including them would\n"
            "  meter every request at $0 — budgets would not apply and the bill would\n"
            "  under-report, with no error anywhere."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
