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

GAP 2 (adversary finding), closed by `TestPreferenceWrittenViaChatMemoryReachesTheTerminalAgent`
below: done-condition 3 says a preference is stored VIA CHAT MEMORY, but the `seeded`
fixture above hand-writes `memoryentries` documents directly -- the collection name, the
userId ObjectId-vs-string typing, and the field names are asserted by the test author,
not produced by the system under test, so none of the tests using it would notice if
chat_memory.py stopped being faithful to LibreChat's real schema. That class writes the
preference through chat's own POST /api/memories against the running bundle and reads it
back through chat_memory's own CLI wrapper, run for real inside the running control-plane
container -- no hand-authored Mongo document anywhere in that chain.
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

import oidc_login  # noqa: E402
from conftest import DOGFOOD_USER, compose  # noqa: E402
from opencode_stub import ChatCompletionStub, free_port, system_text  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OPENCODE_CONFIG_PATH = REPO / "deploy/workspace/opencode.json"
TIMEOUT = 60.0


def _token(client, chat_url: str) -> str:
    r = client.post(
        f"{chat_url}/api/auth/refresh",
        headers={"Cookie": oidc_login._cookie_header(client)},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(client, chat_url: str) -> dict:
    return {
        "Authorization": f"Bearer {_token(client, chat_url)}",
        "Cookie": oidc_login._cookie_header(client),
    }


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


def _system_prompt_opencode_sends_with_memory_file(memory_markdown: str, tmp_path) -> str:
    """Point the REAL opencode 1.x binary's `instructions` at a real memory file holding
    `memory_markdown`, run it for real, and return the system prompt of the main agent
    turn's request to the (stubbed) model.

    deploy/workspace/opencode.json's own third instructions entry,
    /etc/opencode/memory/MEMORY.md, is redirected here to a real temp file with the real
    rendered content -- the cluster mount path is not reachable from a bundle-side test.
    Shared by both `test_the_real_terminal_agent_session_is_influenced_by_the_stored_preference`
    (content from a hand-seeded Mongo fixture -- proves chat_memory's rendering logic
    given arbitrary well-formed data) and
    `TestPreferenceWrittenViaChatMemoryReachesTheTerminalAgent` (content produced by the
    real chat surface's own POST /api/memories, read back through the real chat_memory
    module against the real chatdb -- proves the end-to-end claim done-condition 3 makes,
    with no hand-authored Mongo document anywhere in the chain).
    """
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text(memory_markdown)

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
        return system_text(main_request["messages"])
    finally:
        stub.stop()


def test_the_real_terminal_agent_session_is_influenced_by_the_stored_preference(
    seeded, tmp_path
):
    """The whole claim, end to end: render the real user's real memory, point the REAL
    opencode binary's `instructions` at it, and read the marker back out of the actual
    request opencode sends to the model.

    This proves chat_memory's rendering logic given arbitrary well-formed Mongo data;
    it does NOT by itself prove the data came from LibreChat's own memory API, which is
    exactly the gap `TestPreferenceWrittenViaChatMemoryReachesTheTerminalAgent` below
    closes with a real POST /api/memories in place of `seeded`'s hand-written documents.
    """
    prompt = _system_prompt_opencode_sends_with_memory_file(
        seeded["chat_memory"].render_instructions_markdown("baron"), tmp_path
    )
    assert seeded["marker_a"] in prompt, (
        f"the stored preference never reached the model's system prompt; system "
        f"prompt was:\n{prompt[:2000]}"
    )


# ---------------------------------------------------------------- gap 2: no hand-seeded
# Mongo anywhere in the chain -- write through chat's own API, read back through the
# real chat_memory module against the real chatdb.


@pytest.mark.usefixtures("stack_up")
class TestPreferenceWrittenViaChatMemoryReachesTheTerminalAgent:
    """enterpriseaiframework-471, gap 2 (adversary finding): done-condition (3) says a
    preference is stored VIA CHAT MEMORY, but every test above seeds Mongo directly --
    the collection name, the userId ObjectId-vs-string typing, and the field names are
    asserted by the test author rather than produced by the system under test, so none
    of them would notice if chat_memory.py stopped being faithful to LibreChat's actual
    schema. This class writes the preference through the real chat surface's own
    POST /api/memories (the same real endpoint
    tests/test_chat_surface_version.py::test_a_memory_outlives_the_session_that_wrote_it
    already exercises against the running bundle with a real OIDC session) and reads it
    back through chat_memory.py's own CLI wrapper (render_workspace_memory.py), run for
    real inside the running control-plane container against the SAME chatdb Mongo the
    bundle actually populated. No fixture hand-writes a Mongo document anywhere below.

    Requires `stack_up`: unlike every other test in this file (which stands up its own
    disposable, standalone mongod), this one needs the real running bundle -- the real
    chat surface to write through, and the real control-plane container to read back
    through.
    """

    def _store_and_render(self, chat_session, chat_url) -> tuple[str, str, str]:
        """POST a fresh marker preference through the real chat API, then render it
        back via the real control-plane container's CLI wrapper. Returns
        (key, value, rendered_markdown); the caller is responsible for deleting `key`.
        """
        # Same lowercase-letters-and-underscores-only key constraint
        # test_a_memory_outlives_the_session_that_wrote_it documents: LibreChat's
        # MemoryEntry schema rejects anything else with a 500.
        suffix = "".join(chr(ord("a") + b % 26) for b in uuid.uuid4().bytes[:8])
        key = f"probe_{suffix}"
        value = f"remembered-{uuid.uuid4().hex[:12]}"
        headers = _auth(chat_session, chat_url)

        created = chat_session.post(
            f"{chat_url}/api/memories", headers=headers,
            json={"key": key, "value": value}, timeout=TIMEOUT,
        )
        assert created.status_code in (200, 201), (
            f"the surface would not store a memory ({created.status_code}): "
            f"{created.text[:300]}"
        )

        rendered = compose(
            "exec", "-T", "control-plane", "python3", "-m",
            "app.render_workspace_memory", DOGFOOD_USER,
        )
        assert rendered.returncode == 0, (
            f"render_workspace_memory.py failed inside the real control-plane "
            f"container: {rendered.stderr}"
        )
        return key, value, rendered.stdout

    def test_a_memory_stored_through_the_chat_api_is_read_back_by_chat_memory(
        self, chat_session, chat_url
    ):
        key, value, rendered = self._store_and_render(chat_session, chat_url)
        try:
            assert value in rendered, (
                f"a memory written through chat's own POST /api/memories was not read "
                f"back by chat_memory.render_instructions_markdown, run for real inside "
                f"the real control-plane container against the real chatdb Mongo; "
                f"rendered content was: {rendered!r}"
            )
            assert key in rendered
        finally:
            chat_session.delete(
                f"{chat_url}/api/memories/{key}",
                headers=_auth(chat_session, chat_url), timeout=TIMEOUT,
            )

    def test_the_same_preference_reaches_a_real_terminal_agent_session(
        self, chat_session, chat_url, tmp_path
    ):
        """The whole chain, end to end, with no hand-authored Mongo document anywhere:
        chat's own API writes the memory; chat_memory (via the real CLI wrapper, run
        inside the real control-plane container against the real chatdb) renders it;
        the REAL opencode binary loads that rendered file as an instructions entry and
        forwards its content to the model.
        """
        key, value, rendered = self._store_and_render(chat_session, chat_url)
        try:
            assert value in rendered, (
                "the memory was not readable back before even reaching opencode -- see "
                "test_a_memory_stored_through_the_chat_api_is_read_back_by_chat_memory "
                "for the isolated failure"
            )
            prompt = _system_prompt_opencode_sends_with_memory_file(rendered, tmp_path)
            assert value in prompt, (
                f"a preference stored via chat's own memory API never reached the "
                f"terminal agent's system prompt; system prompt was:\n{prompt[:2000]}"
            )
        finally:
            chat_session.delete(
                f"{chat_url}/api/memories/{key}",
                headers=_auth(chat_session, chat_url), timeout=TIMEOUT,
            )
