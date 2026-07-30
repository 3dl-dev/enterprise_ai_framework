"""Fast, no-database tests for chat_memory's rendering logic alone.

The real query path -- resolving a username through chat_identity against a real `users`
collection, reading real `memoryentries` documents, and proving the rendered file
actually reaches a real opencode session -- is proven against a real, disposable mongod
in tests/test_workspace_memory_bridge.py; that is the ground-truth proof for this item.
This file is narrower and does not repeat it: it monkeypatches only
`memories_for_user` (never Mongo, never pymongo) to pin down `render_instructions_
markdown`'s own formatting decisions -- the empty case, ordering, and never returning an
empty string -- the same way test_export_attribution.py in this directory patches
chat_identity's cache rather than starting a database for logic that does not touch one.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Same shim test_export_attribution.py uses: chat_identity imports nothing that needs a
# real pymongo here (this file never calls the Mongo-touching functions), but the
# module-level `from pymongo import MongoClient` inside chat_identity._connect is
# deferred (only imported inside the function body), so no shim is actually required for
# THIS file's imports to succeed -- chat_memory.py itself only imports pymongo/bson
# lazily inside memories_for_user, which these tests never call for real.
from app import chat_memory  # noqa: E402


def test_no_memories_renders_a_real_readable_file_not_an_empty_string(monkeypatch):
    monkeypatch.setattr(chat_memory, "memories_for_user", lambda username: [])
    md = chat_memory.render_instructions_markdown("nobody")
    assert md.startswith(chat_memory.HEADER)
    assert chat_memory.EMPTY_BODY in md
    assert md.endswith("\n")


def test_memories_are_rendered_as_a_bullet_per_key(monkeypatch):
    monkeypatch.setattr(
        chat_memory, "memories_for_user",
        lambda username: [
            {"key": "preferred_language", "value": "Python", "updated_at": 2},
            {"key": "editor", "value": "vim", "updated_at": 1},
        ],
    )
    md = chat_memory.render_instructions_markdown("baron")
    assert "- **preferred_language**: Python" in md
    assert "- **editor**: vim" in md
    assert chat_memory.EMPTY_BODY not in md


def test_memories_for_user_returns_empty_when_the_username_has_no_chat_identity(monkeypatch):
    """chat_identity.ids_for returning nothing (a workspace user who has never signed
    into chat) must degrade to "nothing to remember", never an exception."""
    fake_identity = types.SimpleNamespace(ids_for=lambda username: [])
    monkeypatch.setattr(chat_memory, "chat_identity", fake_identity)
    assert chat_memory.memories_for_user("never-used-chat") == []


def test_memories_for_user_returns_empty_when_mongo_is_unreachable(monkeypatch):
    """_connect() returning None (no CHAT_MONGO_URL, or a real connection failure) must
    degrade the same way -- a chat database hiccup costs a missing preference, never a
    failed workspace provision."""
    fake_identity = types.SimpleNamespace(ids_for=lambda username: ["6a67b18069dba4d1126fef44"])
    monkeypatch.setattr(chat_memory, "chat_identity", fake_identity)
    monkeypatch.setattr(chat_memory, "_connect", lambda: None)
    assert chat_memory.memories_for_user("baron") == []
