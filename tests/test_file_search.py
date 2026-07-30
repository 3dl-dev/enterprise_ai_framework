"""enterpriseaiframework-c7c: upload a document, ask about it, get a grounded answer —
scoped to the uploader, with the embedding call on the one bill.

THIS IS CONFIGURATION AND PROOF, NOT CONSTRUCTION, per the item: v0.8.7 already ships
file_search in the served agents capability list, enabled by default
(`defaultAgentCapabilities` in librechat-data-provider). What was actually MISSING —
found on the running cluster as `warn: RAG API is either not running or not reachable at
undefined` — is the backing service LibreChat's file_search tool talks to. This wires it:
a dedicated Postgres/pgvector instance (`ragvector`) plus rag_api itself (`rag-api`,
danny-avila/rag_api, MIT, no seat/revenue/feature trigger — see bundle/docker-compose.yml
for the full reasoning), with its embedding calls routed through OUR gateway rather than
any provider directly.

FOUR THINGS, each proven rather than assumed:

1. SCOPED TO THE UPLOADER (TestScopedToTheUploader). A SECOND REAL Keycloak user,
   independently OIDC-authenticated (the same live authorization-code flow
   test_code_execution.py's cross-user tests use), tries to reference the first user's
   file_id in a real chat turn and gets nothing back. Design §3.4's pattern — partition
   by construction — holds at TWO independent layers here, both traced to LibreChat's
   and rag_api's own source before this test was written (not merely reasoned about):
     - LibreChat: `packages/data-schemas/src/methods/file.ts#updateFileUsage` resolves
       every `files[].file_id` in the request body through an OWNER-SCOPED query
       (`withOwnerScope({file_id}, {userId: req.user.id, ...})`) — "Owner scoping is
       fail-closed" per that function's own comment. A file_id naming someone else's
       document simply does not resolve to an attachment; primeResources never sees it.
     - rag_api: `/query`'s own handler independently re-checks `doc_metadata.user_id`
       against the AUTHENTICATED caller (`request.state.user["id"]`, from a JWT
       `security_middleware` verifies with the shared `JWT_SECRET` — HS256, signature
       checked, not merely decoded) and 403s a mismatch. Even a bug in LibreChat's own
       check would still hit this second, independent gate.
   Both derive identity from something the CONTROL PLANE (LibreChat's own
   session-authenticated backend, or a signature it produced) asserts — never from
   anything a caller's request body claims. Finding 27 applies with full force, and is
   tested live below rather than inherited from reading the vendored source.

2. THE EMBEDDING CALL IS BILLABLE (TestEmbeddingIsBilledThroughTheGateway), established
   BY MEASUREMENT: rag-api's `RAG_OPENAI_BASEURL` points at the gateway, so an upload's
   embedding call should land a `LiteLLM_SpendLogs` row under `rag-api::file-search` —
   asserted directly against the gateway's own ledger table, not inferred from config.
   What this does NOT prove, stated plainly: rag_api's embeddings client is one
   process-wide instance (`app/config.py`'s module-level `init_embeddings(...)`) with no
   per-request `user` field, so every upload's embedding call is billed under this ONE
   shared service principal — a known, attributable credential, same shape as chat's own
   shared `CHAT_VIRTUAL_KEY` — but NOT attributed to the individual uploading human the
   way chat spend is (chat forwards the LibreChat Mongo user id as `end_user` on every
   completion; rag_api has no equivalent hook without patching its source, which
   "integrate, do not reimplement" forbids). Recorded as a finding in the item's return,
   not silently built past.

3. NO PROVIDER-HOSTED RETRIEVAL SERVICE. Storage and retrieval are pgvector inside our
   own `ragvector` container on this compose network; nothing in the data path is a
   hosted vector service. `rag-api` publishes no host port, same posture as
   webfetch/rerank.

4. ANSWERS ARE GROUNDED, NOT PLAUSIBLE (TestGroundedAnswer). Same shape as the
   web-search citation work (0be): a fresh nonce is planted in the uploaded document and
   the model's reply must contain it. fakeprovider cannot compute or guess a nonce it has
   never seen — see fakeprovider/app.py's `SEARCH_FILES:` marker and the generic
   tool-result relay it shares with `EXECUTE_BASH:`/`CALL_MCP_ECHO:` — so a nonce
   appearing in the persisted reply is only possible if file_search actually retrieved
   the document through the real rag_api service, over the real network, backed by the
   real embedding call in (2).
"""

