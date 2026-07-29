"""Live proof for enterpriseaiframework-b8d: durable, per-user memory.

DONE CONDITION (from the item): state a preference in one conversation, start a
genuinely NEW conversation, observe the preference applied without restating it. Then
restart the chat deployment and confirm it still holds -- proving it lives in Mongo
(the `memory` collection LibreChat v0.8.0 ships, read via GET/POST /api/memories) and
not in the chat process. Verified against the CLUSTER at
https://gateway.tailcb6ef9.ts.net:8443, not compose.

ATTENTION-GATED: memory is user data. Configuring it in bundle/librechat/librechat.yaml
(the `memory.agent` block routing memory extraction through our own gateway, same
virtual key, same ledger as any other model call -- see the comments there) only wires
LibreChat's own per-user storage (keyed on req.user.id in every model/route touched --
api/models Memory methods, api/server/routes/memories.js); it does not by itself prove
isolation. TestMemoryIsolation proves it empirically with two real Keycloak identities
(BOOTSTRAP_USER from bundle/.env, and the `student` account
deploy/bin/ensure-second-user.sh already created and pinned in the
`workspace-test-user` Secret -- reused here rather than minting a third user) rather
than reading the schema and asserting it must be fine.

Kept out of `tests/` (pytest.ini scopes that to the hermetic suite) because this talks
to the real cluster over the public hostname, drives a real model through Forge/glm,
spends a small amount of real money, and -- uniquely among this repo's live tests --
restarts the shared `chat` Deployment. That restart is a few seconds of downtime for
anyone else using the chat surface at that moment; this suite is meant to be run
deliberately (dogfood verification), not on a timer.

Run: .venv-test/bin/pytest tests-live/test_memory.py -v --tb=short -p no:cacheprovider
"""

import base64
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "bundle"
NAMESPACE = "enterprise-ai"

import oidc_login  # noqa: E402  (pytest.ini sets pythonpath = tests)

# /api/agents/chat rejects non-browser User-Agents with "Illegal request"
# (dogfood-findings.md finding 19) -- not a bug to work around, a precondition.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

CHAT_URL = "https://gateway.tailcb6ef9.ts.net:8443"

# Must be a model that reliably invokes tools -- memory extraction is a tool call
# (set_memory/delete_memory) on every turn. glm-5.2@deepinfra is what this bundle has
# actually measured doing that (wave 1); it is also what librechat.yaml configures as
# the `memory.agent` model, so this is exercising the real configured path, not a
# stand-in. `fake-large` cannot be used: it has no tool-calling behavior at all.
MODEL = "glm-5.2@deepinfra"
ENDPOINT_NAME = "Enterprise AI"
ENDPOINT_TYPE = "custom"


def _env() -> dict:
    out: dict[str, str] = {}
    env_file = BUNDLE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def _kubectl_secret(name: str, key: str) -> str:
    r = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        pytest.fail(
            f"could not read secret/{name} key {key} in namespace {NAMESPACE}: "
            f"{r.stderr.strip() or r.stdout.strip()}"
        )
    return base64.b64decode(r.stdout.strip()).decode()


@pytest.fixture(scope="module")
def creds_a() -> tuple[str, str]:
    e = _env()
    user, password = e.get("BOOTSTRAP_USER"), e.get("BOOTSTRAP_PASSWORD")
    if not user or not password:
        pytest.fail(
            "BOOTSTRAP_USER / BOOTSTRAP_PASSWORD not configured in bundle/.env -- "
            "these are the realm identity provisioned by `make up` / deploy/bin/deploy.sh"
        )
    return user, password


@pytest.fixture(scope="module")
def creds_b() -> tuple[str, str]:
    """The second realm user, reused rather than created.

    deploy/bin/ensure-second-user.sh (enterpriseaiframework-b73, the parallel workspace
    item) already created this account and pinned its password in
    secret/workspace-test-user so reruns of either item's live tests do not invalidate
    each other's credential. If the secret is missing, this fails loudly rather than
    minting a third identity -- the item said to reuse it.
    """
    user = _kubectl_secret("workspace-test-user", "USERNAME")
    password = _kubectl_secret("workspace-test-user", "PASSWORD")
    return user, password


