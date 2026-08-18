"""Metric aggregation — normalized records -> metrics.json, pinned.

The values here are computed by hand from the golden fixtures (3 opencode turns, 1 session
with a folded subagent) so a change in a metric definition shows up as a failing number, not
a silent drift in a customer's report. Also proves the grouping key is a parameter — the
same aggregator slices by model or by surface — which is what the slicing item (-2df) rides on.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import metrics  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "analytics")


def _jsonl(name):
    with open(os.path.join(FIX, name)) as f:
        return [json.loads(line) for line in f if line.strip()]


def _split(recs):
    return ([r for r in recs if r["k"] == "turn"], [r for r in recs if r["k"] == "session"])


def _row(arr, key):
    return next(r for r in arr if r["key"] == key)


def test_metrics_values_from_opencode_fixture():
    turns, sess = _split(_jsonl("opencode_expected.jsonl"))
    m = metrics.build_metrics(turns, sess, min_n=1)
    g = "glm-5.2"

    assert _row(m["corpus"], "turns")["v"][g] == 3
    assert _row(m["corpus"], "sessions")["v"][g] == 1

    orch = m["orch"]
    assert _row(orch, "endcold")["v"][g] == 0        # the one dispatch turn was not cold
    assert _row(orch, "postdisp")["v"][g] == 1       # it did one tool call after dispatch
    assert round(_row(orch, "dispatchrate")["v"][g], 1) == 33.3
    assert round(_row(orch, "premstop")["v"][g], 1) == 33.3  # one bare-nudge follow-up of 3

    code = m["code"]
    assert _row(code, "edits")["v"][g] == 4
    assert _row(code, "editfail")["v"][g] == 25.0    # 1 of 4 edits failed
    assert _row(code, "toolerr")["v"][g] == 12.5     # 1 error in 8 tool calls
    assert _row(code, "tests")["v"][g] == 25.0       # 1 test run per 4 edits
    assert _row(code, "delegation")["v"][g] == 25.0  # 1 of 4 edits by the subagent
    # cost per edit needs the ledger join (-0e90); with no cost on the records it is absent,
    # never an estimated stand-in.
    assert g not in _row(code, "costedit")["v"]

    assert m["index"]["members"] == ["persist", "code"]
    assert g in m["index"]["persist"] and g in m["index"]["code"]


def test_thin_groups_are_suppressed():
    turns, sess = _split(_jsonl("opencode_expected.jsonl"))
    m = metrics.build_metrics(turns, sess, min_n=25)  # fixture has only 3 turns
    assert m["meta"]["groups"] == []
    assert _row(m["corpus"], "turns")["v"] == {}


def test_grouping_key_is_a_parameter_and_index_ranks():
    # combine both surfaces; group by surface instead of model.
    recs = _jsonl("opencode_expected.jsonl") + _jsonl("librechat_expected.jsonl")
    turns, sess = _split(recs)
    m = metrics.build_metrics(turns, sess, key=lambda r: r["surface"], min_n=1)

    assert set(m["meta"]["groups"]) == {"terminal", "chat"}
    # terminal did real coding; chat did none -> terminal must not score below chat on code.
    ci = m["index"]["code"]
    assert ci["terminal"] >= ci["chat"]
    # both groups appear in every array's rows.
    assert {"terminal", "chat"} <= set(_row(m["orch"], "toolsturn")["v"])


def test_meta_flags_thin_groups_for_the_ui():
    turns, sess = _split(_jsonl("opencode_expected.jsonl"))
    m = metrics.build_metrics(turns, sess, min_n=1)
    # under 300 turns -> flagged thin so the report can warn rather than draw a false line.
    assert "glm-5.2" in m["meta"]["thin"]
    assert m["meta"]["turn_counts"]["glm-5.2"] == 3
