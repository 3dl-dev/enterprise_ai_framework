"""opencode SQLite -> normalized turn/session records (terminal / ide surfaces).

Two layers, split so the semantics can be golden-tested without a database:

  read_raw_sessions(db_path)   the SQLite layer: opencode's session/message/part tables
                               (opencode 1.18.7 schema) into plain JSON-able dicts.
  normalize(raw_sessions, ...) PURE: raw dicts -> records. This is what the fixtures pin.

opencode's schema (see docs/design/records/behavioral-analytics-sources.md): a `session`
row per conversation (`parent_id` links a subagent's child session to its parent); a
`message` row per message with a JSON `data` blob (role, modelID, tokens, finish); a `part`
row per content block with a JSON `data` blob typed text/reasoning/tool/patch/step-*.

Divergences from the practice kit's Claude Code reader, handled here:
  * subagents are child SESSIONS (`parent_id`), not `Agent` tool calls. Turn records are
    emitted for the top-level session only; child sessions fold into that session's coding
    aggregate and subagent census.
  * dispatch inside a turn is the `task` tool part (schema.DISPATCH), reconciled with the
    child-session census at the session level.
  * an edit is an edit/write TOOL part — carrying the written content, path and status —
    NOT the `patch` content part, which opencode emits sparsely and not one-per-edit (so
    counting it double-counts or under-counts). The spike record assumed `patch` was
    authoritative; the live db showed otherwise. Noted on item -1a8.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from .schema import EDIT_TOOLS, CodeAggregate, TurnAccumulator, session_record

# opencode assistant `finish` values that mean the model was cut off mid-turn.
_ABORTED = {"aborted", "cancelled", "canceled", "interrupted"}


def _iso(ms: int | None) -> str | None:
    """opencode stores epoch MILLISECONDS. Render ISO-8601 UTC for the record."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


# ------------------------------------------------------------------ the SQLite layer


def read_raw_sessions(db_path: str) -> list[dict]:
    """Read one opencode db into raw session dicts. Opens READ-ONLY over a live db.

    The db is WAL-mode and live (a user may be mid-session), so it is opened immutable —
    we never take a write lock on a store the user is using. Messages and their parts come
    back in wall-clock order, which is the order a turn is walked in.
    """
    uri = f"file:{db_path}?immutable=1&mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        sessions = [
            {
                "id": r["id"],
                "parent_id": r["parent_id"],
                "model": r["model"],
                "title": r["title"],
                "time_created": r["time_created"],
                "messages": [],
            }
            for r in conn.execute(
                "SELECT id, parent_id, model, title, time_created FROM session"
            )
        ]
        by_id = {s["id"]: s for s in sessions}

        msg_index: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT id, session_id, time_created, data FROM message "
            "ORDER BY session_id, time_created, id"
        ):
            sess = by_id.get(r["session_id"])
            if sess is None:
                continue
            data = _loads(r["data"])
            msg = {
                "id": r["id"],
                "role": data.get("role"),
                "modelID": data.get("modelID"),
                "effort": _effort_of(data),
                "finish": data.get("finish"),
                "tokens": _tokens_of(data),
                "time_created": r["time_created"],
                "parts": [],
            }
            sess["messages"].append(msg)
            msg_index[r["id"]] = msg

        for r in conn.execute(
            "SELECT message_id, time_created, data FROM part ORDER BY message_id, time_created, id"
        ):
            msg = msg_index.get(r["message_id"])
            if msg is not None:
                msg["parts"].append(_loads(r["data"]))
    finally:
        conn.close()
    return sessions