def _login_client(chat_url: str, user: str, password: str) -> httpx.Client:
    client = oidc_login.login(chat_url, user, password)
    client.headers.update({"User-Agent": _BROWSER_UA})
    refreshed = client.post(f"{chat_url}/api/auth/refresh")
    assert refreshed.status_code == 200, (
        f"session refresh failed ({refreshed.status_code}) for {user}: {refreshed.text[:300]}"
    )
    token = refreshed.json().get("token")
    assert token, f"no access token in refresh response for {user}: {refreshed.text[:300]}"
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture(scope="module")
def chat_client_a(creds_a) -> httpx.Client:
    """BOOTSTRAP_USER's session -- the user who states the preference."""
    user, password = creds_a
    client = _login_client(CHAT_URL, user, password)
    yield client
    client.close()


@pytest.fixture(scope="module")
def chat_client_b(creds_b) -> httpx.Client:
    """The `student` account -- a second, unrelated Keycloak identity."""
    user, password = creds_b
    client = _login_client(CHAT_URL, user, password)
    yield client
    client.close()


def _send_message(client: httpx.Client, text: str, conversation_id: str | None = None) -> dict:
    """POST a real chat message on the default custom endpoint and return the
    terminal ('final': true) SSE event as a dict. Mirrors tests-live/test_mcp_echo.py.

    THIS ONLY WORKS AGAINST LibreChat v0.8.0, WHICH IS WHAT THE CLUSTER STILL RUNS.
    enterpriseaiframework-f50 moved the compose bundle and deploy/k8s/50-chat.yaml to
    v0.8.7, where POST /api/agents/chat/<endpoint> no longer streams: it returns
    `application/json` {"streamId", "conversationId", "status": "started"} and the answer
    arrives on a separate GET /api/agents/chat/stream/<streamId>. Parsed as SSE that body
    yields zero frames, so this helper fails with "stream ended with no terminal event" —
    naming the wrong thing entirely. Measured against a v0.8.7 container, not inferred.

    tests/chat_turn.py already handles both protocols (it reads the answer from the
    persisted message via /api/messages/<conversationId>, which works on both). This file
    was NOT converted to it: the assertions below read a memory attachment off the
    terminal SSE event, and what the equivalent looks like on v0.8.7 cannot be established
    without running a tool-calling model against a v0.8.7 surface — which needs the
    cluster upgraded. Converting it blind would swap a known breakage for an unknown one.
    Tracked as its own item; do it in the same maintenance window as the cluster upgrade.
    """
    body = {
        "text": text,
        "endpoint": ENDPOINT_NAME,
        "endpointType": ENDPOINT_TYPE,
        "model": MODEL,
        "sender": "User",
        "isCreatedByUser": True,
        "messageId": str(uuid.uuid4()),
        "parentMessageId": "00000000-0000-0000-0000-000000000000",
        "conversationId": conversation_id,
        "error": False,
        "isContinued": False,
        "isTemporary": False,
        "isRegenerate": False,
    }
    url = f"{CHAT_URL}/api/agents/chat/{urllib.parse.quote(ENDPOINT_NAME, safe='')}"

    final_event: dict | None = None
    with client.stream(
        "POST", url, json=body, headers={"Accept": "text/event-stream"}, timeout=120,
    ) as r:
        assert r.status_code == 200, (
            f"chat request failed ({r.status_code}); if this is a 500 from the model call, "
            f"check Forge credentials (bundle/.env) before suspecting memory wiring: "
            f"{r.read()[:500]}"
        )
        import json as _json

        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                event = _json.loads(payload)
            except _json.JSONDecodeError:
                continue
            if event.get("final") is True:
                final_event = event
            if "text" in event and set(event.keys()) == {"text"}:
                pytest.fail(f"chat surface returned an error event: {event['text']}")

    assert final_event is not None, "stream ended with no terminal ('final': true) event"
    return final_event


def _reply_text(final: dict) -> str:
    content = (final.get("responseMessage") or {}).get("content") or []
    return "".join(c.get("text", "") for c in content if c.get("type") == "text")


def _memory_attachment(final: dict) -> dict | None:
    """The memory tool's own recorded write, if the turn produced one.

    Memory extraction runs as a separate background agent invocation
    (api/server/controllers/agents/client.js #useMemory / #runMemory), not as a
    tool-call content block in the main reply -- the visible assistant text ("I'll
    remember that...") is not evidence the write happened, since a model can say that
    without any tool existing. The actual write surfaces as an
    `attachments[].memory` entry on the SAME final responseMessage
    (createMemoryCallback in @librechat/api), with `{key, type, value}` -- that is
    the thing this checks.
    """
    attachments = (final.get("responseMessage") or {}).get("attachments") or []
    for a in attachments:
        if a.get("memory"):
            return a["memory"]
    return None


