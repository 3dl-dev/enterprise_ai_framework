"""Normalizer golden tests — the semantics of a turn, pinned per surface.

Three things are proven here:

  * the PURE normalizers turn real per-surface transcripts into the exact expected
    turn/session records (byte-stable golden files), with the load-bearing semantics
    (dispatch, work-after-last-dispatch, cold-stop nudge, interrupt, coding aggregate,
    subagent fold) asserted explicitly so a reviewer reads the meaning, not just a diff;
  * the opencode SQLite reader reconstructs the same raw shape from a db built in
    opencode 1.18.7's real schema — so the sqlite layer and the pure layer agree;
  * every emitted record is CONTENT-FREE: no transcript text survives as a value, which is
    the invariant that keeps the analytics tenant-safe (design record).

Fixtures under tests/fixtures/analytics/ are redacted, hand-authored, and small enough to
verify by eye. The expected files were frozen from the normalizer AFTER the semantics were
checked against the fixture by hand (see the assertions below), not snapshotted blind.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import librechat, opencode  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "analytics")


def _load_json(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def _load_jsonl(name):
    with open(os.path.join(FIX, name)) as f:
        return [json.loads(line) for line in f if line.strip()]


def _turns(recs):
    return [r for r in recs if r["k"] == "turn"]


def _session(recs):
    return next(r for r in recs if r["k"] == "session")


# --------------------------------------------------------------------- opencode


def test_opencode_matches_golden():
    raw = _load_json("opencode_raw.json")
    recs = opencode.normalize(raw, tenant="camp", principal="baron", surface="terminal")
    assert recs == _load_jsonl("opencode_expected.jsonl")


def test_opencode_turn_semantics():
    raw = _load_json("opencode_raw.json")
    turns = _turns(opencode.normalize(raw, tenant="camp", principal="baron"))
    assert len(turns) == 3  # child session emits no main-thread turn

    t1, t2, t3 = turns
    # turn 1 dispatched (a `task` part) and then did one more tool call — not a cold stop —
    # but the human's next message was a bare "continue", so it still handed back early.
    assert t1["dispatch"] == 1 and t1["post_disp"] == 1
    assert t1["nudge"] == 1 and t1["interrupted"] == 0
    assert t1["tc"] == {"bash": 2, "read": 2, "task": 1, "write": 1}
    assert t1["style"] is not None  # final message cleared the 200-char floor
    # turn 2 never dispatched; a short final message has no measurable style.
    assert t2["dispatch"] == 0 and t2["post_disp"] is None and t2["style"] is None
    # turn 3 was aborted mid-flight -> interrupted, and an aborted turn is never a nudge.
    assert t3["interrupted"] == 1 and t3["nudge"] == 0


def test_opencode_session_folds_in_subagent_coding():
    raw = _load_json("opencode_raw.json")
    sess = _session(opencode.normalize(raw, tenant="camp", principal="baron"))
    assert sess["n_subagents"] == 1
    assert sess["subagent_output_tokens"] == 30
    code = sess["code"]
    # 4 edits = 2 writes on the main thread + 1 failed edit + 1 write inside the subagent.
    assert code["edits"] == 4
    assert code["edit_failures"] == 1 and code["tool_errors"] == 1
    assert code["tests"] == 1 and code["commits"] == 1 and code["reverts"] == 0
    # parser.py written three times -> two of them are rework.
    assert code["rework"] == 2


def _build_opencode_db(path, raw_sessions):
    """Write the raw fixture into a SQLite db shaped like opencode 1.18.7's real store,
    so read_raw_sessions is exercised against the actual schema. Tokens are stored NESTED
    (opencode's on-disk shape); the reader is what flattens them."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, "
        "model TEXT, title TEXT, time_created INTEGER)"
    )
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)"
    )
    conn.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
        "time_created INTEGER, data TEXT)"
    )
    for s in raw_sessions:
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?)",
            (s["id"], "proj", s.get("parent_id"), s.get("model"), s.get("title"),
             s.get("time_created")),
        )
        for m in s["messages"]:
            t = m.get("tokens") or {}
            data = {
                "role": m.get("role"),
                "modelID": m.get("modelID"),
                "finish": m.get("finish"),
                "tokens": {
                    "input": t.get("input", 0), "output": t.get("output", 0),
                    "cache": {"read": t.get("cache_read", 0), "write": t.get("cache_write", 0)},
                },
            }
            conn.execute(
                "INSERT INTO message VALUES (?,?,?,?)",
                (m["id"], s["id"], m.get("time_created"), json.dumps(data)),
            )
            for idx, p in enumerate(m.get("parts", [])):
                conn.execute(
                    "INSERT INTO part VALUES (?,?,?,?,?)",
                    (f"{m['id']}_p{idx:03d}", m["id"], s["id"], m.get("time_created"),
                     json.dumps(p)),
                )
    conn.commit()
    conn.close()


def test_opencode_sqlite_reader_agrees_with_golden(tmp_path):
    raw = _load_json("opencode_raw.json")
    db = str(tmp_path / "opencode.db")
    _build_opencode_db(db, raw)

    read = opencode.read_raw_sessions(db)
    recs = opencode.normalize(read, tenant="camp", principal="baron", surface="terminal")
    assert recs == _load_jsonl("opencode_expected.jsonl")


# --------------------------------------------------------------------- librechat


def test_librechat_matches_golden():
    msgs = _load_json("librechat_messages.json")
    names = {"hexbaron": "baron", "hexclaire": "claire"}
    recs = librechat.normalize(msgs, tenant="camp", resolve_principal=lambda u: names.get(u, u))
    assert recs == _load_jsonl("librechat_expected.jsonl")


def test_librechat_prose_surface_semantics():
    msgs = _load_json("librechat_messages.json")
    names = {"hexbaron": "baron", "hexclaire": "claire"}
    recs = librechat.normalize(msgs, tenant="camp", resolve_principal=lambda u: names.get(u, u))
    # principal comes from the injected resolver, not the raw ObjectId.
    assert {r["principal"] for r in recs} == {"baron", "claire"}
    # chat is a prose surface: every session records code=null, never a false zero.
    for s in (r for r in recs if r["k"] == "session"):
        assert s["code"] is None
    t1 = _turns(recs)[0]
    assert t1["ends_q"] == 1 and t1["nudge"] == 1           # asked a question, got nudged
    assert t1["style"]["permission"] >= 1 and t1["style"]["hedge"] >= 1
    # the unfinished assistant message marks its turn interrupted.
    assert any(t["interrupted"] == 1 for t in _turns(recs))


# --------------------------------------------------------------------- invariant

_STRING_KEYS = {"k", "tenant", "surface", "sess", "principal", "model", "effort", "ts"}


def _assert_no_text(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                assert k in _STRING_KEYS, f"unexpected string at {path}.{k}: content may be leaking"
            _assert_no_text(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _assert_no_text(v, f"{path}[{i}]")


def test_records_are_content_free():
    """No transcript text survives as a value — the tenant-safety invariant. Only a fixed
    set of label fields may be strings; everything else is a count."""
    oc = opencode.normalize(_load_json("opencode_raw.json"), tenant="camp", principal="baron")
    lc = librechat.normalize(_load_json("librechat_messages.json"), tenant="camp")
    for rec in oc + lc:
        _assert_no_text(rec)
