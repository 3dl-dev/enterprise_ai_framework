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
grounded answer, `unfinished:false`. Root cause, traced into LibreChat's own client source
(`client/src/utils/endpoints.ts#applyModelSpecEphemeralAgent`):

    ephemeralAgent.web_search = modelSpec.webSearch ?? false

For a genuinely new conversation (`convoId === Constants.NEW_CONVO`) there is no
per-conversation localStorage override to layer on top of that default, so a modelSpec
that never sets `webSearch: true` makes the tool's absence on a first turn
UNCONDITIONAL, not stochastic — deterministic for every never-signed-in account, on every
model spec that omits the key. Meanwhile `librechat.yaml`'s shared `promptPrefix`
unconditionally instructs the model: "Search the web when the question turns on something
that could have changed since your training..." — a real instruction pointed at a tool
that was never actually attached. Told to use a tool it does not have, the model
fabricated a plausible-looking tool-call block in prose instead of making a real one.

This test cannot exercise the model (that needs a live cluster; see
tests-live/test_first_conversation.py, which additionally asserts the persisted message
carries a REAL tool_call block and not this leaked-text shape). What it CAN check, cheaply
and on every run, is the half of the fix that is a pure config fact: every selectable
model spec whose promptPrefix promises web search must actually have the tool attached by
default. A future model spec that copies the promptPrefix paragraph without also setting
`webSearch: true` reintroduces exactly this bug, silently, the same way this one did.
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
    """`preset.webSearch` must be `true` on any modelSpec whose `promptPrefix` tells the
    model it may search the web — otherwise the promise in the prompt and the tool the
    turn is actually given disagree, which is exactly enterpriseaiframework-222."""
    specs = _model_specs()
    offenders = []
    for spec in specs:
        preset = spec.get("preset", {})
        prompt_prefix = preset.get("promptPrefix", "") or ""
        if "search the web" in prompt_prefix.lower():
            if preset.get("webSearch") is not True:
                offenders.append(spec.get("name"))
    assert not offenders, (
        f"model spec(s) {offenders!r} instruct the model to search the web in their "
        f"promptPrefix but do not set `preset.webSearch: true` — a brand-new "
        f"conversation on that spec never attaches the web_search tool "
        f"(client/src/utils/endpoints.ts#applyModelSpecEphemeralAgent: "
        f"`ephemeralAgent.web_search = modelSpec.webSearch ?? false`, and there is no "
        f"localStorage override yet for a conversation that has never been sent), so "
        f"the model is told to use a tool it was never given"
    )


def test_the_default_and_cheapest_specs_both_carry_webSearch_true():
    """Named explicitly, not only covered by the general rule above, because these two
    are the ones a real account actually lands on (`glm-5-2` is `default: true` /
    `prioritize: true`; `glm-4-7` is the other selectable spec) — this is the exact pair
    the live cluster measurement exercised."""
    specs = {s.get("name"): s for s in _model_specs()}
    for name in ("glm-5-2", "glm-4-7"):
        assert name in specs, f"expected modelSpec {name!r} in librechat.yaml — was it renamed?"
        preset = specs[name].get("preset", {})
        assert preset.get("webSearch") is True, (
            f"modelSpec {name!r} does not set preset.webSearch: true — a first-ever "
            f"conversation on this spec would never get the web_search tool attached"
        )
