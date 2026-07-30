"""enterpriseaiframework-2a6: a user finds an earlier conversation BY WHAT WAS SAID IN
IT, not by scrolling — and one user cannot surface another's conversation that way.

CHECKED BEFORE BUILDING ANYTHING (as the item required): LibreChat v0.8.7 ships this
natively. `GET /api/messages?search=<term>` (api/server/routes/messages.js) runs
`db.searchMessages` -> `Message.meiliSearch` (packages/data-schemas/src/methods/
message.ts, the mongoMeili mongoose plugin at
packages/data-schemas/src/models/plugins/mongoMeili.ts) — a real-time index-on-save of
every message's `text` field into a self-hosted Meilisearch, queried with
`filter: user = "<searching user's id>"`. `GET /api/convos?search=<term>` does the
analogous thing over conversation titles. Turning this on is bundle/docker-compose.yml
(a `meilisearch` service, `SEARCH=true` + `MEILI_HOST` + `MEILI_MASTER_KEY` on `chat`) —
configuration and proof, not construction, per the item's own instruction.

LICENSE (checked, not assumed — the item asked this be checked carefully rather than
taken as "it's what upstream uses"): getmeili/meilisearch's engine is MIT
(LICENSE-MIT in that repo). A separate file, LICENSE-EE, covers Meilisearch's
Enterprise Edition extensions under BSL-1.1 — multi-tenant SSO/RBAC dashboards and
analytics add-ons this deployment does not use. The community search and filter API
this bundle runs (self-hosted, no seat/feature/user-count gate on the capability
exercised here) is what the compose comment and deploy/k8s/11-data.yaml comment record.

THE DONE CONDITION, literally: search for a distinctive phrase said in an earlier
conversation and find that conversation, with a fresh nonce so the result cannot be a
coincidence — proven through the real running surface, a real OIDC-authenticated
session, and a real chat turn. Then a SECOND real user, logged in as themselves, must
NOT find the first user's conversation searching the identical phrase — tested, not
asserted, exactly as the item's MUST HOLD requires.
"""

import uuid

import httpx
import pytest

import chat_turn
import oidc_login
from conftest import idp_admin_token

MODEL = "fake-large"
ENDPOINT_NAME = "Enterprise AI"
TIMEOUT = 180.0

# A second real Keycloak account, distinct from the bootstrap user, so the negative
# case is a real cross-account boundary rather than a second session for the same
# person. Named/created the same way tests/test_code_execution.py's cross-user
# isolation tests do — not imported from there because that helper is private to its
# own module by convention, per that file's own comment on the pattern.
SECOND_USER = "conversation-search-isolation-check"
SECOND_PASSWORD = "conversation-search-isolation-check-pw-1"


