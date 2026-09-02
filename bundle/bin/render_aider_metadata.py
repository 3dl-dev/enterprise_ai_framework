#!/usr/bin/env python3
"""Render deploy/workspace/model-metadata.json's context windows from the router catalog.

Aider resolves model metadata through litellm's own built-in table (see the file's own
`_comment`), which knows nothing about a model arriving as a generic OpenAI endpoint on
our gateway — without an entry aider silently assumes an 8k context and prices at zero,
which truncates the repo map on anything bigger. This is the render step that keeps that
table's context windows tracking the router catalog (item enterpriseaiframework-987,
design record C4/item -037): a model discovered at GET {catalog-url}/v1/models gets an
`openai/<id>` entry with its context window; a model no longer discovered keeps whatever
entry the file already had rather than losing it outright (see `render_model_metadata`).

Deliberately narrow. Two things stay OUT of this script and stay hand-curated in
deploy/workspace/model-settings.yml, on purpose:

  - edit_format / use_repo_map / lazy tuning — these are MEASURED per model against a
    real harness (tests-live/aider_editformat_probe.sh), not something a catalog id can
    tell you; a wrong guess fails SILENTLY (aider prints a plausible answer and leaves
    the file untouched — see model-settings.yml's own comment). Auto-generating this
    from a bare model id would be fabricating untested data. A model with no entry there
    just gets aider's own built-in default (`whole`), which is safe, not silent-broken.
  - the workspace's DEFAULT model (WORKSPACE_MODEL / --model in provision-workspace.sh)
    stays operator-set; item -037 tracks making that discovery-driven too.

Costs are deliberately always zero — the gateway is the meter, and a second, client-side
estimate that disagrees with the bill is worse than no estimate (same reasoning as the
file's own `_comment`).

Graceful like the other two renderers: an empty/unreachable catalog leaves the file
untouched, so this is never worse than the static table it replaces.
"""

from __future__ import annotations

import json
import sys
import urllib.request

DEFAULT_CONTEXT = 128000
DEFAULT_OUTPUT = 32768
PREFIX = "openai/"

# Always present regardless of catalog contents: the in-cluster fake provider used to
# smoke-test the workspace with no provider account and no spend (model-settings.yml's
# own comment). It is never in a real catalog, so it would otherwise be dropped by every
# re-render.
PINNED_ENTRIES = {
    "openai/fake-large": {
        "max_input_tokens": 32000,
        "max_output_tokens": 4096,
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
        "litellm_provider": "openai",
        "mode": "chat",
    },
}


def render_model_metadata(base: dict, models: list[dict]) -> dict:
    """Return model-metadata.json with its per-model entries regenerated from `models`.

    `models` are OpenRouter-parity objects (id, optional context_length). An empty list
    leaves `base` unchanged (the graceful fallback — keep whatever was baked). Non-model
    keys (`_comment`) and PINNED_ENTRIES are preserved/added regardless.
    """
    if not models:
        return base

    out: dict = {"_comment": base.get("_comment", [])}
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        out[f"{PREFIX}{mid}"] = {
            "max_input_tokens": m.get("context_length") or DEFAULT_CONTEXT,
            "max_output_tokens": DEFAULT_OUTPUT,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
            "litellm_provider": "openai",
            "mode": "chat",
        }
    if len(out) == 1:  # every model in the catalog was missing an id
        return base
    out.update(PINNED_ENTRIES)
    return out


def _fetch_models(base_url: str) -> list[dict]:
    url = base_url.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp).get("data", [])
    except Exception as exc:  # noqa: BLE001 — degrade to the baked table, never crash a build
        print(f"warning: catalog fetch failed ({exc}); keeping baked model-metadata.json", file=sys.stderr)
        return []


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: render_aider_metadata.py <model-metadata.json> <catalog-base-url>", file=sys.stderr)
        return 2
    path, base_url = argv[1], argv[2]
    with open(path) as f:
        base = json.load(f)
    rendered = render_model_metadata(base, _fetch_models(base_url))
    with open(path, "w") as f:
        json.dump(rendered, f, indent=2)
        f.write("\n")
    n = len(rendered) - (1 if "_comment" in rendered else 0)
    print(f"rendered {path}: {n} model entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
