"""LibreChat Mongo -> normalized turn/session records (chat surface).

Chat is a PROSE surface, not a coding one: no file edits, no patches. Its measurable
families are prose structure, escalation (question/permission/hedge density) and tokens —
so every session record here carries `code: null`, never a misleading zero (design record).

Two layers, same split as the opencode reader:

  read_raw_messages(db)         the Mongo layer: the `messages` collection into dicts.
  normalize(messages, ...)      PURE: dicts -> records. Golden-testable, no Mongo.

LibreChat's schema (from the live `librechat` db): a `messages` doc per message with
`messageId, conversationId, parentMessageId, isCreatedByUser, text, model, tokenCount,
user, createdAt, unfinished, error`. Role is the `isCreatedByUser` flag (there is no
role enum). `user` is LibreChat's internal ObjectId hex — the SAME id
`control-plane/app/chat_identity.py` already maps to a username for the bill; the caller
passes that resolver in as `resolve_principal` so we do not re-derive identity here.
"""

from __future__ import annotations

from collections import defaultdict

from .schema import TurnAccumulator, session_record


def _created(m: dict):
    """LibreChat createdAt is a Mongo date; in a JSON export it is {"$date": ...} or a
    string. Return something sortable/renderable — the ISO string when we have one."""
    v = m.get("createdAt")
    if isinstance(v, dict):
        return v.get("$date") or ""
    return v or ""


def _identity(u):
    return u


def normalize(
    messages: list[dict],
    *,
    tenant: str,
    resolve_principal=_identity,
    toolcalls_by_message: dict[str, list[str]] | None = None,
    surface: str = "chat",
) -> list[dict]:
    """Raw LibreChat messages -> turn + session records. Pure; golden-testable.

    `resolve_principal(user_objectid_hex) -> username` is injected so this stays free of the
    live identity lookup; the caller passes `chat_identity`'s resolver. `toolcalls_by_message`
    maps a messageId to the tool names invoked in it (chat tool-use lives in its own
    collection); absent -> chat used no tools, which is the common case.
    """
    tc = toolcalls_by_message or {}
    conv: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        conv[m.get("conversationId")].append(m)

    out: list[dict] = []
    for cid, msgs in conv.items():
        msgs.sort(key=lambda m: (_created(m), m.get("messageId") or ""))
        out.extend(_normalize_conversation(cid, msgs, tenant, resolve_principal, tc, surface))
    return out


def _normalize_conversation(cid, msgs, tenant, resolve_principal, tc, surface):
    principal = resolve_principal((msgs[0].get("user") if msgs else None)) or "(unattributed)"

    turns: list[dict] = []
    acc: TurnAccumulator | None = None
    dominant_model: str | None = None

    for m in msgs:
        if m.get("isCreatedByUser"):
            nxt = m.get("text") or ""
            if acc is not None:
                rec = acc.finalize(nxt)
                if rec:
                    turns.append(rec)
            acc = TurnAccumulator(
                tenant=tenant,
                surface=surface,
                session_id=cid,
                principal=principal,
                ts=_created(m) or None,
                user_chars=len(nxt),
            )
        else:
            if acc is None:
                acc = TurnAccumulator(
                    tenant=tenant, surface=surface, session_id=cid, principal=principal,
                    ts=_created(m) or None,
                )
            model = m.get("model")
            dominant_model = dominant_model or model
            # LibreChat tokenCount is this message's total tokens; for an assistant message
            # that is its output. The ledger join (-0e90) supersedes this for cost.
            acc.add_assistant(model, None, {"output": m.get("tokenCount") or 0})
            if m.get("unfinished") or m.get("error"):
                acc.interrupted = True
            for name in tc.get(m.get("messageId"), ()):  # chat tool-use, if any
                acc.add_tool(name)
            acc.add_text(m.get("text") or "")

    if acc is not None:
        rec = acc.finalize(None)
        if rec:
            turns.append(rec)

    sess = session_record(
        tenant=tenant,
        surface=surface,
        session_id=cid,
        principal=principal,
        model=dominant_model,
        n_subagents=0,
        n_workflow_runs=0,
        subagent_output_tokens=0,
        code=None,  # chat is a prose surface — no coding metrics, and no false zero
    )
    return turns + [sess]


# ------------------------------------------------------------------ the Mongo layer


def read_raw_messages(db, *, tenant_filter=None) -> list[dict]:
    """Read the `messages` collection into dicts. `db` is a pymongo Database (read-only by
    convention, like metering.py). Projected to the fields the normalizer uses — the text
    is measured at ingest and only its counts persist, so content never leaves this process.
    """
    projection = {
        "_id": 0, "messageId": 1, "conversationId": 1, "parentMessageId": 1,
        "isCreatedByUser": 1, "text": 1, "model": 1, "tokenCount": 1, "user": 1,
        "createdAt": 1, "unfinished": 1, "error": 1,
    }
    query = {} if tenant_filter is None else tenant_filter
    docs = list(db.messages.find(query, projection))
    # Coerce Mongo-native types to JSON-safe ones at the boundary: `user` is an ObjectId
    # (must match the str(_id) identity map), `createdAt` a datetime (must land in the
    # content-free record and serialize into the store). Without this the collector's
    # json.dump of the records throws and the whole tick is lost.
    for d in docs:
        if d.get("user") is not None:
            d["user"] = str(d["user"])
        ca = d.get("createdAt")
        if hasattr(ca, "isoformat"):
            d["createdAt"] = ca.isoformat()
    return docs
