"""enterpriseaiframework-e6f's `skills_catalog` and `prompt_prefix` params on
`chat_turn.build_payload`, checked against the actual dict it produces.

DELIBERATELY NOT IN test_chat_surface_version.py, which carries a module-level
`pytestmark = pytest.mark.usefixtures("stack_up")` — every test in that file requires the
bundle to be running. These do not: `build_payload` is a pure function with no I/O, and
requiring a live stack to check a dict's shape would make this suite's cheapest, fastest
claim depend on its most expensive precondition for no reason.

These are pure wire-shape claims (what LibreChat's server reads off `req.body`), not
claims about model behaviour — the live behavioural claim (does the model actually reach
for the right tool) is tests-live/test_tool_selection.py, which needs a real model for
exactly the reason fakeprovider tests elsewhere give: a non-reasoning stub cannot exercise
"the model decides." This file is the cheap, hermetic half: that the payload sent to make
that decision possible is shaped the way its own docstring says it is.
"""

import chat_turn


def test_skills_catalog_sets_the_toggle_with_no_forced_names():
    """The automatic-discovery shape: `ephemeralAgent.skills = True`, no `manualSkills`
    key at all — distinct from `manual_skills`, which also sets `manualSkills`."""
    body = chat_turn.build_payload(
        "hi", "m", "Enterprise AI", skills_catalog=True,
    )
    assert body["ephemeralAgent"]["skills"] is True
    assert "manualSkills" not in body


def test_manual_skills_wins_over_skills_catalog_without_double_setting_anything():
    body = chat_turn.build_payload(
        "hi", "m", "Enterprise AI",
        manual_skills=["incident-escalation"], skills_catalog=True,
    )
    assert body["ephemeralAgent"]["skills"] is True
    assert body["manualSkills"] == ["incident-escalation"]


def test_no_skills_flag_at_all_sets_neither():
    body = chat_turn.build_payload("hi", "m", "Enterprise AI")
    assert "ephemeralAgent" not in body
    assert "manualSkills" not in body


def test_prompt_prefix_sets_the_top_level_field_librechat_reads():
    """`packages/api/src/agents/load.ts#loadEphemeralAgent` reads `req.body?.promptPrefix`
    directly — a TOP-LEVEL key, not nested under `ephemeralAgent`. A test that put it in
    the wrong place would send a payload the server silently ignores while looking correct
    in every other respect."""
    body = chat_turn.build_payload(
        "hi", "m", "Enterprise AI", prompt_prefix="decide for yourself",
    )
    assert body["promptPrefix"] == "decide for yourself"


def test_no_prompt_prefix_means_the_key_is_entirely_absent_not_empty():
    """Distinguishing 'no system prompt' from 'an empty one' matters: `loadEphemeralAgent`
    treats a present-but-empty string as a real (empty) instructions override, same as any
    other string. `prompt_prefix=None` (the default) must omit the key outright."""
    body = chat_turn.build_payload("hi", "m", "Enterprise AI")
    assert "promptPrefix" not in body


def test_send_turn_forwards_both_new_params_to_build_payload(monkeypatch):
    """`send_turn` is the entry point every caller (including the live test) actually
    uses — a regression that dropped these two kwargs on their way through send_turn
    would be invisible to the tests above, which call build_payload directly."""
    captured = {}

    def fake_build_payload(*args, **kwargs):
        captured.update(kwargs)
        return {"endpoint": "Enterprise AI"}

    monkeypatch.setattr(chat_turn, "build_payload", fake_build_payload)
    monkeypatch.setattr(
        chat_turn, "start_turn", lambda *a, **k: ("conv-1", None)
    )
    monkeypatch.setattr(
        chat_turn, "wait_for_reply", lambda *a, **k: {"text": "ok"}
    )

    chat_turn.send_turn(
        client=None, chat_url="http://chat.invalid", text="hi", model="m",
        endpoint="Enterprise AI", skills_catalog=True, prompt_prefix="be autonomous",
    )
    assert captured["skills_catalog"] is True
    assert captured["prompt_prefix"] == "be autonomous"
