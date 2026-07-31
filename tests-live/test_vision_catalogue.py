"""enterpriseaiframework-282's live-tier proof: a real, priced, vision-capable model is
rendered into the gateway catalogue and is selectable.

SCOPE, STATED PRECISELY — this file was named test_vision.py until the wave-3 rework and
that name overstated what it proves. Read this before trusting the filename.

282's done-condition is "at least one vision-capable model priced and selectable" — a
catalogue and pricing claim, not an image-comprehension claim. This file proves exactly
that: a vision-family model name (matching Forge's `vl|vision|pixtral` naming) is present
in the LOCAL catalogue this bundle renders from Forge's live `/v1/models`
(bundle/bin/render-gateway-config.py), answers an ordinary text turn through this gateway,
and is priced (not metered at $0 — see TestARealVisionModelIsInTheCatalogueAndPriced's own
docstring for why this is proven against the candidate's rendered config and its own
LiteLLM_SpendLogs row, not against /admin/unpriced's global aggregate; an unpriced model
silently under-reports the bill).

WHAT THIS FILE DOES NOT PROVE, AND WHY THAT ASSERTION LIVES ELSEWHERE

An earlier version of this file also asserted that the model correctly names the color of
a real image sent to it — the full round-trip claim. That assertion is TRUE IN SPEC but
FALSE IN THIS DEPLOYMENT today: measured directly against FORGE_BASE_URL, bypassing the
gateway, Forge's own request validator rejects `data:` image URLs for every vision model
in the catalogue ('invalid URL scheme "data": only http/https allowed'), which is the only
URL shape LibreChat's local file strategy emits (encode.js). The http(s) alternative fails
too — Bedrock demands inline base64, deepinfra cannot fetch a URL. So the real image path
400s regardless of which vision model is chosen.

That is a product defect, not a test defect. It is filed as enterpriseaiframework-e03
(gated for a founder decision between an S3/presigned file strategy and a Forge-side
fix), wired as a blocker of enterpriseaiframework-020 ("a user pastes a screenshot ...
and the model answers about its contents"), which is the item that actually owns the
round-trip claim. The full assertion — send a real image, assert the model names its
color — is recoverable from this file's git history at commit c8cb0a5 /
tests-live/test_vision.py (class TestTheVisionModelActuallySeesTheImage) and belongs back
in a live-tier test the moment e03 lands; 020 cannot close without re-adding it. It is not
re-added here, skipped or otherwise, because a test that can only fail until a founder
decision ships is not this item's proof and CLAUDE.md's "no skipped tests" rule means it
should not sit in this branch pretending to be one.

Requires the bundle's `make up` to have rendered its LOCAL gateway catalogue WITH a real
FORGE_API_KEY configured (`direnv exec . make up`, or `make forge-config` then `make up`)
— i.e. the same precondition every other test in this directory already has via
`tests-live/conftest.py`'s `env` fixture, which fails loudly rather than skipping when
Forge credentials are not configured.
"""

import re
import subprocess
import time
import uuid

import httpx
import pytest

from conftest import BUNDLE

TIMEOUT = 120.0

# Forge serves a whole vision-language family under different provider paths; matched
# by name here ONLY to pick a candidate to actually test against — never as the proof
# itself. The proof is the model being priced and answering a real turn below. Same
# family bundle/litellm/config.base.yaml's fake-vision-large entry documents.
_VISION_NAME_RE = re.compile(r"vl|vision|pixtral", re.IGNORECASE)


@pytest.fixture(scope="module")
def catalog_yaml() -> str:
    path = BUNDLE / "litellm" / "config.generated.yaml"
    if not path.exists():
        pytest.fail(f"{path} missing — run `make up` first")
    return path.read_text()


@pytest.fixture(scope="module")
def vision_model_candidate(catalog_yaml: str) -> str:
    names = re.findall(r"^\s*-\s*model_name:\s*(\S+)\s*$", catalog_yaml, re.MULTILINE)
    candidates = [n for n in names if _VISION_NAME_RE.search(n) and "fake" not in n]
    if not candidates:
        pytest.fail(
            "no vision-family model name (matching vl|vision|pixtral) is in the "
            "rendered catalogue — either Forge's catalogue changed or FORGE_API_KEY "
            "was not configured when `make up` last rendered it"
        )
    return sorted(candidates)[0]