def _conversation_id(final: dict) -> str | None:
    conv = final.get("conversation") or {}
    return (
        conv.get("conversationId")
        or (final.get("responseMessage") or {}).get("conversationId")
        or final.get("conversationId")
    )


def _get_memories(client: httpx.Client) -> list[dict]:
    r = client.get(f"{CHAT_URL}/api/memories")
    assert r.status_code == 200, f"GET /api/memories failed ({r.status_code}): {r.text[:300]}"
    return r.json().get("memories", [])


def _wait_for_memory(client: httpx.Client, needle: str, timeout: float = 45.0) -> dict:
    """Poll GET /api/memories until an entry whose value contains `needle` appears.

    The chat request itself awaits memory processing before its SSE stream ends
    (api/server/controllers/agents/client.js #awaitMemoryWithTimeout, up to a 3s grace
    period after the main completion finishes) so this should resolve almost
    immediately; the poll loop is a safety margin against a slow Forge round trip, not
    evidence memory works asynchronously behind the response.
    """
    deadline = time.monotonic() + timeout
    last_seen: list[dict] = []
    while time.monotonic() < deadline:
        last_seen = _get_memories(client)
        for m in last_seen:
            if needle in (m.get("value") or ""):
                return m
        time.sleep(2)
    pytest.fail(
        f"memory containing {needle!r} never appeared within {timeout}s; "
        f"memories on file: {last_seen}"
    )


def _delete_memory(client: httpx.Client, key: str) -> None:
    client.delete(f"{CHAT_URL}/api/memories/{urllib.parse.quote(key, safe='')}")


