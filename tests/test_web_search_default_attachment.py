"""enterpriseaiframework-222: a brand-new conversation must actually be given the
web_search tool, not just told to use one.

DELIBERATELY NOT IN test_chat_surface_version.py, which carries a module-level
`pytestmark = pytest.mark.usefixtures("stack_up")` — every test there requires the bundle
to be running. This is a pure config-shape check (parsing committed YAML), same reasoning
as test_chat_turn_tool_selection_payload.py: it should not depend on a live stack to catch
a config regression that needs no live stack to detect.

THE BUG THIS GUARDS. Measured on the live cluster 2026-07-31: a never-signed-in account's
first turn terminated having emitted the web_search tool call as literal TEXT
(':::tool\\n{"name": "web_search", ...}') rather than a real tool invocation — no error, no
grounded answer, `unfinished:false`. Root cause: the deployed model spec never attached the
web_search tool to a first turn, while `librechat.yaml`'s shared `promptPrefix`
unconditionally instructs the model: "Search the web when the question turns on something
that could have changed since your training...". Told to use a tool it does not have, the
model fabricated a plausible-looking tool-call block in prose.

THE PLACEMENT MATTERS AND WAS WRONG TWICE ALREADY, both times silently. Two earlier fix
attempts both parsed without any error and both did nothing:

  1. `preset.webSearch: true` (camelCase, nested under the modelSpec's `preset:`) —
     copied the placement of `executeCode`, which was already (also silently) wrong.
  2. `preset.web_search: true` (snake_case, nested under `preset:`) — DOES survive the
     pinned v0.8.7 image's config-parsing schema and DOES reach the authenticated
     `/api/config` response (verified by reading it back), but still in the wrong
     PLACE.

Verified directly against the running v0.8.7 container's own bundled client JS
(`client/dist/assets/hooks.*.js`): the function that computes a brand-new conversation's
`ephemeralAgent.web_search` (LibreChat's `applyModelSpecEphemeralAgent`, minified as
`dk()` in this build) reads `modelSpec.webSearch` — a TOP-LEVEL property of the modelSpec
object, sibling to `preset`, name, and default — never `modelSpec.preset.webSearch` or
`.preset.web_search`. Both earlier attempts reached the browser but never reached
`ephemeralAgent`, because they were one YAML level too deep.

This test cannot exercise the model, the client, or the served config (that needs a live
cluster; see tests-live/test_first_conversation.py, which additionally asserts the
persisted message carries a REAL tool_call block and not this leaked-text shape). What it
CAN check, cheaply and on every run, is the config-shape fact that's actually load-bearing:
`webSearch: true` must sit at the TOP LEVEL of the modelSpec, not nested under `preset`,
on every spec whose promptPrefix promises web search.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
LIBRECHAT_YAML = REPO / "bundle" / "librechat" / "librechat.yaml"


def _model_specs() -> list[dict]:
    config = yaml.safe_load(LIBRECHAT_YAML.read_text())
    specs = config.get("modelSpecs", {}).get("list", [])
    assert specs, "librechat.yaml has no modelSpecs.list entries — has the shape changed?"
    return specs


def test_every_model_spec_that_promises_web_search_actually_attaches_it():
    """`webSearch: true` must sit at the TOP LEVEL of any modelSpec whose `promptPrefix`
    tells the model it may search the web — otherwise the promise in the prompt and the
    tool the turn is actually given disagree, which is exactly
    enterpriseaiframework-222. `preset.promptPrefix` is checked (not a top-level
    promptPrefix — modelSpecs only carry it under preset)."""
    specs = _model_specs()
    offenders = []
    for spec in specs:
        prompt_prefix = (spec.get("preset") or {}).get("promptPrefix", "") or ""
        if "search the web" in prompt_prefix.lower():
            if spec.get("webSearch") is not True:
                offenders.append(spec.get("name"))
    assert not offenders, (
        f"model spec(s) {offenders!r} instruct the model to search the web in their "
        f"promptPrefix but do not set the TOP-LEVEL `webSearch: true` (not nested under "
        f"`preset:` — see this file's module docstring for why the nested placement is "
        f"a silent no-op on the pinned image) — a brand-new conversation on that spec "
        f"never attaches the web_search tool, so the model is told to use a tool it was "
        f"never given"
    )


def test_no_model_spec_nests_websearch_under_preset():
    """Regression guard for the exact failure this item's first two fix attempts
    produced: `preset.webSearch` or `preset.web_search` both parse without error and do
    nothing on the pinned image, because the client reads the TOP-LEVEL property. A
    future edit that reintroduces either nested spelling (e.g. copied from `executeCode`,
    which has the same defect and is intentionally left as-is — see dogfood-findings.md)
    must fail loudly here instead of silently doing nothing again."""
    specs = _model_specs()
    offenders = [
        s.get("name") for s in specs
        if "webSearch" in (s.get("preset") or {}) or "web_search" in (s.get("preset") or {})
    ]
    assert not offenders, (
        f"model spec(s) {offenders!r} set webSearch/web_search UNDER `preset:` — this "
        f"is read by nothing on the pinned image; the client reads the TOP-LEVEL "
        f"`webSearch` property of the modelSpec object, sibling to `preset`, not a key "
        f"inside it"
    )


def test_the_default_and_cheapest_specs_both_carry_top_level_webSearch_true():
    """Named explicitly, not only covered by the general rule above, because these two
    are the ones a real account actually lands on (`glm-5-2` is `default: true` /
    `prioritize: true`; `glm-4-7` is the other selectable spec) — this is the exact pair
    the live cluster measurement exercised."""
    specs = {s.get("name"): s for s in _model_specs()}
    for name in ("glm-5-2", "glm-4-7"):
        assert name in specs, f"expected modelSpec {name!r} in librechat.yaml — was it renamed?"
        assert specs[name].get("webSearch") is True, (
            f"modelSpec {name!r} does not set top-level webSearch: true — a first-ever "
            f"conversation on this spec would never get the web_search tool attached"
        )
