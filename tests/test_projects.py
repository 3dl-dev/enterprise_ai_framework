"""enterpriseaiframework-084: a Project groups conversations and carries shared knowledge
and instructions all of them see.

WHAT V0.8.7 SHIPS NATIVELY, CHECKED BEFORE BUILDING ANYTHING (per the item's own
constraint). Reading the running image (not the docs) at
`packages/data-schemas/src/schema/chatProject.ts`, `.../methods/chatProject.ts`,
`api/server/routes/projects.js` and `api/server/controllers/agents/v1.js`:

  - `ChatProject` IS a real, shipped primitive: a Mongo collection with `name`,
    `description`, and a `user` field every query filters on
    (`ChatProject.findOne({_id, user})`), reachable at `/api/projects`. Conversations
    join it via `Conversation.chatProjectId`, set through the dedicated
    `PUT /api/projects/conversations/:conversationId` endpoint, which re-resolves the
    target project through the SAME owner-scoped query before allowing the move
    (`assignConversationToProject` in that file: a `projectId` the caller does not own
    throws `Project not found`, not silently succeeds). This is the grouping half of the
    item, and it needed no code — only proof, which is what the isolation classes below
    are.
  - `ChatProject` carries NO knowledge and NO instructions of its own — no such fields
    exist on its schema. v0.8.7's native carrier for "instructions + attached file_search
    knowledge, reusable across conversations" is a wholly different, ALSO-native
    primitive: `Agent` (`instructions`, `tools`, `tool_resources.file_search.file_ids`,
    ACL-checked on both `/api/agents/:id` and the chat-turn route via
    `canAccessAgentFromBody`). There is no native binding between the two — nothing in
    the shipped schema lets a `ChatProject` name a default Agent, and this is a locked
    upstream image (`ghcr.io/danny-avila/librechat:v0.8.7`, not built from source in this
    repo — see bundle/docker-compose.yml's `chat:` comment on why patching it is
    forbidden by "integrate, do not reimplement"), so that binding cannot be added inside
    LibreChat itself.

SO WHAT THIS ITEM ACTUALLY BUILDS: nothing patched, nothing reimplemented. A Project is
composed from two native, unmodified LibreChat primitives — `ChatProject` for grouping,
one `Agent` per project for its knowledge (instructions text plus file_search's existing
owner-scoped file pipeline from enterpriseaiframework-c7c, reused rather than duplicated)
— and the composition is exercised end to end below: attach knowledge to the project's
agent, start brand-new conversations against that agent, assign them into the project,
and prove the knowledge reached them. The caller (a real browser in production, this
test file here) supplies `agent_id` when starting each new conversation "in" the
project, the same way any real client must select which persisted Agent to talk to —
that is not a shortcut this suite invented, it is the actual native mechanism, and is
recorded as a finding in the item's return: no packaged LibreChat UI auto-selects a
project's Agent from `chatProjectId` alone, because nothing in the shipped schema
carries that link for it to read.

SCOPING IS THE LOAD-BEARING PART (finding 27: an identity the CALLER asserts in a
request body is not evidence; only one the control plane resolves from the
authenticated session is). Every isolation test below is driven by a SECOND, REAL,
independently OIDC-authenticated Keycloak user — same pattern
tests/test_file_search.py and tests/test_scope_items.py already established — attempting
to reach the FIRST user's project, conversation-move, or knowledge agent by naming it
directly in a request. Each negative is paired with a positive control proving the
miss is about ownership, not a broken pipeline for the second user in general;
without that pairing a negative result would be meaningless (file_search.py's own stated
reasoning, reused here for the same reason).
"""

import uuid

import httpx
import pytest

import chat_turn
import file_upload
import oidc_login
from conftest import idp_admin_token

pytestmark = pytest.mark.usefixtures("stack_up")

TIMEOUT = 180.0
MODEL = "fake-large"
ENDPOINT_NAME = "Enterprise AI"


