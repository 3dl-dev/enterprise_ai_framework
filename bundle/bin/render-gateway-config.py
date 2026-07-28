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

# How a fake catalogue entry is recognised: by who serves it.
FAKE_UPSTREAM = "fakeprovider:8080"


def _without_fakes(text: str) -> str:
    """Drop every catalogue entry served by the fake provider.

    Keyed on the UPSTREAM, not the model name, because the name is not a reliable signal:
    the base file also maps `claude-opus-5` to the fake provider, so the Anthropic-native
    inbound path has something to answer with when there is no provider account. In a
    cluster that has real models that entry is a trap — it is the only claude-opus-5 in
    the catalogue, so selecting it returns "ack <hex>" from a stub while looking like a
    frontier model. Removing by name would have left it exactly where it was.

    Done textually because the base file is a commented template, and round-tripping it
    through a YAML parser would discard the comments explaining every decision in it.
    """
    lines = text.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("- model_name:"):
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("- model_name:"):
                if lines[j].strip() == MARKER:
                    break
                j += 1
            block = "".join(lines[i:j])
            if FAKE_UPSTREAM not in block:
                out.append(block)
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


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

    base_text = BASE.read_text()

    # The generated entries authenticate with os.environ/FORGE_API_KEY at runtime. With
    # no key reachable, emitting them produces a catalog that advertises models and then
    # 401s on every call — worse than not offering them, because the failure surfaces at
    # request time instead of at configuration time. Cached catalog and prices do not
    # change this: they let us render *offline*, not *unauthenticated*.
    if not env.get("FORGE_API_KEY"):
        OUT.write_text(base_text.replace(MARKER + "\n", "").replace(MARKER, ""))
        if CATALOG_CACHE.exists():
            print(
                "WARNING: Forge is configured on this machine but no FORGE_API_KEY is\n"
                "         visible, so the catalog has been reduced to fakes only.\n"
                "         Emitting the models anyway would advertise them and then 401.\n"
                "         Run `direnv allow` (and `op signin` if needed), or use\n"
                "         `direnv exec . make up`.",
                file=sys.stderr,
            )
        else:
            print("no Forge credentials — generated fake-provider-only catalog")
        return 0

    catalog, pricing = load(offline, env)

    if not catalog:
        # No Forge configured at all: strip the marker and ship the fakes only, so the
        # bundle still comes up with no provider account (scope item 8).
        OUT.write_text(base_text.replace(MARKER + "\n", "").replace(MARKER, ""))
        if CATALOG_CACHE.exists():
            # Forge *was* configured on this machine before, so silently dropping every
            # real model would look like the upstream vanished. Almost always this is a
            # shell without direnv loaded.
            print(
                "WARNING: Forge was configured here previously but no credentials are\n"
                "         visible now, so the catalog has been reduced to fakes only.\n"
                "         Run `direnv allow` (and `op signin` if needed), or use\n"
                "         `direnv exec . make up`.",
                file=sys.stderr,
            )
        else:
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
    # Real models exist, so the fake upstreams come OUT.
    #
    # They are the no-provider-account fallback (scope item 8), never a supplement. Left
    # in alongside real models they are simply two more entries in the picker that answer
    # every prompt with "ack <hex>" — and worse, LibreChat's titleModel pointed at one,
    # so every conversation in the cluster was named by a stub. The chats were real; only
    # their titles came from the fake provider, which made a working history read as a
    # broken one. Fallback or supplement, not both.
    OUT.write_text(_without_fakes(base_text).replace(MARKER, block))

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
