"""Adversarial transcripts — the shapes that broke naive extractors, pinned.

The practice-kit SKILL calls out several of these as mistakes that reversed a conclusion:
a turn with no attributable model, a model switch mid-turn, a session whose work is all in
subagents, and a resumed session that opens with the assistant. Each is a real record shape;
this fixture is where the normalizer's handling of them is nailed down.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import opencode  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "analytics")


def _recs():
    with open(os.path.join(FIX, "opencode_adversarial.json")) as f:
        raw = json.load(f)
    return opencode.normalize(raw, tenant="camp", principal="baron", surface="terminal")


def _turns_of(recs, sess):
    return [r for r in recs if r["k"] == "turn" and r["sess"] == sess]


def _sess_of(recs, sess):
    return next(r for r in recs if r["k"] == "session" and r["sess"] == sess)


def test_turn_with_no_model_is_dropped_but_session_survives():
    recs = _recs()
    assert _turns_of(recs, "ses_nomodel") == []          # unattributable turn dropped
    assert _sess_of(recs, "ses_nomodel")["model"] == "model-A"  # session still recorded


def test_model_switch_attributes_to_first_model():
    recs = _recs()
    turns = _turns_of(recs, "ses_switch")
    assert len(turns) == 1
    assert turns[0]["model"] == "model-A"   # first assistant model wins
    assert turns[0]["n_asst"] == 2          # both messages counted in the one turn


def test_subagent_heavy_session_folds_all_worker_coding():
    recs = _recs()
    hub_turn = _turns_of(recs, "ses_hub")[0]
    # three task dispatches, nothing after the last -> the turn ends cold.
    assert hub_turn["dispatch"] == 3 and hub_turn["post_disp"] == 0
    sess = _sess_of(recs, "ses_hub")
    assert sess["n_subagents"] == 3
    assert sess["code"]["edits"] == 3 and sess["code"]["worker_edits"] == 3


def test_assistant_first_resumed_session_is_measured():
    recs = _recs()
    turns = _turns_of(recs, "ses_asstfirst")
    assert len(turns) == 1
    assert turns[0]["model"] == "model-C" and turns[0]["user_chars"] == 0
    assert _sess_of(recs, "ses_asstfirst")["code"]["tests"] == 1  # ran pytest