def _loads(text: str | None) -> dict:
    try:
        d = json.loads(text or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _tokens_of(data: dict) -> dict:
    t = data.get("tokens") or {}
    cache = t.get("cache") or {}
    return {
        "input": t.get("input") or 0,
        "output": t.get("output") or 0,
        "cache_write": cache.get("write") or 0,
        "cache_read": cache.get("read") or 0,
    }


def _effort_of(data: dict) -> str | None:
    # opencode carries reasoning effort in a few shapes across versions; check the likely
    # spots without inventing one. A confound the report filters on, per the SKILL.
    for key in ("effort", "reasoningEffort", "reasoning_effort"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return v
    opts = data.get("options") or data.get("providerOptions") or {}
    if isinstance(opts, dict):
        v = opts.get("reasoningEffort") or opts.get("effort")
        if isinstance(v, str) and v:
            return v
    return None


# ------------------------------------------------------------------ the pure normalizer


def normalize(
    raw_sessions: list[dict],
    *,
    tenant: str,
    principal: str,
    surface: str = "terminal",
) -> list[dict]:
    """Raw opencode sessions -> turn + session records. Pure; golden-testable.

    `surface` is the base surface these sessions belong to (terminal or ide). One db is one
    user's workspace, so `principal` is constant across it.
    """
    children: dict[str, list[dict]] = defaultdict(list)
    tops: list[dict] = []
    for s in raw_sessions:
        if s.get("parent_id"):
            children[s["parent_id"]].append(s)
        else:
            tops.append(s)

    out: list[dict] = []
    for s in tops:
        out.extend(_normalize_top(s, children, tenant, principal, surface))
    return out


def _descendants(root_id: str, children: dict[str, list[dict]]) -> list[dict]:
    """Every child session under root, at any depth."""
    acc: list[dict] = []
    stack = list(children.get(root_id, ()))
    while stack:
        c = stack.pop()
        acc.append(c)
        stack.extend(children.get(c["id"], ()))
    return acc


def _out_tokens(session: dict) -> int:
    return sum(int((m.get("tokens") or {}).get("output") or 0) for m in session["messages"])


def _normalize_top(
    session: dict,
    children: dict[str, list[dict]],
    tenant: str,
    principal: str,
    surface: str,
) -> list[dict]:
    sid = session["id"]
    code = CodeAggregate()

    turns = list(_emit_turns(session, tenant, surface, sid, principal, code))

    subs = _descendants(sid, children)
    sub_out = 0
    for child in subs:
        # child (subagent) coding folds into the SAME aggregate — a model that delegates
        # its typing must not read as having written nothing (SKILL: this reversed a result).
        for msg in child["messages"]:
            if msg.get("role") == "assistant":
                for part in msg.get("parts", []):
                    _apply_part(part, None, code)
        sub_out += _out_tokens(child)

    dominant = session.get("model") or (turns[0]["model"] if turns else None)
    sess = session_record(
        tenant=tenant,
        surface=surface,
        session_id=sid,
        principal=principal,
        model=dominant,
        n_subagents=len(subs),
        n_workflow_runs=0,  # opencode has no workflow grouping; child sessions are the fan-out
        subagent_output_tokens=sub_out,
        code=code,
    )
    return turns + [sess]


def _user_text(msg: dict) -> str:
    return "".join(
        p.get("text") or "" for p in msg.get("parts", []) if p.get("type") == "text"
    )


def _emit_turns(session, tenant, surface, sid, principal, code):
    acc: TurnAccumulator | None = None

    for msg in session["messages"]:
        role = msg.get("role")
        if role == "user":
            nxt = _user_text(msg)
            if acc is not None:
                rec = acc.finalize(nxt)
                if rec:
                    yield rec
            acc = TurnAccumulator(
                tenant=tenant,
                surface=surface,
                session_id=sid,
                principal=principal,
                ts=_iso(msg.get("time_created")),
                user_chars=len(nxt),
            )
        elif role == "assistant":
            if acc is None:
                # an assistant message with no preceding human (resumed/compacted session).
                # Start a turn so its work is still measured.
                acc = TurnAccumulator(
                    tenant=tenant, surface=surface, session_id=sid, principal=principal,
                    ts=_iso(msg.get("time_created")),
                )
            acc.add_assistant(msg.get("modelID"), msg.get("effort"), msg.get("tokens"))
            if (msg.get("finish") or "").lower() in _ABORTED:
                acc.interrupted = True
            for part in msg.get("parts", []):
                _apply_part(part, acc, code)

    if acc is not None:
        rec = acc.finalize(None)
        if rec:
            yield rec


# Edit/write tool input carries the written text under one of these keys, and the target
# path under one of these. Names vary across opencode tool versions; check each.
_CONTENT_KEYS = ("content", "newString", "new_string", "replacement", "patch")
_PATH_KEYS = ("filePath", "file_path", "path")


def _apply_part(part: dict, acc: TurnAccumulator | None, code: CodeAggregate) -> None:
    """Fold one opencode part into the turn accumulator and/or the coding aggregate.

    `acc is None` for a subagent's message: its tools/edits still count toward the session
    coding aggregate, but it produces no main-thread turn record.
    """
    ptype = part.get("type")
    if ptype == "text":
        if acc is not None:
            acc.add_text(part.get("text", ""))
        return
    if ptype != "tool":
        # reasoning / step-start / step-finish / patch: not turn tools, not coding events
        # we count (patch is not authoritative — see module docstring).
        return

    name = (part.get("tool") or "").lower()
    if acc is not None:
        acc.add_tool(name)

    state = part.get("state") or {}
    status = (state.get("status") or "").lower()
    failed = status == "error"
    inp = state.get("input") or {}

    if name == "bash":
        code.note_bash(str(inp.get("command") or ""))
    if failed:
        code.note_tool_error()

    if name in EDIT_TOOLS:
        content = ""
        for k in _CONTENT_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v:
                content = v
                break
        path = ""
        for k in _PATH_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v:
                path = v
                break
        code.note_edit([path] if path else [], chars=len(content), failed=failed)