# ---------------------------------------------------------------------------
# Session plumbing, mirroring test_file_search.py / test_scope_items.py
# ---------------------------------------------------------------------------


def _chat_token(client, chat_url: str) -> str:
    r = client.post(
        f"{chat_url}/api/auth/refresh",
        headers={"Cookie": oidc_login._cookie_header(client)},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _chat_headers(client, chat_url: str) -> dict:
    # User-Agent unconditionally: /api/agents and /api/projects refuse a non-browser
    # User-Agent with "Illegal request" (dogfood-findings.md finding 19), same as the
    # chat-turn and upload routes chat_turn.BROWSER_UA already guards.
    return {
        "Authorization": f"Bearer {_chat_token(client, chat_url)}",
        "Cookie": oidc_login._cookie_header(client),
        "User-Agent": chat_turn.BROWSER_UA,
    }


SECOND_USER = "projects-isolation-check"
SECOND_PASSWORD = "projects-isolation-check-pw-1"


def _ensure_second_realm_user(env) -> None:
    """Kept as its own copy, independent of test_file_search.py's identically-shaped
    helper, by the same module-privacy convention that file follows: an independent
    username so the two suites' cross-user checks cannot interfere with each other's
    sessions or artifacts."""
    idp = f"http://localhost:{env.get('IDP_PORT', '8082')}"
    realm = env.get("IDP_REALM", "enterprise-ai")
    headers = {"Authorization": f"Bearer {idp_admin_token(env)}"}

    found = httpx.get(
        f"{idp}/admin/realms/{realm}/users",
        headers=headers, params={"username": SECOND_USER, "exact": "true"}, timeout=30,
    ).json()

    profile = {
        "username": SECOND_USER,
        "email": f"{SECOND_USER}@example.invalid",
        "firstName": "Projects-Isolation-Check",
        "lastName": "User",
        "enabled": True,
        "emailVerified": True,
        "requiredActions": [],
    }

    if not found:
        r = httpx.post(
            f"{idp}/admin/realms/{realm}/users",
            headers={**headers, "Content-Type": "application/json"}, json=profile, timeout=30,
        )
        assert r.status_code == 201, r.text
        found = httpx.get(
            f"{idp}/admin/realms/{realm}/users",
            headers=headers, params={"username": SECOND_USER, "exact": "true"}, timeout=30,
        ).json()
    else:
        r = httpx.put(
            f"{idp}/admin/realms/{realm}/users/{found[0]['id']}",
            headers={**headers, "Content-Type": "application/json"}, json=profile, timeout=30,
        )
        r.raise_for_status()

    user_id = found[0]["id"]
    r = httpx.put(
        f"{idp}/admin/realms/{realm}/users/{user_id}/reset-password",
        headers={**headers, "Content-Type": "application/json"},
        json={"type": "password", "value": SECOND_PASSWORD, "temporary": False},
        timeout=30,
    )
    r.raise_for_status()


@pytest.fixture(scope="module")
def second_chat_session(chat_url, env):
    """A REAL second OIDC-authenticated chat session — a different Keycloak account,
    logged in through the same authorization-code flow oidc_login.login() drives for the
    bootstrap user, not a synthesized header or claim."""
    _ensure_second_realm_user(env)
    client = oidc_login.login(chat_url, SECOND_USER, SECOND_PASSWORD)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Project / Agent helpers
# ---------------------------------------------------------------------------


def _create_project(client, chat_url, headers, name: str) -> dict:
    r = client.post(
        f"{chat_url}/api/projects", json={"name": name}, headers=headers, timeout=TIMEOUT,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _delete_project(client, chat_url, headers, project_id: str) -> None:
    client.delete(f"{chat_url}/api/projects/{project_id}", headers=headers, timeout=TIMEOUT)


def _create_knowledge_agent(client, chat_url, headers, name: str, instructions: str,
                             file_ids: list[str] | None = None) -> str:
    """A persisted Agent carrying standing instructions and, optionally, file_search
    knowledge — v0.8.7's native carrier for both (see module docstring)."""
    payload = {
        "provider": ENDPOINT_NAME,
        "model": MODEL,
        "name": name,
        "instructions": instructions,
    }
    if file_ids:
        payload["tools"] = ["file_search"]
        payload["tool_resources"] = {"file_search": {"file_ids": file_ids}}
    r = client.post(f"{chat_url}/api/agents", json=payload, headers=headers, timeout=TIMEOUT)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _delete_agent(client, chat_url, headers, agent_id: str) -> None:
    client.delete(f"{chat_url}/api/agents/{agent_id}", headers=headers, timeout=TIMEOUT)


def _new_agent_conversation(client, chat_url, headers, agent_id: str, text: str) -> dict:
    """Start a BRAND NEW conversation (no parent, no prior history) against a persisted
    Agent and return the persisted assistant reply. This is "a new conversation in the
    project" for every grounding test below: new, not a continuation."""
    payload = {
        "text": text,
        "endpoint": "agents",
        "endpointType": "agents",
        "agent_id": agent_id,
        "sender": "User",
        "isCreatedByUser": True,
        "messageId": str(uuid.uuid4()),
        "parentMessageId": chat_turn.NO_PARENT,
        "conversationId": None,
        "error": False,
        "isContinued": False,
        "isTemporary": False,
        "isRegenerate": False,
    }
    conversation_id, _ = chat_turn.start_turn(
        client, chat_url, payload, headers=headers, timeout=TIMEOUT,
    )
    reply = chat_turn.wait_for_reply(
        client, chat_url, conversation_id, headers=headers, timeout=TIMEOUT,
    )
    return {"conversation_id": conversation_id, "reply": reply}


def _upload_nonce_document(client, chat_url, headers) -> tuple[str, str]:
    """Upload a small text file carrying a fresh nonce, marked for file_search. Returns
    (nonce, file_id) — same shape as test_file_search.py's identical helper."""
    nonce = uuid.uuid4().hex
    content = (
        f"Internal memo. The secret activation code is {nonce}. Keep it confidential."
    ).encode()
    filename = f"memo-{nonce[:8]}.txt"
    record = file_upload.upload_file(
        client, chat_url, headers, filename, content, mimetype="text/plain",
    )
    assert record.get("file_id"), f"upload response carried no file_id: {record}"
    assert record.get("embedded") is True, (
        f"upload did not report embedded=true: {record}"
    )
    return nonce, record["file_id"]


def _assign_conversation(client, chat_url, headers, conversation_id: str, project_id: str):
    return client.put(
        f"{chat_url}/api/projects/conversations/{conversation_id}",
        json={"projectId": project_id}, headers=headers, timeout=TIMEOUT,
    )


@pytest.fixture(scope="module")
def fakeprovider_url(env) -> str:
    return f"http://localhost:{env.get('FAKEPROVIDER_PORT', '8090')}"


def _prompts_containing(fakeprovider_url: str, nonce: str) -> list[dict]:
    """Ground truth for "did this text actually reach the upstream request" — the same
    seam tests/test_skill_corpus.py and TestModelPickerAndReasoningEffort in
    test_scope_items.py already rely on, reused for the same reason: a reply digest alone
    cannot prove WHAT reached the model, only that something did."""
    r = httpx.get(
        f"{fakeprovider_url}/debug/prompts", params={"contains": nonce}, timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Grouping: the native ChatProject primitive, exercised end to end
# ---------------------------------------------------------------------------


class TestProjectGroupsConversations:
    def test_a_conversation_can_be_created_then_added_to_a_project(self, chat_session, chat_url):
        headers = _chat_headers(chat_session, chat_url)
        project = _create_project(
            chat_session, chat_url, headers, f"grouping-check-{uuid.uuid4().hex[:8]}",
        )
        try:
            reply_msg = chat_turn.send_turn(
                chat_session, chat_url, "hello, this belongs in the project",
                model=MODEL, endpoint=ENDPOINT_NAME, headers=headers, timeout=TIMEOUT,
            )
            assert reply_msg, "no assistant message was persisted for the setup turn"
            conversation_id = reply_msg["conversationId"]

            assign = _assign_conversation(
                chat_session, chat_url, headers, conversation_id, project["_id"],
            )
            assert assign.status_code == 200, assign.text
            assert assign.json()["conversation"]["chatProjectId"] == project["_id"]

            fetched = chat_session.get(
                f"{chat_url}/api/projects/{project['_id']}", headers=headers, timeout=TIMEOUT,
            )
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["lastConversationId"] == conversation_id, (
                "the project's own stats did not pick up the conversation that was just "
                "added to it — grouping did not actually take effect server-side"
            )
        finally:
            _delete_project(chat_session, chat_url, headers, project["_id"])


class TestProjectIsolation:
    """Finding 27, tested rather than trusted: a SECOND real user, never the same
    session with a different claim."""

    def test_a_second_user_cannot_read_the_first_users_project(
        self, chat_session, second_chat_session, chat_url,
    ):
        headers_a = _chat_headers(chat_session, chat_url)
        project = _create_project(
            chat_session, chat_url, headers_a, f"iso-read-{uuid.uuid4().hex[:8]}",
        )
        try:
            headers_b = _chat_headers(second_chat_session, chat_url)
            r = second_chat_session.get(
                f"{chat_url}/api/projects/{project['_id']}", headers=headers_b, timeout=TIMEOUT,
            )
            assert r.status_code == 404, (
                f"user B fetched user A's project directly by id — expected 404, got "
                f"{r.status_code}: {r.text}"
            )

            # Positive control: A can read A's own project, so the 404 above is about
            # ownership, not a broken projects endpoint.
            own = chat_session.get(
                f"{chat_url}/api/projects/{project['_id']}", headers=headers_a, timeout=TIMEOUT,
            )
            assert own.status_code == 200, own.text
        finally:
            _delete_project(chat_session, chat_url, headers_a, project["_id"])

    def test_the_first_users_project_never_appears_in_the_second_users_listing(
        self, chat_session, second_chat_session, chat_url,
    ):
        headers_a = _chat_headers(chat_session, chat_url)
        project = _create_project(
            chat_session, chat_url, headers_a, f"iso-list-{uuid.uuid4().hex[:8]}",
        )
        try:
            headers_b = _chat_headers(second_chat_session, chat_url)
            listing = second_chat_session.get(
                f"{chat_url}/api/projects", headers=headers_b,
                params={"limit": 100}, timeout=TIMEOUT,
            )
            assert listing.status_code == 200, listing.text
            ids = [p["_id"] for p in listing.json().get("projects", [])]
            assert project["_id"] not in ids, (
                f"user A's project {project['_id']} appeared in user B's own project "
                f"listing"
            )
        finally:
            _delete_project(chat_session, chat_url, headers_a, project["_id"])

    def test_a_second_user_cannot_rename_or_delete_the_first_users_project(
        self, chat_session, second_chat_session, chat_url,
    ):
        """The rest of the CRUD surface, not just the read/list paths above: `PATCH`
        and `DELETE` both re-run the same owner-scoped lookup
        (`{_id: projectId, user}`) before touching anything, so a caller naming another
        user's `projectId` gets a 404 rather than an accidental mutation."""
        headers_a = _chat_headers(chat_session, chat_url)
        project = _create_project(
            chat_session, chat_url, headers_a, f"iso-crud-{uuid.uuid4().hex[:8]}",
        )
        try:
            headers_b = _chat_headers(second_chat_session, chat_url)

            renamed = second_chat_session.patch(
                f"{chat_url}/api/projects/{project['_id']}",
                json={"name": "hijacked-by-b"}, headers=headers_b, timeout=TIMEOUT,
            )
            assert renamed.status_code == 404, (
                f"user B renamed user A's project — expected 404, got "
                f"{renamed.status_code}: {renamed.text}"
            )

            deleted = second_chat_session.delete(
                f"{chat_url}/api/projects/{project['_id']}", headers=headers_b, timeout=TIMEOUT,
            )
            assert deleted.status_code == 404, (
                f"user B deleted user A's project — expected 404, got "
                f"{deleted.status_code}: {deleted.text}"
            )

            # Ground truth: A's project still exists, still named what A named it —
            # neither forged mutation above silently took effect.
            still_there = chat_session.get(
                f"{chat_url}/api/projects/{project['_id']}", headers=headers_a, timeout=TIMEOUT,
            )
            assert still_there.status_code == 200, still_there.text
            assert still_there.json()["name"] == project["name"], (
                "user A's project name changed even though B's rename was refused"
            )
        finally:
            _delete_project(chat_session, chat_url, headers_a, project["_id"])

    def test_a_second_user_cannot_move_their_own_conversation_into_the_first_users_project(
        self, chat_session, second_chat_session, chat_url,
    ):
        """The sharper version of finding 27 for this endpoint: `projectId` in the
        request body is CALLER-ASSERTED. If `assignConversationToProject` trusted it
        instead of re-resolving it through an owner-scoped query, user B could file
        their own conversation into user A's project — not reading A's data, but
        planting B's traffic inside a container A believes is exclusively theirs.
        """
        headers_a = _chat_headers(chat_session, chat_url)
        project_a = _create_project(
            chat_session, chat_url, headers_a, f"iso-assign-{uuid.uuid4().hex[:8]}",
        )
        headers_b = _chat_headers(second_chat_session, chat_url)
        project_b = None
        try:
            reply_b = chat_turn.send_turn(
                second_chat_session, chat_url, "B's own conversation",
                model=MODEL, endpoint=ENDPOINT_NAME, headers=headers_b, timeout=TIMEOUT,
            )
            assert reply_b, "no assistant message was persisted for B's setup turn"
            conversation_id_b = reply_b["conversationId"]

            forged = _assign_conversation(
                second_chat_session, chat_url, headers_b, conversation_id_b, project_a["_id"],
            )
            assert forged.status_code == 404, (
                f"user B moved their own conversation into user A's project — expected "
                f"a 404 ('Project not found', from the owner-scoped re-resolution), got "
                f"{forged.status_code}: {forged.text}"
            )

            # Positive control: the SAME conversation, moved into a project B actually
            # owns, must succeed — proving the 404 above is about A's ownership of
            # project_a, not the assign endpoint being broken for B in general.
            project_b = _create_project(
                second_chat_session, chat_url, headers_b, f"iso-assign-b-{uuid.uuid4().hex[:8]}",
            )
            genuine = _assign_conversation(
                second_chat_session, chat_url, headers_b, conversation_id_b, project_b["_id"],
            )
            assert genuine.status_code == 200, (
                f"user B could not move their OWN conversation into their OWN project — "
                f"the assign endpoint is broken for B entirely, which would make the "
                f"negative result above meaningless rather than an isolation success: "
                f"{genuine.text}"
            )
        finally:
            _delete_project(chat_session, chat_url, headers_a, project_a["_id"])
            if project_b:
                _delete_project(second_chat_session, chat_url, headers_b, project_b["_id"])


# ---------------------------------------------------------------------------
# Knowledge: a project's Agent, and new conversations that demonstrably use it
# ---------------------------------------------------------------------------


class TestProjectKnowledgeGrounding:
    """Done condition: attach knowledge (a file, standing instructions) to a project,
    then prove a NEW conversation in that project surfaces content that could only have
    come from the attached material — not merely that a reply arrived."""

    def test_a_new_conversation_is_grounded_in_the_projects_attached_file(
        self, chat_session, chat_url,
    ):
        headers = _chat_headers(chat_session, chat_url)
        project = _create_project(
            chat_session, chat_url, headers, f"knowledge-file-{uuid.uuid4().hex[:8]}",
        )
        agent_id = None
        try:
            nonce, file_id = _upload_nonce_document(chat_session, chat_url, headers)
            agent_id = _create_knowledge_agent(
                chat_session, chat_url, headers,
                name=f"project-knowledge-{uuid.uuid4().hex[:8]}",
                instructions="You are this project's assistant.",
                file_ids=[file_id],
            )

            result = _new_agent_conversation(
                chat_session, chat_url, headers, agent_id,
                "SEARCH_FILES:secret activation code",
            )
            assign = _assign_conversation(
                chat_session, chat_url, headers, result["conversation_id"], project["_id"],
            )
            assert assign.status_code == 200, assign.text

            text = chat_turn.reply_text(result["reply"])
            assert nonce in text, (
                f"expected the nonce planted in the project's attached document "
                f"({nonce!r}) in a brand-new conversation's reply — fakeprovider cannot "
                f"produce it itself, so its presence is only possible if file_search "
                f"actually retrieved the project's knowledge file; got {text!r}"
            )
        finally:
            if agent_id:
                _delete_agent(chat_session, chat_url, headers, agent_id)
            _delete_project(chat_session, chat_url, headers, project["_id"])

    def test_a_new_conversation_carries_the_projects_standing_instructions(
        self, chat_session, chat_url, fakeprovider_url,
    ):
        headers = _chat_headers(chat_session, chat_url)
        project = _create_project(
            chat_session, chat_url, headers, f"knowledge-instr-{uuid.uuid4().hex[:8]}",
        )
        agent_id = None
        try:
            instruction_nonce = uuid.uuid4().hex
            agent_id = _create_knowledge_agent(
                chat_session, chat_url, headers,
                name=f"project-instructions-{uuid.uuid4().hex[:8]}",
                instructions=(
                    f"Standing house rule for this project. Codeword: {instruction_nonce}."
                ),
            )

            # Deliberately unrelated to the instructions text — this must not be a
            # marker turn fakeprovider echoes back; the claim is that the instructions
            # reached the UPSTREAM REQUEST, checked directly via fakeprovider's own
            # capture, not inferred from the visible reply.
            result = _new_agent_conversation(
                chat_session, chat_url, headers, agent_id, "what is 2 plus 2",
            )
            assign = _assign_conversation(
                chat_session, chat_url, headers, result["conversation_id"], project["_id"],
            )
            assert assign.status_code == 200, assign.text

            captured = _prompts_containing(fakeprovider_url, instruction_nonce)
            assert captured, (
                f"the project's standing instructions (codeword {instruction_nonce!r}) "
                f"never reached the upstream request for a brand-new conversation "
                f"started against the project's agent"
            )
        finally:
            if agent_id:
                _delete_agent(chat_session, chat_url, headers, agent_id)
            _delete_project(chat_session, chat_url, headers, project["_id"])

    def test_a_second_new_conversation_in_the_project_also_sees_the_knowledge(
        self, chat_session, chat_url,
    ):
        """"All of them see it" (the item's own wording) means more than one — a second,
        independently-started new conversation against the same project must be grounded
        too, not just the first conversation that happened to attach the file."""
        headers = _chat_headers(chat_session, chat_url)
        project = _create_project(
            chat_session, chat_url, headers, f"knowledge-multi-{uuid.uuid4().hex[:8]}",
        )
        agent_id = None
        try:
            nonce, file_id = _upload_nonce_document(chat_session, chat_url, headers)
            agent_id = _create_knowledge_agent(
                chat_session, chat_url, headers,
                name=f"project-knowledge-multi-{uuid.uuid4().hex[:8]}",
                instructions="You are this project's assistant.",
                file_ids=[file_id],
            )

            for _ in range(2):
                result = _new_agent_conversation(
                    chat_session, chat_url, headers, agent_id,
                    "SEARCH_FILES:secret activation code",
                )
                assign = _assign_conversation(
                    chat_session, chat_url, headers, result["conversation_id"], project["_id"],
                )
                assert assign.status_code == 200, assign.text
                text = chat_turn.reply_text(result["reply"])
                assert nonce in text, (
                    f"a new conversation in the project was not grounded in the "
                    f"project's attached file (nonce {nonce!r} missing); got {text!r}"
                )
        finally:
            if agent_id:
                _delete_agent(chat_session, chat_url, headers, agent_id)
            _delete_project(chat_session, chat_url, headers, project["_id"])


class TestProjectKnowledgeIsolation:
    """The same finding-27 discipline applied to the KNOWLEDGE half, not just the
    grouping half: a project's Agent carries files and instructions that must be exactly
    as private as the project itself."""

    def test_a_second_user_cannot_use_the_first_users_project_agent(
        self, chat_session, second_chat_session, chat_url,
    ):
        headers_a = _chat_headers(chat_session, chat_url)
        nonce, file_id = _upload_nonce_document(chat_session, chat_url, headers_a)
        agent_id = _create_knowledge_agent(
            chat_session, chat_url, headers_a,
            name=f"knowledge-iso-{uuid.uuid4().hex[:8]}",
            instructions="Private project instructions.",
            file_ids=[file_id],
        )
        try:
            headers_b = _chat_headers(second_chat_session, chat_url)
            payload = {
                "text": "SEARCH_FILES:secret activation code",
                "endpoint": "agents",
                "endpointType": "agents",
                "agent_id": agent_id,
                "sender": "User",
                "isCreatedByUser": True,
                "messageId": str(uuid.uuid4()),
                "parentMessageId": chat_turn.NO_PARENT,
                "conversationId": None,
                "error": False,
                "isContinued": False,
                "isTemporary": False,
                "isRegenerate": False,
            }
            url = f"{chat_url}/api/agents/chat/agents"
            request_headers = {**headers_b, "Accept": "text/event-stream"}
            resp = second_chat_session.post(
                url, json=payload, headers=request_headers, timeout=TIMEOUT,
            )
            assert resp.status_code == 403, (
                f"user B used user A's private project agent in a chat turn — expected "
                f"403 ('Insufficient permissions'), got {resp.status_code}: {resp.text}"
            )
            assert nonce not in resp.text, (
                "user A's file content leaked into user B's refused-request response"
            )

            direct = second_chat_session.get(
                f"{chat_url}/api/agents/{agent_id}", headers=headers_b, timeout=TIMEOUT,
            )
            assert direct.status_code == 403, (
                f"user B could read user A's private project agent's own configuration "
                f"directly — expected 403, got {direct.status_code}: {direct.text}"
            )
        finally:
            _delete_agent(chat_session, chat_url, headers_a, agent_id)

    def test_the_negative_result_is_about_agent_ownership_not_a_broken_pipeline(
        self, second_chat_session, chat_url,
    ):
        """Positive control for the test above: user B, using B's OWN project agent and
        B's OWN uploaded file, gets grounded too — so the refusal above is specifically
        about A's agent belonging to A, not file_search/agents being broken for B."""
        headers_b = _chat_headers(second_chat_session, chat_url)
        nonce, file_id = _upload_nonce_document(second_chat_session, chat_url, headers_b)
        agent_id = _create_knowledge_agent(
            second_chat_session, chat_url, headers_b,
            name=f"knowledge-iso-b-{uuid.uuid4().hex[:8]}",
            instructions="B's own project instructions.",
            file_ids=[file_id],
        )
        try:
            result = _new_agent_conversation(
                second_chat_session, chat_url, headers_b, agent_id,
                "SEARCH_FILES:secret activation code",
            )
            text = chat_turn.reply_text(result["reply"])
            assert nonce in text, (
                f"user B could not get grounded in user B's OWN project agent's "
                f"knowledge — the pipeline is broken for B entirely, which would make "
                f"the cross-user negative result above meaningless. Reply was: {text!r}"
            )
        finally:
            _delete_agent(second_chat_session, chat_url, headers_b, agent_id)