def _spend_row_since(env: dict, model_group: str, since: str, timeout: float = 90.0):
    """Poll `LiteLLM_SpendLogs` for a row under `model_group` no older than `since`.

    Same shape as tests/test_scope_items.py's
    TestModelPickerAndReasoningEffort._spend_row_since, and for the same reason: it is
    the only way to answer "did THIS candidate's own turn actually land a priced row",
    rather than "is anything anywhere unpriced" (what /admin/unpriced answers).
    `model_group` carries the ALIAS a caller requested (litellm's `model_name`), which
    for every Forge entry `render-gateway-config.py` emits is the same string
    `litellm_params.model` resolves to once LiteLLM strips the `openai/` provider
    prefix it prepends for its own routing — so this column names exactly the
    catalogue entry a caller picked, real or fake.

    Polls rather than reads once: the gateway batches spend rows for 7-13s
    (flush_spend_on_shutdown.handler's own comment) before they land in postgres — the
    exact batching window that made the old /admin/unpriced poll pass on its first
    attempt, before any row had landed at all.

    Uses `docker compose` against the bundle's own compose file/env-file rather than a
    hardcoded project name, matching tests/conftest.py's `compose()` helper — this file
    stays self-contained (mock_scope names only this file) rather than importing across
    the hermetic/live boundary the module docstring deliberately keeps separate.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "compose", "-f", str(BUNDLE / "docker-compose.yml"),
             "--env-file", str(BUNDLE / ".env"), "exec", "-T", "postgres",
             "psql", "-U", env.get("POSTGRES_USER", "eai"), "-d", "gateway",
             "-tA", "-F", "|", "-c",
             "SELECT spend, prompt_tokens, completion_tokens FROM "
             f"\"LiteLLM_SpendLogs\" WHERE model_group = '{model_group}' "
             f"AND \"startTime\" >= '{since}'::text::timestamptz "
             "ORDER BY \"startTime\" DESC LIMIT 1"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"psql failed\n{result.stdout}\n{result.stderr}"
        line = result.stdout.strip()
        if line:
            spend_s, prompt_s, completion_s = line.split("|")
            return float(spend_s), int(prompt_s), int(completion_s)
        time.sleep(3)
    pytest.fail(
        f"no LiteLLM_SpendLogs row landed under model_group={model_group!r} since "
        f"{since} — the turn was either never sent under that model, or it silently "
        "sent something else"
    )


def _rendered_cost_per_token(catalog_yaml: str, model_name: str) -> tuple[float, float]:
    """The (input, output) `cost_per_token` this bundle actually renders for
    `model_name`, read straight off `config.generated.yaml` — no network call.

    bundle/bin/render-gateway-config.py writes `input_cost_per_token` /
    `output_cost_per_token` into both `litellm_params` and `model_info` for every entry
    it emits (see its `yaml_entry`); this is a static assertion on that output, not an
    inference from behaviour. It is intentionally paired below with a dynamic assertion
    on a real spend row rather than relied on alone: the rendered value proves the
    config *says* a price, not that the gateway actually *billed* it.
    """
    pattern = re.compile(
        rf"^\s*-\s*model_name:\s*{re.escape(model_name)}\s*$(.*?)"
        r"(?=^\s*-\s*model_name:|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(catalog_yaml)
    if not match:
        pytest.fail(f"{model_name!r} has no model_name block in the rendered catalogue")
    block = match.group(1)
    inputs = re.findall(r"input_cost_per_token:\s*([0-9.eE+-]+)", block)
    outputs = re.findall(r"output_cost_per_token:\s*([0-9.eE+-]+)", block)
    if not inputs or not outputs:
        pytest.fail(
            f"{model_name!r}'s rendered block carries no input_cost_per_token/"
            f"output_cost_per_token at all:\n{block}"
        )
    return float(inputs[0]), float(outputs[0])


class TestARealVisionModelIsInTheCatalogueAndPriced:
    """Proves enterpriseaiframework-282's claim only: catalogue membership + pricing.

    Does NOT send an image and does NOT claim the model can see one — see the module
    docstring for why that assertion lives with enterpriseaiframework-020/e03 instead.

    PRICED IS PROVEN TWO WAYS, NOT BY POLLING /admin/unpriced (wave-3 rework). That
    endpoint wraps metering.unpriced_models(), a GLOBAL aggregate over every model that
    served traffic anywhere in the deployment since `since` — it never reads the
    candidate's own price or the candidate's own spend, and LiteLLM batches spend rows
    for 7-13s (flush_spend_on_shutdown's own comment) before they land in postgres. A
    negative control proved the old loop passed at t+0.07s with NO turn ever sent under
    any model: the first poll already saw an empty (hence "ok") global unpriced set,
    because nothing anywhere had metered at zero yet, not because this candidate priced
    correctly. Replaced with:
      1. a STATIC assertion that the rendered config this gateway is actually running
         carries a nonzero cost_per_token for `vision_model_candidate` specifically
         (`_rendered_cost_per_token`, no network call, ground-truth for "the config
         prices this model" independent of any turn ever being sent), and
      2. a DYNAMIC assertion that a real turn against exactly that model produces a
         `LiteLLM_SpendLogs` row named `model_group = vision_model_candidate` with
         spend > 0 — the same shape tests/test_scope_items.py's
         TestModelPickerAndReasoningEffort._spend_row_since uses, scoped to the
         candidate rather than reading a global "is anything unpriced" flag.
    """

    def test_it_is_offered_and_priced(
        self, gateway_url, virtual_key, vision_model_candidate, catalog_yaml, env,
    ):
        input_cost, output_cost = _rendered_cost_per_token(
            catalog_yaml, vision_model_candidate
        )
        assert input_cost > 0 and output_cost > 0, (
            f"{vision_model_candidate!r} is rendered into the catalogue with "
            f"input_cost_per_token={input_cost}, output_cost_per_token={output_cost} — "
            "at least one is zero, so this model would meter for free and no budget "
            "could ever bind on it"
        )

        since = time.strftime("%Y-%m-%dT%H:%M:%S+00", time.gmtime(time.time() - 2))
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}"},
            json={
                "model": vision_model_candidate, "max_tokens": 10,
                "messages": [{"role": "user",
                              "content": f"reply with exactly: ok {uuid.uuid4().hex[:6]}"}],
            },
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, (
            f"{vision_model_candidate!r} is in the rendered catalogue but refused a "
            f"plain turn ({r.status_code}): {r.text[:300]}"
        )
        assert r.json()["usage"]["total_tokens"] > 0

        spend, prompt_tokens, completion_tokens = _spend_row_since(
            env, vision_model_candidate, since
        )
        assert prompt_tokens > 0 and completion_tokens > 0, (
            f"{vision_model_candidate!r}'s own ledger row recorded no tokens — the turn "
            "did not actually reach it"
        )
        assert spend > 0, (
            f"{vision_model_candidate!r} metered at $0 on its OWN ledger row — an "
            "unpriced model meters at $0, so budgets never trip and the bill "
            "under-reports"
        )
