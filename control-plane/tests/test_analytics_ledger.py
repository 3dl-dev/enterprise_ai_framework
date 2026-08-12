"""The ledger join — real billed cost stamped onto sessions, pinned.

The pure attribution (window a session from its turns, bucket spend rows by alias + time) is
tested here; the asyncpg fetch is live-only (tests-live), the same split metering.py uses.
The end-to-end asserts the product's whole point: after the join, cost-per-edit is a REAL
number from the bill, and a session the ledger cannot see reads "unknown", never $0.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import ledger, metrics  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "analytics")


def _jsonl(name):
    with open(os.path.join(FIX, name)) as f:
        return [json.loads(line) for line in f if line.strip()]


def _mk_turn(sess, ts, principal="baron", surface="terminal"):
    return {"k": "turn", "sess": sess, "ts": ts, "principal": principal, "surface": surface}


def _mk_sess(sess, principal="baron", surface="terminal"):
    return {"k": "session", "sess": sess, "principal": principal, "surface": surface,
            "model": "m", "code": {"edits": 1}}


def test_session_windows_alias_and_span():
    turns = [
        _mk_turn("A", "2026-08-01T10:00:00+00:00"),
        _mk_turn("A", "2026-08-01T10:05:00+00:00"),
    ]
    w = ledger.session_windows(turns)
    assert w["A"]["alias"] == "baron::terminal"
    assert w["A"]["start"].isoformat() == "2026-08-01T10:00:00+00:00"
    assert w["A"]["end"].isoformat() == "2026-08-01T10:05:00+00:00"


def test_attribute_costs_buckets_by_alias_and_window():
    turns = [
        _mk_turn("A", "2026-08-01T10:00:00+00:00"),
        _mk_turn("A", "2026-08-01T10:05:00+00:00"),
        _mk_turn("B", "2026-08-01T11:00:00+00:00"),
    ]
    sessions = [_mk_sess("A"), _mk_sess("B"), _mk_sess("C")]  # C has no turns
    rows = [
        {"alias": "baron::terminal", "startTime": "2026-08-01T10:02:00Z", "spend": 0.10, "total_tokens": 1000},
        {"alias": "baron::terminal", "startTime": "2026-08-01T11:00:10Z", "spend": 0.05, "total_tokens": 500},   # within 15s skew of B
        {"alias": "baron::terminal", "startTime": "2026-08-01T09:00:00Z", "spend": 0.99, "total_tokens": 9},     # before any window
        {"alias": "claire::terminal", "startTime": "2026-08-01T10:03:00Z", "spend": 7.0, "total_tokens": 7},     # other principal
    ]
    out = {s["sess"]: s["cost"] for s in ledger.attribute_costs(sessions, turns, rows)}

    assert out["A"]["source"] == "ledger"
    assert out["A"]["ledger_spend_usd"] == 0.10 and out["A"]["tokens"] == 1000
    assert out["B"]["ledger_spend_usd"] == 0.05
    # C never ran, and the 09:00 / claire rows matched nothing -> C is unknown, not $0.
    assert out["C"] == {"source": "none"}


def test_no_matching_rows_is_unknown_never_zero():
    turns = [_mk_turn("A", "2026-08-01T10:00:00+00:00")]
    out = ledger.attribute_costs([_mk_sess("A")], turns, [])
    assert out[0]["cost"] == {"source": "none"}


def test_costedit_is_real_after_join_end_to_end():
    recs = _jsonl("opencode_expected.jsonl")
    turns = [r for r in recs if r["k"] == "turn"]
    sess = [r for r in recs if r["k"] == "session"]
    # one real spend row inside ses_top's window (16:00:00..16:00:04), alias baron::terminal
    rows = [{"alias": "baron::terminal", "startTime": "2026-08-01T16:00:03Z",
             "spend": 0.40, "total_tokens": 2000}]
    priced = ledger.attribute_costs(sess, turns, rows)
    assert priced[0]["cost"]["source"] == "ledger"

    m = metrics.build_metrics(turns, priced, min_n=1)
    costedit = next(r for r in m["code"] if r["key"] == "costedit")["v"]
    # $0.40 billed / 4 edits = $0.10 per edit — a real number, not an estimate.
    assert costedit["glm-5.2"] == 0.1
