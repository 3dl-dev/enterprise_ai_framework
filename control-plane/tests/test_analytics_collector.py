"""The collector's merge — refresh a surface without dropping the others.

The network/db reads are covered live; what is pinned here is the invariant that makes the
report resilient: a tick replaces only the surfaces it collected and keeps the rest, so one
source blinking never blanks the page. Written atomically so a reader never sees a half file.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import collector  # noqa: E402


def _write(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _read(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _rec(k, surface, sess):
    return {"k": k, "surface": surface, "sess": sess, "model": "m",
            **({"code": None} if k == "session" else {})}


def test_merge_replaces_only_collected_surfaces(tmp_path):
    store = str(tmp_path / "records.jsonl")
    _write(store, [
        _rec("turn", "chat", "c1"), _rec("session", "chat", "c1"),
        _rec("turn", "ide", "i1"), _rec("session", "ide", "i1"),
    ])
    # a tick that collected only chat, with a different chat session
    fresh = [_rec("turn", "chat", "c2"), _rec("session", "chat", "c2")]
    collector.merge_store(store, fresh, {"chat"})

    out = _read(store)
    chat_sess = {r["sess"] for r in out if r["surface"] == "chat"}
    ide_sess = {r["sess"] for r in out if r["surface"] == "ide"}
    assert chat_sess == {"c2"}          # old chat replaced
    assert ide_sess == {"i1"}           # ide untouched — the source that didn't run this tick


def test_merge_with_no_surfaces_keeps_everything(tmp_path):
    store = str(tmp_path / "records.jsonl")
    original = [_rec("turn", "ide", "i1"), _rec("session", "ide", "i1")]
    _write(store, original)
    collector.merge_store(store, [], set())      # nothing collected
    assert len(_read(store)) == len(original)


def test_merge_creates_store_when_absent(tmp_path):
    store = str(tmp_path / "sub" / "records.jsonl")   # dir does not exist yet
    n = collector.merge_store(store, [_rec("turn", "chat", "c1")], {"chat"})
    assert n == 1 and os.path.isfile(store)


def test_collect_once_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("ANALYTICS_COLLECT_ENABLED", "0")
    import asyncio
    assert asyncio.run(collector.collect_once()) == {"enabled": False}