def _ensure_second_realm_user(env) -> None:
    """Create (or update) a second, real Keycloak user. Idempotent: a rerun of this
    test must not fail because a previous run already created the account."""
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
        "firstName": "Conversation-Search-Isolation-Check",
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
    logged in through the same authorization-code flow oidc_login.login() drives for
    the bootstrap user, not a synthesized header or claim."""
    _ensure_second_realm_user(env)
    client = oidc_login.login(chat_url, SECOND_USER, SECOND_PASSWORD)
    yield client
    client.close()


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


def _send_nonce_turn(client, chat_url, headers) -> tuple[str, str]:
    """One real chat turn whose human message carries a fresh, unguessable phrase.
    Returns (nonce, conversation_id).

    The message body is the bare nonce, nothing else — no shared boilerplate sentence
    around it. Meilisearch's default query matching is partial (OR-like across query
    tokens, not "all tokens must match"), so if every test turn wrapped its nonce in
    the same sentence, a search for one user's nonce could legitimately re-surface
    that SAME user's OTHER past turns via the shared wording alone — a false positive
    that looks like a cross-user leak but is neither (ownership scoping still held;
    the query just partially matched common vocabulary). A bare random hex string
    shares no tokens with any other bare random hex string, so a match can only be the
    conversation that actually said it.
    """
    nonce = uuid.uuid4().hex
    text = nonce
    payload = chat_turn.build_payload(text, MODEL, ENDPOINT_NAME)
    conversation_id, _ = chat_turn.start_turn(
        client, chat_url, payload, headers=headers, timeout=TIMEOUT
    )
    reply = chat_turn.wait_for_reply(
        client, chat_url, conversation_id, headers=headers, timeout=TIMEOUT
    )
    assert reply, "no assistant reply was persisted for this turn"
    return nonce, conversation_id


def _search_messages(client, chat_url, headers, term: str, retries: int = 15,
                     delay: float = 2.0) -> list[dict]:
    """GET /api/messages?search=<term>, polling briefly.

    mongoMeili indexes on save via a real network call to Meilisearch — not
    instantaneous, so this polls rather than asserting on the first response. What it
    never does is supply the answer itself: a search that never becomes non-empty
    fails the test rather than being padded out.
    """
    import time

    last: list[dict] = []
    deadline = time.monotonic() + retries * delay
    while time.monotonic() < deadline:
        r = client.get(
            f"{chat_url}/api/messages",
            params={"search": term}, headers=headers, timeout=30.0,
        )
        assert r.status_code == 200, f"search request failed: {r.status_code} {r.text[:300]}"
        last = r.json().get("messages", [])
        if last:
            return last
        time.sleep(delay)
    return last


class TestAUserFindsTheirOwnConversationByWhatWasSaidInIt:
    """The item's DONE condition, literally: a distinctive phrase said in an earlier
    turn, a fresh nonce so a match cannot be coincidence, through the real surface."""

    def test_searching_a_fresh_nonce_finds_the_conversation_it_was_said_in(
        self, chat_session, chat_url
    ):
        headers = _chat_headers(chat_session, chat_url)
        nonce, conversation_id = _send_nonce_turn(chat_session, chat_url, headers)

        results = _search_messages(chat_session, chat_url, headers, nonce)
        assert results, (
            f"searching for the fresh nonce {nonce!r} returned no messages at all — "
            "conversation search did not find the turn that said it"
        )
        matching = [m for m in results if m.get("conversationId") == conversation_id]
        assert matching, (
            f"search for {nonce!r} returned results, but none carry the conversationId "
            f"({conversation_id}) the phrase was actually said in: {results}"
        )

    def test_searching_a_phrase_never_said_finds_nothing(self, chat_session, chat_url):
        """The companion claim: search is not simply returning every conversation.
        A nonce that was never sent in any turn must come back empty."""
        headers = _chat_headers(chat_session, chat_url)
        never_said = f"NEVER-SAID-{uuid.uuid4().hex}"
        results = _search_messages(
            chat_session, chat_url, headers, never_said, retries=3, delay=1.0
        )
        assert results == [], (
            f"a phrase that was never sent in any conversation returned results: {results}"
        )


class TestSearchIsScopedToTheSearchingUser:
    """MUST HOLD, from the item: results are scoped to the searching user, and the
    negative case is tested rather than asserted. Two real Keycloak accounts, two real
    OIDC sessions, one real chat turn each."""

    def test_a_second_real_user_cannot_find_the_first_users_conversation(
        self, chat_session, second_chat_session, chat_url
    ):
        first_headers = _chat_headers(chat_session, chat_url)
        nonce, conversation_id = _send_nonce_turn(chat_session, chat_url, first_headers)

        # Confirm the owner can find it first — if this fails, the negative case below
        # would pass for the wrong reason (search broken entirely, not scoped).
        owner_results = _search_messages(chat_session, chat_url, first_headers, nonce)
        assert any(m.get("conversationId") == conversation_id for m in owner_results), (
            f"the owning user could not find their own nonce {nonce!r} — search is "
            "broken, so the isolation check below would prove nothing"
        )

        second_headers = _chat_headers(second_chat_session, chat_url)
        intruder_results = _search_messages(
            second_chat_session, chat_url, second_headers, nonce, retries=5, delay=1.0
        )
        # The precise claim, not merely "no results at all": no hit may carry the
        # victim conversation's id or the victim's own nonce text. An intruder who
        # happens to have unrelated matches of their own (not possible here, since
        # second_chat_session is fresh, but asserted this way regardless of that) is
        # a different question from "did this user see the other user's data".
        assert not any(
            m.get("conversationId") == conversation_id for m in intruder_results
        ), (
            f"a second real user's search surfaced the FIRST user's conversation "
            f"({conversation_id}) via its own nonce {nonce!r}: {intruder_results} — "
            "conversation search is not scoped to the searching user"
        )
        assert not any(nonce in (m.get("text") or "") for m in intruder_results), (
            f"a second real user's search returned the FIRST user's exact nonce text "
            f"{nonce!r}: {intruder_results}"
        )

    def test_each_user_finds_their_own_conversation_search_is_not_globally_broken(
        self, chat_session, second_chat_session, chat_url
    ):
        """The other half of the same claim, symmetric: the second user's own phrase,
        searched by the second user, must still be found. Without this, the isolation
        test above could pass merely because search always returns nothing."""
        second_headers = _chat_headers(second_chat_session, chat_url)
        their_nonce, their_conversation_id = _send_nonce_turn(
            second_chat_session, chat_url, second_headers
        )

        own_results = _search_messages(
            second_chat_session, chat_url, second_headers, their_nonce
        )
        assert any(
            m.get("conversationId") == their_conversation_id for m in own_results
        ), (
            f"the second user could not find their own nonce {their_nonce!r} via "
            f"search: {own_results}"
        )

        # And the first (bootstrap) user must not see it either — the precise claim
        # (this specific conversation/text), not "zero results altogether": the
        # bootstrap user's session has its OWN accumulated nonce turns from earlier
        # tests in this module, and a bare-hex nonce is disjoint from those by
        # construction, but the assertion is written against the specific victim
        # conversation/text rather than emptiness so it stays correct even if that
        # changes.
        first_headers = _chat_headers(chat_session, chat_url)
        cross_results = _search_messages(
            chat_session, chat_url, first_headers, their_nonce, retries=5, delay=1.0
        )
        assert not any(
            m.get("conversationId") == their_conversation_id for m in cross_results
        ), (
            f"the bootstrap user's search surfaced the SECOND user's conversation "
            f"({their_conversation_id}): {cross_results} — search leaks across users "
            "in both directions, not just one"
        )
        assert not any(their_nonce in (m.get("text") or "") for m in cross_results), (
            f"the bootstrap user's search returned the SECOND user's exact nonce "
            f"text {their_nonce!r}: {cross_results}"
        )
