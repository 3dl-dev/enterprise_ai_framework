"""Chat's per-user memory, surfaced as instructions the terminal agent auto-loads.

THE PROBLEM THIS SOLVES (enterpriseaiframework-471, done-condition 3)

A user tells chat "remember I prefer X" and that preference is durably stored — see
bundle/librechat/librechat.yaml's `memory` block and enterpriseaiframework-b8d. The
terminal agent (opencode) is a completely separate process with no access to LibreChat's
Mongo, so without a bridge that preference is invisible there. opencode already has the
hook: its `instructions` config key is a list of markdown files it loads into every
session's system prompt (deploy/workspace/opencode.json), the same mechanism that
already carries the deployment's platform facts and tenant house rules
(enterpriseaiframework-cbf). This module renders one more such file, per user, from the
same Mongo collection LibreChat's own memory UI reads.

WHERE THE DATA ACTUALLY LIVES, VERIFIED AGAINST THE PINNED IMAGE, NOT ASSUMED

LibreChat v0.8.7's memory schema (packages/data-schemas/src/schema/memory.ts, extracted
from ghcr.io/danny-avila/librechat:v0.8.7 — the image this bundle pins) is a mongoose
model named `MemoryEntry` with fields `userId` (ObjectId ref User), `key`, `value`,
`tokenCount`, `updated_at`. mongoose's own pluralizer (confirmed by running
`node -e "console.log(require('mongoose').pluralize()('MemoryEntry'))"` inside that same
image) names the collection `memoryentries` — not something this module invents.

WHY THIS READS ANOTHER COMPONENT'S DATABASE, again

Same reasoning as chat_identity.py, which already does exactly this for a different
collection (`users`) for a different reason (naming a spend row's principal). The
mapping exists in exactly one place; there is no API a credential-less operator script
could call instead without minting the user a session, and duplicating the data would be
a second source of truth for something a chat message already wrote once. Read-only,
narrow, isolated here.

Degrades to "nothing to remember" on any failure — Mongo unreachable, user has no chat
memories yet, chat_identity cannot resolve the username — never to an error. A workspace
that fails to provision because a chat database hiccuped would cost far more than a
missing preference.
"""

import os

from . import chat_identity

_MONGO_URL = os.environ.get("CHAT_MONGO_URL", "")
_MONGO_DB = os.environ.get("CHAT_MONGO_DB", "librechat")
_COLLECTION = "memoryentries"

HEADER = "# Remembered preferences"
EMPTY_BODY = "Nothing stored yet."
INTRO = "Stated by this user in chat and carried over here automatically."


def _connect():
    """Return a Mongo client, or None if we cannot or should not have one.

    Mirrors chat_identity._connect exactly (short timeouts, swallow every connection
    error) — this is a nicety surfaced at workspace-provisioning time, never something
    worth failing a pod over.
    """
    if not _MONGO_URL:
        return None
    try:
        from pymongo import MongoClient
    except ImportError:
        return None
    try:
        return MongoClient(
            _MONGO_URL, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500,
            socketTimeoutMS=2000,
        )
    except Exception:
        return None


def memories_for_user(username: str) -> list[dict]:
    """Every stored memory entry for `username`, newest-first. Empty on any failure.

    Goes through chat_identity.ids_for rather than a second lookup of the `users`
    collection — one place resolves a username to a chat identity, same as the spend
    ledger's own translation, so a user who has more than one chat account (chat_identity
    never assumes exactly one) gets memories from all of them.
    """
    ids = chat_identity.ids_for(username)
    if not ids:
        return []
    client = _connect()
    if client is None:
        return []
    try:
        from bson import ObjectId
        from bson.errors import InvalidId

        object_ids = []
        for i in ids:
            try:
                object_ids.append(ObjectId(i))
            except (InvalidId, TypeError):
                continue
        if not object_ids:
            return []
        docs = list(client[_MONGO_DB][_COLLECTION].find({"userId": {"$in": object_ids}}))
    except Exception:
        return []
    finally:
        try:
            client.close()
        except Exception:
            pass

    out = [
        {"key": d.get("key", ""), "value": d.get("value", ""), "updated_at": d.get("updated_at")}
        for d in docs
        if d.get("key") and d.get("value")
    ]
    out.sort(key=lambda m: m["updated_at"] or 0, reverse=True)
    return out


def render_instructions_markdown(username: str) -> str:
    """The exact byte content of the file opencode's `instructions` list points at for
    this user (/etc/opencode/memory/MEMORY.md — see deploy/k8s/61-workspace.template.yaml
    and deploy/bin/lib/workspace-memory.sh).

    Never empty-string: an absent-vs-present distinction on this file is carried by the
    ConfigMap's `optional: true` volume, not by this function returning nothing, so a
    freshly provisioned user with no chat memories yet still gets a real, readable file
    rather than opencode silently skipping a zero-byte one.
    """
    memories = memories_for_user(username)
    if not memories:
        return f"{HEADER}\n\n{EMPTY_BODY}\n"
    lines = [HEADER, "", INTRO, ""]
    for m in memories:
        lines.append(f"- **{m['key']}**: {m['value']}")
    return "\n".join(lines) + "\n"
