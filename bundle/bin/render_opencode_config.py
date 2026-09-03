#!/usr/bin/env python3
"""Render deploy/workspace/opencode.json's model list from the router catalog.

opencode's `@ai-sdk/openai-compatible` provider takes an EXPLICIT `models` map (with the
per-model context/output limits it shows in the picker), not runtime discovery — and the
file is baked into the workspace image. So "the coding surface's model list tracks the
router" (item enterpriseaiframework-75c, design record C2) is a render step, like
render-gateway-config.py is for LiteLLM: fetch GET /v1/models and regenerate the block.

The catalog source is the router the workspaces reach — the operator's freerouter, which
reflects its upstreams' models through federation. Everything in opencode.json other than
the model block (provider wrapper, instructions, skills, mcp, theme) is preserved verbatim.

Graceful: an empty/unreachable catalog leaves the template's baked models untouched, so the
coding surface is never worse than the static list it replaces.
"""

from __future__ import annotations

import json
import sys
import urllib.request

PROVIDER = "enterprise-ai"
DEFAULT_CONTEXT = 128000
OUTPUT_CAP = 32768  # matches the baked config; the gateway presents generic models and a
                    # short output cap keeps reasoning from eating the whole turn budget.


def _humanize(model_id: str) -> str:
    return model_id.split("@", 1)[0].replace("-", " ").replace("_", " ").title()


def render_opencode_config(
    template: dict, models: list[dict], *, provider: str = PROVIDER
) -> dict:
    """Return opencode.json with its provider `models` block generated from `models`.

    `models` are OpenRouter-parity objects (id, optional name/context_length). An empty
    list leaves the template unchanged (keeps its baked models — the graceful fallback).
    The top-level default `model` is preserved when it still exists in the new catalog,
    else it points at the first catalog model.
    """
    if not models:
        return template

    block: dict[str, dict] = {}
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        block[mid] = {
            "name": m.get("name") or _humanize(mid),
            "limit": {
                "context": m.get("context_length") or DEFAULT_CONTEXT,
                "output": OUTPUT_CAP,
            },
        }
    if not block:
        return template

    out = json.loads(json.dumps(template))  # deep copy, never mutate the caller's dict
    out.setdefault("provider", {}).setdefault(provider, {})["models"] = block

    current = out.get("model", "")
    bare = current.split("/", 1)[1] if current.startswith(f"{provider}/") else None
    if bare not in block:
        out["model"] = f"{provider}/{next(iter(block))}"
    return out


def _fetch_models(base_url: str) -> list[dict]:
    url = base_url.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp).get("data", [])
    except Exception as exc:  # noqa: BLE001 — degrade to the baked models, never crash a build
        print(f"warning: catalog fetch failed ({exc}); keeping baked models", file=sys.stderr)
        return []


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: render_opencode_config.py <opencode.json> <catalog-base-url>", file=sys.stderr)
        return 2
    path, base_url = argv[1], argv[2]
    with open(path) as f:
        template = json.load(f)
    rendered = render_opencode_config(template, _fetch_models(base_url))
    with open(path, "w") as f:
        json.dump(rendered, f, indent=2)
        f.write("\n")
    n = len(rendered.get("provider", {}).get(PROVIDER, {}).get("models", {}))
    print(f"rendered {path}: {n} models, default {rendered.get('model')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
