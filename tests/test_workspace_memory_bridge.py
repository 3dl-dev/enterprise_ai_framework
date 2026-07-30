"""enterpriseaiframework-471, done-condition 3: a preference stored via chat memory
demonstrably influences a terminal-agent session.

TWO THINGS ARE PROVEN HERE, BOTH FOR REAL, IN THE ORDER THE MECHANISM ACTUALLY RUNS:

1. control-plane/app/chat_memory.py reads LibreChat's OWN memory collection correctly.
   This runs against a real, disposable mongod — not a mock of pymongo, not a fixture
   that hands the function the answer it is supposed to produce — seeded with documents
   shaped exactly like the pinned image's own schema (verified in chat_memory.py's
   docstring: `MemoryEntry` -> collection `memoryentries`, extracted from
   ghcr.io/danny-avila/librechat:v0.8.7 itself, and confirmed again below by asserting
   this test's own seed round-trips through the real chat_identity.refresh() query
   against a real `users` collection, not a monkeypatched cache).

2. The rendered file, once it is one of opencode's `instructions`, genuinely reaches the
   model: the REAL opencode binary is run against deploy/workspace/opencode.json (loaded
   from disk, adapted only at the provider/model, which cannot be a real paid model
   here), and the request it sends to that provider is inspected directly for the
   preference's own text -- a string this test's own Mongo seed put there, not something
   the stub could have produced by fabricating an answer.

WHAT IS MOCKED, AND WHAT IS NOT: nothing about Mongo, nothing about opencode, and nothing
about the tenant-instructions/skills wiring already proven elsewhere. The model behind
opencode's configured provider is tests/opencode_stub.py -- a real paid model is
unnecessary to prove that opencode LOADS a local file and FORWARDS its content, which is
the entire claim under test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control-plane"))

from opencode_stub import ChatCompletionStub, free_port, system_text  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OPENCODE_CONFIG_PATH = REPO / "deploy/workspace/opencode.json"


def _require(binary: str) -> None:
    if shutil.which(binary) is None:
        pytest.fail(
            f"`{binary}` is not on PATH. This test runs the real binary rather than "
            f"mocking it away, per the ground-source rule for this item."
        )


@pytest.fixture(scope="module", autouse=True)
def _preconditions():
    _require("docker")
    _require("opencode")
    try:
        import pymongo  # noqa: F401
    except ImportError:
        pytest.fail(
            "pymongo is not importable in this test environment. bundle/bin/run-tests.sh "
            "installs pymongo==4.10.1 into .venv-test for exactly this file -- if you are "
            "running outside that venv, `pip install pymongo==4.10.1` first."
        )


# ---------------------------------------------------------------- a real, disposable mongod


@pytest.fixture(scope="module")
def mongo():
    """A standalone mongod, not the shared bundle's `chatdb` -- this never touches
    another agent's or the main checkout's stack, and it never leaves the container
    running past this module."""
    name = f"eaf-test-memory-mongo-{uuid.uuid4().hex[:8]}"
    port = free_port()
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-p", f"127.0.0.1:{port}:27017", "mongo:7"],
        capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0, f"docker run mongo:7 failed: {run.stderr}"

    import pymongo

    url = f"mongodb://127.0.0.1:{port}"
    client = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            client = pymongo.MongoClient(url, serverSelectionTimeoutMS=1000)
            client.admin.command("ping")
            break
        except Exception:
            client = None
            time.sleep(0.3)
    if client is None:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail(f"mongo:7 never became reachable:\n{logs.stdout}\n{logs.stderr}")

    yield {"url": url, "client": client}

    client.close()
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture()
def seeded(mongo, monkeypatch):
    """Two real Keycloak-backed chat users and their real memory documents, shaped
    exactly like LibreChat v0.8.7's own schema (packages/data-schemas/src/schema/
    memory.ts): userId/key/value/tokenCount/updated_at, collection `memoryentries`.

    Isolation is asserted, not assumed: user B's memory carries its own distinct marker
    so a test that accidentally returned everyone's memories would be caught, not just a
    test that returned nothing.
    """
    from bson import ObjectId

    monkeypatch.setenv("CHAT_MONGO_URL", mongo["url"])
    monkeypatch.setenv("CHAT_MONGO_DB", "librechat")

    db = mongo["client"]["librechat"]
    db.drop_collection("users")
    db.drop_collection("memoryentries")

    user_a_id = ObjectId()
    user_b_id = ObjectId()
    db["users"].insert_many([
        {"_id": user_a_id, "username": "baron", "email": "baron@example.invalid"},
        {"_id": user_b_id, "username": "other-user", "email": "other@example.invalid"},
    ])

    marker_a = f"MARKER-A-{uuid.uuid4().hex[:10]}"
    marker_b = f"MARKER-B-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    db["memoryentries"].insert_many([
        {
            "userId": user_a_id, "key": "preferred_language", "value": marker_a,
            "tokenCount": 4, "updated_at": now,
        },
        {
            "userId": user_b_id, "key": "preferred_language", "value": marker_b,
            "tokenCount": 4, "updated_at": now,
        },
    ])

    # Re-import fresh so the module picks up the env vars just set -- chat_identity and
    # chat_memory both read CHAT_MONGO_URL/CHAT_MONGO_DB at import time.
    import importlib

    from app import chat_identity as _chat_identity
    importlib.reload(_chat_identity)
    from app import chat_memory as _chat_memory
    importlib.reload(_chat_memory)

    return {"marker_a": marker_a, "marker_b": marker_b, "chat_memory": _chat_memory}


# ---------------------------------------------------------------- the real Mongo read


def test_a_users_memories_are_rendered_and_a_strangers_are_not(seeded):
    md = seeded["chat_memory"].render_instructions_markdown("baron")
    assert seeded["marker_a"] in md
    assert seeded["marker_b"] not in md, (
        "a user's rendered memory file must never contain another user's preference"
    )
    assert "preferred_language" in md


def test_a_user_with_no_memories_yet_gets_a_real_readable_file_not_an_error(seeded):
    md = seeded["chat_memory"].render_instructions_markdown("other-user-with-nothing-stored")
    assert "Nothing stored yet" in md
    assert md  # never empty-string; see the module's own docstring for why that matters


def test_the_cli_wrapper_prints_the_same_content_a_kubectl_exec_would_capture(seeded, monkeypatch):
    """render_workspace_memory.py is what deploy/bin/lib/workspace-memory.sh actually
    invokes via `kubectl exec`. Run it for real, as a subprocess, against the same Mongo.
    """
    import os

    control_plane_dir = REPO / "control-plane"
    env = {**os.environ, "CHAT_MONGO_URL": os.environ["CHAT_MONGO_URL"],
           "CHAT_MONGO_DB": os.environ["CHAT_MONGO_DB"]}
    result = subprocess.run(
        [sys.executable, "-m", "app.render_workspace_memory", "baron"],
        cwd=str(control_plane_dir), env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert seeded["marker_a"] in result.stdout
    assert seeded["marker_b"] not in result.stdout


# ---------------------------------------------------------------- the real opencode run


def test_the_real_terminal_agent_session_is_influenced_by_the_stored_preference(
    seeded, tmp_path
):
    """The whole claim, end to end: render the real user's real memory, point the REAL
    opencode 1.x binary's `instructions` at it (deploy/workspace/opencode.json's own
    third instructions entry, /etc/opencode/memory/MEMORY.md, redirected here to a real
    temp file with the real rendered content -- the cluster mount path is not reachable
    from a bundle-side test), and read the marker back out of the actual request opencode
    sends to the model.
    """
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text(seeded["chat_memory"].render_instructions_markdown("baron"))

    cfg = json.loads(OPENCODE_CONFIG_PATH.read_text())
    cfg.pop("skills", None)
    cfg.pop("mcp", None)  # not exercised by this test; see test_workspace_mcp_parity.py

    def responder(payload: dict):
        return {"role": "assistant", "content": "ack"}, "stop"

    stub = ChatCompletionStub(responder).start()
    try:
        cfg["provider"] = {
            "stub": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Stub",
                "options": {"baseURL": stub.base_url, "apiKey": "sk-stub"},
                "models": {"stub-model": {"name": "Stub Model"}},
            }
        }
        cfg["model"] = "stub/stub-model"
        # The real deployment mounts a ConfigMap directory here; a bundle-side test has
        # no cluster to reach, so this substitutes a real temp file holding the real
        # rendered content at the SAME instructions-list position.
        instructions = cfg["instructions"]
        assert any("memory/MEMORY.md" in p for p in instructions), (
            "deploy/workspace/opencode.json no longer lists a memory instructions file "
            "-- this test's substitution would silently prove nothing"
        )
        cfg["instructions"] = [
            str(memory_md) if "memory/MEMORY.md" in p else p for p in instructions
        ]
        # The other two instructions paths (PLATFORM.md, tenant/TENANT.md) do not exist
        # on this machine. That is deliberately exercised, not avoided: a workspace pod
        # whose tenant ConfigMap has not been seeded yet mounts an empty directory and
        # opencode must not fail to start over a missing instructions file (see
        # tests-live/test_workspace_instructions.py's `optional: true` coverage on the
        # cluster side; this is the bundle-side half of the same claim).

        cfg_path = tmp_path / "opencode.json"
        cfg_path.write_text(json.dumps(cfg))

        import os

        result = subprocess.run(
            ["opencode", "run", "what is my preferred language?", "--model", "stub/stub-model"],
            env={**os.environ, "OPENCODE_CONFIG": str(cfg_path)},
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"opencode exited {result.returncode}\nstdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
        assert stub.requests, "opencode never called the configured model"
        # opencode also fires a separate, short-lived title-generation request with its
        # OWN unrelated system prompt ("You are a title generator...") for every new
        # conversation; picking "any request with a system prompt" would match that one
        # first and prove nothing. The main agent turn's system prompt is the one that
        # begins the way opencode's own built-in agent identity does.
        main_request = next(
            (r for r in stub.requests if "opencode" in system_text(r.get("messages", [])).lower()
             and "title generator" not in system_text(r.get("messages", [])).lower()),
            None,
        )
        assert main_request is not None, (
            "no request carried the main agent system prompt; requests seen: "
            f"{[system_text(r.get('messages', []))[:80] for r in stub.requests]}"
        )
        prompt = system_text(main_request["messages"])
        assert seeded["marker_a"] in prompt, (
            f"the stored preference never reached the model's system prompt; system "
            f"prompt was:\n{prompt[:2000]}"
        )
    finally:
        stub.stop()