import time
import uuid

import pytest

import chat_turn
import file_upload
import oidc_login
from conftest import compose, idp_admin_token

pytestmark = pytest.mark.usefixtures("stack_up")

TIMEOUT = 180.0
MODEL = "fake-large"
ENDPOINT_NAME = "Enterprise AI"
RAG_ALIAS = "rag-api::file-search"

# chat_session (conftest.py) only carries the OIDC session cookie — LibreChat's own API
# additionally wants a short-lived bearer minted from that session
# (packages/api's refresh route). Duplicated from test_code_execution.py's private
# _chat_token/_chat_headers by the same module-privacy convention that file follows.


def _chat_token(client, chat_url: str) -> str:
    r = client.post(
        f"{chat_url}/api/auth/refresh",
        headers={"Cookie": oidc_login._cookie_header(client)},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _chat_headers(client, chat_url: str) -> dict:
    return {
        "Authorization": f"Bearer {_chat_token(client, chat_url)}",
        "Cookie": oidc_login._cookie_header(client),
    }


# ---------------------------------------------------------------------------
# A second, real Keycloak user — same admin-API pattern
# test_code_execution.py::_ensure_second_realm_user uses, kept independent (own
# username) so the two suites' cross-user checks cannot interfere with each other's
# sessions or artifacts.
# ---------------------------------------------------------------------------

SECOND_USER = "file-search-isolation-check"
SECOND_PASSWORD = "file-search-isolation-check-pw-1"


def _ensure_second_realm_user(env) -> None:
    idp = f"http://localhost:{env.get('IDP_PORT', '8082')}"
    realm = env.get("IDP_REALM", "enterprise-ai")
    headers = {"Authorization": f"Bearer {idp_admin_token(env)}"}

    import httpx

    found = httpx.get(
        f"{idp}/admin/realms/{realm}/users",
        headers=headers, params={"username": SECOND_USER, "exact": "true"}, timeout=30,
    ).json()

    profile = {
        "username": SECOND_USER,
        "email": f"{SECOND_USER}@example.invalid",
        "firstName": "File-Search-Isolation-Check",
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
# Shared turn/upload helpers
# ---------------------------------------------------------------------------


def _upload_nonce_document(client, chat_url, headers) -> tuple[str, str, dict]:
    """Upload a small text file carrying a fresh nonce, marked for file_search.
    Returns (nonce, file_id, the full upload response)."""
    nonce = uuid.uuid4().hex
    content = (
        f"Internal memo. The secret activation code is {nonce}. "
        "Keep it confidential."
    ).encode()
    filename = f"memo-{nonce[:8]}.txt"
    record = file_upload.upload_file(
        client, chat_url, headers, filename, content, mimetype="text/plain",
    )
    assert record.get("file_id"), f"upload response carried no file_id: {record}"
    assert record.get("embedded") is True, (
        f"upload did not report embedded=true — the RAG API pipeline (rag-api, "
        f"ragvector) did not accept the file, so nothing below can be grounded in it: "
        f"{record}"
    )
    return nonce, record["file_id"], record


def _search_turn(client, chat_url, headers, file_id: str, filename: str, file_type: str):
    return chat_turn.send_turn(
        client, chat_url,
        "SEARCH_FILES:secret activation code",
        model=MODEL, endpoint=ENDPOINT_NAME, file_search=True,
        files=[{"file_id": file_id, "filename": filename, "type": file_type}],
        headers=headers, timeout=TIMEOUT,
    )


class TestGroundedAnswer:
    """Done condition: "the answer contains a fact that could only have come from that
    file" — not merely that a response arrived."""

    def test_the_answer_contains_the_planted_nonce(self, chat_session, chat_url):
        headers = _chat_headers(chat_session, chat_url)
        nonce, file_id, record = _upload_nonce_document(chat_session, chat_url, headers)

        reply = _search_turn(
            chat_session, chat_url, headers, file_id,
            record["filename"], record.get("type", "text/plain"),
        )
        assert reply, "no assistant message was persisted for this turn"
        text = chat_turn.reply_text(reply)
        assert nonce in text, (
            f"expected the nonce planted in the uploaded document ({nonce!r}) in the "
            f"reply — fakeprovider cannot produce it itself (see fakeprovider/app.py's "
            f"SEARCH_FILES relay), so its presence here is only possible if file_search "
            f"actually retrieved the document through the real RAG API; got {text!r}"
        )


class TestScopedToTheUploader:
    """Requirement 1: a user must not be able to retrieve another user's document,
    tested with a second real user rather than reasoned about (Finding 27)."""

    def test_a_second_user_cannot_retrieve_the_first_users_document(
        self, chat_session, second_chat_session, chat_url,
    ):
        headers_a = _chat_headers(chat_session, chat_url)
        nonce, file_id, record = _upload_nonce_document(chat_session, chat_url, headers_a)

        headers_b = _chat_headers(second_chat_session, chat_url)
        reply = _search_turn(
            second_chat_session, chat_url, headers_b, file_id,
            record["filename"], record.get("type", "text/plain"),
        )
        assert reply, "no assistant message was persisted for user B's turn"
        text = chat_turn.reply_text(reply)
        assert nonce not in text, (
            f"user B's turn, referencing user A's file_id {file_id!r}, surfaced user "
            f"A's nonce ({nonce!r}) in the reply — a second user retrieved the first "
            f"user's document. Reply was: {text!r}"
        )

    def test_the_negative_result_is_about_ownership_not_a_broken_pipeline(
        self, second_chat_session, chat_url,
    ):
        """Positive control for the test above. Without this, a miss on the cross-user
        turn could equally mean file_search is broken for user B outright, which would
        make the negative result upstairs meaningless."""
        headers_b = _chat_headers(second_chat_session, chat_url)
        nonce, file_id, record = _upload_nonce_document(second_chat_session, chat_url, headers_b)

        reply = _search_turn(
            second_chat_session, chat_url, headers_b, file_id,
            record["filename"], record.get("type", "text/plain"),
        )
        assert reply, "no assistant message was persisted for user B's own-document turn"
        text = chat_turn.reply_text(reply)
        assert nonce in text, (
            f"user B could not retrieve user B's OWN document ({nonce!r}) — file_search "
            f"is broken for user B entirely, which would make the cross-user negative "
            f"result above meaningless rather than a scoping success. Reply was: {text!r}"
        )


class TestEmbeddingIsBilledThroughTheGateway:
    """Requirement 2, established by measurement, not assumption."""

    def _rag_alias_row_count(self, env) -> int:
        result = compose(
            "exec", "-T", "postgres", "psql", "-U", env.get("POSTGRES_USER", "eai"),
            "-d", "gateway", "-tA", "-c",
            "SELECT count(*) FROM \"LiteLLM_SpendLogs\" WHERE "
            f"metadata->>'user_api_key_alias' = '{RAG_ALIAS}'",
            check=False,
        )
        assert result.returncode == 0, f"psql failed\n{result.stdout}\n{result.stderr}"
        return int(result.stdout.strip() or 0)

    def test_an_upload_produces_a_ledger_row_under_the_rag_api_principal(
        self, chat_session, chat_url, env,
    ):
        assert env.get("RAG_VIRTUAL_KEY", "").strip(), (
            "RAG_VIRTUAL_KEY missing from bundle/.env — bin/provision-rag-key.sh runs "
            "as part of `make up`; without it rag-api holds no gateway credential and "
            "cannot spend at all, let alone attributably"
        )
        headers = _chat_headers(chat_session, chat_url)
        before = self._rag_alias_row_count(env)
        _upload_nonce_document(chat_session, chat_url, headers)

        # The gateway batches spend rows (flush_spend_on_shutdown.handler's own
        # comment: 7-13s buffering); poll rather than assume an immediate write.
        deadline = time.monotonic() + 60
        after = before
        while time.monotonic() < deadline:
            after = self._rag_alias_row_count(env)
            if after > before:
                break
            time.sleep(3)
        assert after > before, (
            f"expected this upload's embedding call to land a new LiteLLM_SpendLogs "
            f"row under alias {RAG_ALIAS!r} (before={before}, after={after}) — either "
            f"the embedding call did not go through the gateway at all, or it went "
            f"through under no attributable principal"
        )