def _login_with_retry(
    chat_url: str, user: str, password: str, timeout: float = 60.0,
) -> httpx.Client:
    """Same as _login_client, tolerant of the few seconds right after a rollout
    where the public ingress hasn't yet noticed the new pod and 502s.

    This is infra settling time, not the thing under test -- the persistence claim is
    about Mongo surviving the restart, not about the rollout being zero-downtime.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _login_client(chat_url, user, password)
        except AssertionError as e:
            last_error = e
            time.sleep(3)
    raise AssertionError(f"login kept failing for {timeout}s after restart: {last_error}")


def _restart_chat_deployment() -> None:
    restart = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "rollout", "restart", "deployment/chat"],
        capture_output=True, text=True,
    )
    assert restart.returncode == 0, f"rollout restart failed: {restart.stderr}"
    status = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "rollout", "status", "deployment/chat", "--timeout=300s"],
        capture_output=True, text=True,
    )
    assert status.returncode == 0, (
        f"chat deployment did not come back healthy after restart: "
        f"{status.stdout}\n{status.stderr}"
    )


class TestDurablePreferenceMemory:
    """enterpriseaiframework-b8d: a stated preference outlives the conversation and
    the chat process."""

    def test_preference_recalled_in_new_conversation_and_survives_restart(self, chat_client_a):
        nonce = uuid.uuid4().hex[:10]
        fruit = f"nectra-{nonce}"
        key = None
        try:
            # 1. State the preference. Default memory instructions (librechat's
            #    getDefaultInstructions) only write on an explicit "remember" ask --
            #    this phrasing is deliberately that, not incidental mention.
            store_final = _send_message(
                chat_client_a,
                f"Please remember that my favorite fruit is {fruit}.",
            )
            convo1 = _conversation_id(store_final)
            assert convo1, f"no conversationId on the storing turn: {store_final}"

            attachment = _memory_attachment(store_final)
            assert attachment, (
                f"model produced no memory-tool write on an explicit remember "
                f"request. responseMessage: {store_final.get('responseMessage')}"
            )
            assert fruit in (attachment.get("value") or ""), (
                f"memory write did not contain the stated preference: {attachment}"
            )
            key = attachment.get("key")
            assert key, f"stored memory has no key to clean up: {attachment}"

            # 2. Confirm it actually landed in Mongo and is readable back via the
            #    memories API (not just that the tool claimed to write it).
            _wait_for_memory(chat_client_a, fruit)

            # 3. A genuinely NEW conversation (conversationId=None), no restatement.
            recall_final = _send_message(
                chat_client_a,
                "What is my favorite fruit? Reply with just the value, nothing else.",
            )
            convo2 = _conversation_id(recall_final)
            assert convo2 and convo2 != convo1, (
                f"recall turn did not start a new conversation (got {convo2!r}, "
                f"same as storing turn {convo1!r})"
            )
            reply = _reply_text(recall_final)
            assert fruit in reply, (
                f"preference was not honoured in a new conversation without "
                f"restating it; reply was: {reply!r}"
            )

            # 4. Restart the chat deployment. If the preference only lived in the old
            #    process's memory, it disappears here; if it is really in Mongo
            #    (data-schemas `memory` collection), it survives.
            _restart_chat_deployment()

            # Re-authenticate as a fresh session would after a restart, rather than
            # trusting a bearer token minted before it (same reasoning as
            # tests-live/test_mcp_echo.py: prove what a real user experiences).
            fresh_client = _login_with_retry(CHAT_URL, *_env_creds_a())
            try:
                post_restart_final = _send_message(
                    fresh_client,
                    "What is my favorite fruit? Reply with just the value, nothing else.",
                )
                convo3 = _conversation_id(post_restart_final)
                assert convo3 and convo3 not in (convo1, convo2), (
                    "post-restart recall turn was not a new conversation"
                )
                post_reply = _reply_text(post_restart_final)
                assert fruit in post_reply, (
                    f"preference did not survive a chat deployment restart -- it was "
                    f"held in process memory, not Mongo. Reply was: {post_reply!r}"
                )
            finally:
                fresh_client.close()
        finally:
            if key:
                _delete_memory(chat_client_a, key)


def _env_creds_a() -> tuple[str, str]:
    e = _env()
    return e["BOOTSTRAP_USER"], e["BOOTSTRAP_PASSWORD"]


class TestMemoryIsolation:
    """enterpriseaiframework-b8d, ATTENTION-GATED: per-user memory must not leak.

    Proven by trying to read it as the other user, against two real Keycloak
    accounts, not by reading the schema and asserting `userId` scoping must work.
    """

    def test_users_preference_not_visible_to_second_user(self, chat_client_a, chat_client_b):
        nonce_a = uuid.uuid4().hex[:10]
        gem_a = f"lumite-{nonce_a}"
        key_a = None
        try:
            store_final = _send_message(
                chat_client_a,
                f"Please remember that my favorite gemstone is {gem_a}.",
            )
            attachment_a = _memory_attachment(store_final)
            assert attachment_a, (
                f"user A: no memory-tool write: {store_final.get('responseMessage')}"
            )
            key_a = attachment_a.get("key")
            _wait_for_memory(chat_client_a, gem_a)

            # B asks the same question in a brand new conversation. B has never
            # mentioned a gemstone; if this answers with A's value, memory leaked
            # across users.
            b_final = _send_message(
                chat_client_b,
                "What is my favorite gemstone? Reply with just the value, or say you "
                "don't know if you have no memory of it.",
            )
            b_reply = _reply_text(b_final)
            assert gem_a not in b_reply, (
                f"user B's reply contained user A's stored preference -- memory "
                f"leaked across users. B's reply: {b_reply!r}"
            )

            # Read B's own memory list directly -- the strongest form of the check,
            # independent of whether the model chose to relay anything.
            b_memories = _get_memories(chat_client_b)
            assert not any(gem_a in (m.get("value") or "") for m in b_memories), (
                f"user B's GET /api/memories includes user A's stored value: {b_memories}"
            )
        finally:
            if key_a:
                _delete_memory(chat_client_a, key_a)

    def test_second_users_preference_not_visible_to_first_user(self, chat_client_a, chat_client_b):
        nonce_b = uuid.uuid4().hex[:10]
        color_b = f"virdex-{nonce_b}"
        key_b = None
        try:
            store_final = _send_message(
                chat_client_b,
                f"Please remember that my favorite color is {color_b}.",
            )
            attachment_b = _memory_attachment(store_final)
            assert attachment_b, (
                f"user B: no memory-tool write: {store_final.get('responseMessage')}"
            )
            key_b = attachment_b.get("key")
            _wait_for_memory(chat_client_b, color_b)

            a_final = _send_message(
                chat_client_a,
                "What is my favorite color? Reply with just the value, or say you "
                "don't know if you have no memory of it.",
            )
            a_reply = _reply_text(a_final)
            assert color_b not in a_reply, (
                f"user A's reply contained user B's stored preference -- memory "
                f"leaked across users. A's reply: {a_reply!r}"
            )

            a_memories = _get_memories(chat_client_a)
            assert not any(color_b in (m.get("value") or "") for m in a_memories), (
                f"user A's GET /api/memories includes user B's stored value: {a_memories}"
            )
        finally:
            if key_b:
                _delete_memory(chat_client_b, key_b)
