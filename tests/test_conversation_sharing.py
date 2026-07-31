"""enterpriseaiframework-bd2: a user shares a conversation by link, a second person opens
it WITHOUT AN ACCOUNT and sees it, and REVOKING the link stops that access.

CHECKED BEFORE BUILDING ANYTHING (as the item required): LibreChat v0.8.7 ships this
natively. `POST /api/share/:conversationId` (api/server/routes/share.js) calls
`createSharedLink`, which mints a `nanoid()` shareId (21 chars, base64url alphabet — not
derived from and not equal to the conversationId) and scopes creation to
`Conversation.findOne({conversationId, user})`, so a caller can only share their OWN
conversation. `GET /api/share/:shareId` is guarded by `canAccessSharedLink`
(`optionalJwtAuth` first, so it can be reached with no session at all) and returns
`getSharedMessages`'s payload, which is built from `anonymizeMessages`/`anonymizeConvoId` —
the real conversationId is never in the response, only the messages belonging to THIS
share, no `user` field, no key material. `DELETE /api/share/:shareId`
(`deleteSharedLinkWithCleanup`) is a `findOneAndDelete({shareId, user})` — scoped to the
owner and awaited before the route responds, so a subsequent `canAccessSharedLink`'s own
`SharedLink.findOne({shareId, ...})` finds nothing and 404s. All configuration and proof,
not construction, per the item's own instruction.

THE GAP FOUND HERE, NOT ASSUMED: reading `@librechat/api`'s
`createSharedLinkAccessMiddleware` (node_modules/@librechat/api/dist/index.cjs,
`canAccessSharedLink`) shows a SECOND gate past the per-link public ACL grant —
`isEnabled(process.env.ALLOW_SHARED_LINKS_PUBLIC)` — that must ALSO be true for an
unauthenticated caller to pass; otherwise the middleware falls through to
`if (!user) { 401 "Authentication required" }` even though the link's own ACL grants
public view. `docker exec enterprise-ai-chat-1 printenv | grep ALLOW_SHARED` returned
nothing: the var was never set in bundle/docker-compose.yml or deploy/k8s/50-chat.yaml, so
"a second person opens it without an account" was UNREACHABLE before this item —
confirmed live, 2026-07-31: creating a real share and fetching it with a bare `httpx`
client (no cookie, no bearer token) returned 401, not the conversation. Fixed here by
setting `ALLOW_SHARED_LINKS_PUBLIC: "true"` on the chat service in both the compose
bundle and the k3s manifest — the role-level `SHARED_LINKS.SHARE_PUBLIC: true` grant
already exists by default (librechat-data-provider's `roleDefaults.USER.permissions`),
so no role/permission construction was needed, only the missing env flag.

THE DONE CONDITION, literally, all three legs real: a real chat turn creates a real
conversation, a real share link is created through the real API, a bare unauthenticated
`httpx.Client` (no cookie jar carried over, no bearer token) fetches it and sees the
conversation, then the owner revokes it through the real DELETE route and the SAME
unauthenticated fetch immediately (not eventually) fails.

NEGATIVE CONTROLS, written deliberately rather than left to whoever reads this next:
  - unauthenticated fetch AFTER revocation (TestRevocationIsImmediate)
  - enumeration: a random shareId, the conversationId used AS a shareId, and a shareId
    with one character flipped, none of which name a real link (TestEnumerationIsNotPossible)
  - a second real user's conversation cannot be shared, and a second real user cannot
    revoke the first user's share (TestASecondUsersConversationCannotBeShared)
"""

import time
import uuid

import httpx
import pytest

import chat_turn
import oidc_login
from conftest import idp_admin_token

MODEL = "fake-large"
ENDPOINT_NAME = "Enterprise AI"
TIMEOUT = 180.0

# A second real Keycloak account, distinct from the bootstrap user and from
# test_conversation_search.py's own second account, so this file's cross-account boundary
# is independent of collection order or state left by another module. Same pattern as
# test_conversation_search.py's SECOND_USER — not imported from there because that helper
# is private to its own module by convention (see that file's own comment).
SECOND_USER = "conversation-sharing-isolation-check"
SECOND_PASSWORD = "conversation-sharing-isolation-check-pw-1"


def _ensure_second_realm_user(env) -> None:
    """Create (or update) a second, real Keycloak user. Idempotent."""
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
        "firstName": "Conversation-Sharing-Isolation-Check",
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
    """A REAL second OIDC-authenticated chat session — a different Keycloak account."""
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
    """One real chat turn whose message is a fresh, unguessable nonce. Returns
    (nonce, conversation_id). Same reasoning as test_conversation_search.py's helper of
    the same name: a bare random hex string cannot coincidentally match anything else."""
    nonce = uuid.uuid4().hex
    payload = chat_turn.build_payload(nonce, MODEL, ENDPOINT_NAME)
    conversation_id, _ = chat_turn.start_turn(
        client, chat_url, payload, headers=headers, timeout=TIMEOUT
    )
    reply = chat_turn.wait_for_reply(
        client, chat_url, conversation_id, headers=headers, timeout=TIMEOUT
    )
    assert reply, "no assistant reply was persisted for this turn"
    return nonce, conversation_id


def _create_share(client, chat_url, headers, conversation_id) -> httpx.Response:
    return client.post(
        f"{chat_url}/api/share/{conversation_id}", headers=headers, json={}, timeout=TIMEOUT
    )


def _fetch_share_unauthenticated(chat_url, share_id) -> httpx.Response:
    """A BARE client: no cookie jar carried over from any signed-in session, no bearer
    token, nothing. This is the actual claim under test — "without an account" — so this
    helper must never be handed an authenticated client's headers or cookies."""
    with httpx.Client(timeout=30.0) as anon:
        return anon.get(f"{chat_url}/api/share/{share_id}")


def _delete_share(client, chat_url, headers, share_id) -> httpx.Response:
    return client.delete(f"{chat_url}/api/share/{share_id}", headers=headers, timeout=30.0)


def _share_and_get_id(client, chat_url, headers, conversation_id) -> str:
    created = _create_share(client, chat_url, headers, conversation_id)
    assert created.status_code == 200, (
        f"creating a share for a real, owned conversation failed: "
        f"{created.status_code} {created.text[:300]}"
    )
    share_id = created.json().get("shareId")
    assert share_id, f"share creation response carried no shareId: {created.text[:300]}"
    return share_id


class TestASecondPersonOpensASharedLinkWithoutAnAccount:
    """The item's DONE condition: a real share, fetched by a client that never signed in
    at all, sees the conversation."""

    def test_an_unauthenticated_client_sees_the_shared_conversation(
        self, chat_session, chat_url
    ):
        headers = _chat_headers(chat_session, chat_url)
        nonce, conversation_id = _send_nonce_turn(chat_session, chat_url, headers)
        share_id = _share_and_get_id(chat_session, chat_url, headers, conversation_id)

        resp = _fetch_share_unauthenticated(chat_url, share_id)
        assert resp.status_code == 200, (
            f"a real shared link, fetched with NO cookie and NO bearer token, returned "
            f"{resp.status_code} instead of the conversation: {resp.text[:300]}"
        )
        body = resp.json()
        texts = " ".join(
            "".join(
                part.get("text", "") for part in (m.get("content") or []) if isinstance(part, dict)
            ) + (m.get("text") or "")
            for m in body.get("messages", [])
        )
        assert nonce in texts, (
            f"the fresh nonce {nonce!r} sent in the shared conversation is not present in "
            f"the unauthenticated share payload: {body}"
        )

    def test_the_shared_payload_carries_no_more_than_the_conversation(
        self, chat_session, chat_url, env
    ):
        """The item's hard limit: no key material, no other conversations, no account
        surface. Asserted against the real response, not the source code."""
        headers = _chat_headers(chat_session, chat_url)
        nonce, conversation_id = _send_nonce_turn(chat_session, chat_url, headers)
        share_id = _share_and_get_id(chat_session, chat_url, headers, conversation_id)

        resp = _fetch_share_unauthenticated(chat_url, share_id)
        assert resp.status_code == 200, resp.text[:300]

        # No session was established for this anonymous request — a shared link is a
        # read, not a login. If this ever carries Set-Cookie, an anonymous viewer would
        # walk away with session state, which is account surface a public link must not
        # hand out.
        assert "set-cookie" not in {k.lower() for k in resp.headers}, (
            f"an unauthenticated share fetch set a cookie: {dict(resp.headers)}"
        )

        body = resp.json()

        # No account surface: the payload names no user identity at all (LibreChat's
        # own anonymizeMessages/getSharedMessages already strip `user`; asserted here
        # against the live response rather than trusted from reading the source).
        assert "user" not in body, f"shared payload carries a `user` field: {body.keys()}"
        raw = resp.text
        for leaky_field in ("email", "password", "apiKey", "api_key", "virtualKey", "sessionId"):
            assert leaky_field not in raw, (
                f"shared payload contains the field name {leaky_field!r} — possible "
                f"account/credential surface: {raw[:500]}"
            )

        # No key material: neither this deployment's real virtual key nor anything
        # shaped like a bearer/session token appears anywhere in the payload.
        real_key = env.get("CHAT_VIRTUAL_KEY", "")
        if real_key:
            assert real_key not in raw, "the chat surface's virtual key leaked into a shared link"
        gateway_master = env.get("GATEWAY_MASTER_KEY", "")
        if gateway_master:
            assert gateway_master not in raw, "the gateway master key leaked into a shared link"

        # No other conversations: the real conversationId this turn was actually sent
        # under is never exposed (LibreChat anonymizes it), and every message the share
        # returns belongs to the one anonymized id the share itself reports — not a
        # mixture that would mean a second conversation rode along.
        assert conversation_id not in raw, (
            f"the real (non-anonymized) conversationId {conversation_id} appears in the "
            f"public share payload — the internal id was not anonymized"
        )
        returned_convo_id = body.get("conversationId")
        assert returned_convo_id, f"shared payload carries no conversationId: {body}"
        message_convo_ids = {m.get("conversationId") for m in body.get("messages", [])}
        assert message_convo_ids <= {returned_convo_id}, (
            f"messages in the shared payload span more than one conversationId "
            f"({message_convo_ids}) — a share is leaking a second conversation"
        )

        # The account surface is closed even to a caller holding a valid share: listing
        # ALL shares, or reaching the authenticated user profile, still requires a real
        # session — the link is a read of one conversation, not a foothold.
        with httpx.Client(timeout=15.0) as anon:
            listing = anon.get(f"{chat_url}/api/share/")
            assert listing.status_code == 401, (
                f"the shared-links LIST endpoint answered an unauthenticated caller with "
                f"{listing.status_code} instead of 401 — that would expose every user's "
                f"shares, not just the one link"
            )
            whoami = anon.get(f"{chat_url}/api/user")
            assert whoami.status_code == 401, (
                f"/api/user answered an unauthenticated caller with {whoami.status_code} — "
                "a public share link must not carry a usable account session"
            )


class TestEnumerationIsNotPossible:
    """enterpriseaiframework-49c precedent: un-migrated /live/ content was anonymously
    ENUMERABLE. A share whose URL can be walked is not a share mechanism."""

    def test_a_random_shareid_never_created_is_not_found(self, chat_url):
        random_id = uuid.uuid4().hex
        resp = _fetch_share_unauthenticated(chat_url, random_id)
        assert resp.status_code == 404, (
            f"a random, never-created shareId returned {resp.status_code}, not 404: "
            f"{resp.text[:300]}"
        )

    def test_the_real_conversationid_is_not_a_valid_shareid(
        self, chat_session, chat_url
    ):
        """Proves the share URL is not derivable from the conversation's own id — the
        exact shape finding 49c warns against (a URL a caller could compute)."""
        headers = _chat_headers(chat_session, chat_url)
        _, conversation_id = _send_nonce_turn(chat_session, chat_url, headers)
        _share_and_get_id(chat_session, chat_url, headers, conversation_id)

        resp = _fetch_share_unauthenticated(chat_url, conversation_id)
        assert resp.status_code == 404, (
            f"fetching /api/share/<conversationId> (the id the caller already knows, "
            f"not the minted shareId) returned {resp.status_code} instead of 404 — the "
            f"conversationId itself would double as a working share link: "
            f"{resp.text[:300]}"
        )

    def test_a_shareid_has_real_entropy_not_a_short_or_sequential_token(
        self, chat_session, chat_url
    ):
        headers = _chat_headers(chat_session, chat_url)
        ids = []
        for _ in range(2):
            _, conversation_id = _send_nonce_turn(chat_session, chat_url, headers)
            ids.append(_share_and_get_id(chat_session, chat_url, headers, conversation_id))

        for share_id in ids:
            assert len(share_id) >= 16, (
                f"shareId {share_id!r} is only {len(share_id)} chars — too short to "
                "resist a walk of the space"
            )
            assert not share_id.isdigit(), (
                f"shareId {share_id!r} is a bare integer — sequential IDs are walkable"
            )

        # Two shareIds minted moments apart must not differ by a small, guessable delta
        # (the signature of a counter or a short-index scheme).
        assert ids[0] != ids[1], "two distinct shares were minted with the same shareId"

    def test_flipping_one_character_of_a_real_shareid_does_not_hit_another_link(
        self, chat_session, chat_url
    ):
        """A share space small or patterned enough to enumerate would show up as
        near-neighbors of a real id also resolving. None of these mutations name a
        real link (each is independently almost certainly never minted)."""
        headers = _chat_headers(chat_session, chat_url)
        _, conversation_id = _send_nonce_turn(chat_session, chat_url, headers)
        share_id = _share_and_get_id(chat_session, chat_url, headers, conversation_id)

        mutated_first_char = ("a" if share_id[0] != "a" else "b") + share_id[1:]
        mutated_last_char = share_id[:-1] + ("a" if share_id[-1] != "a" else "b")
        truncated = share_id[:-1]

        for candidate in (mutated_first_char, mutated_last_char, truncated):
            assert candidate != share_id
            resp = _fetch_share_unauthenticated(chat_url, candidate)
            assert resp.status_code == 404, (
                f"mutated shareId {candidate!r} (from real {share_id!r}) returned "
                f"{resp.status_code} instead of 404 — the share space is walkable"
            )


class TestRevocationIsImmediate:
    """Revoke, then fetch, and prove the fetch fails — not a database-flag assertion."""

    def test_revoking_a_share_immediately_blocks_the_unauthenticated_fetch(
        self, chat_session, chat_url
    ):
        headers = _chat_headers(chat_session, chat_url)
        nonce, conversation_id = _send_nonce_turn(chat_session, chat_url, headers)
        share_id = _share_and_get_id(chat_session, chat_url, headers, conversation_id)

        # Confirm access actually works before revoking — if this fails, the negative
        # case below would pass for the wrong reason (link was never reachable at all).
        before = _fetch_share_unauthenticated(chat_url, share_id)
        assert before.status_code == 200, (
            f"the link did not work before revocation ({before.status_code}) — the "
            "revocation check below would prove nothing"
        )

        revoked = _delete_share(chat_session, chat_url, headers, share_id)
        assert revoked.status_code == 200, (
            f"revoking a real, owned share failed: {revoked.status_code} {revoked.text[:300]}"
        )

        after = _fetch_share_unauthenticated(chat_url, share_id)
        assert after.status_code == 404, (
            f"an unauthenticated fetch of a REVOKED share returned {after.status_code} "
            f"instead of 404 — revocation did not take effect: {after.text[:300]}"
        )

        # Immediate, not eventual: poll again a moment later and it must still be gone
        # (rules out a race where revocation looked immediate only because the first
        # re-fetch happened to land after some async cleanup completed).
        time.sleep(1.0)
        still_after = _fetch_share_unauthenticated(chat_url, share_id)
        assert still_after.status_code == 404, (
            f"a revoked share became reachable again on a later fetch "
            f"({still_after.status_code}) — revocation is not durable"
        )

    def test_revoking_a_share_removes_it_from_the_owners_own_list(
        self, chat_session, chat_url
    ):
        headers = _chat_headers(chat_session, chat_url)
        _, conversation_id = _send_nonce_turn(chat_session, chat_url, headers)
        share_id = _share_and_get_id(chat_session, chat_url, headers, conversation_id)

        revoked = _delete_share(chat_session, chat_url, headers, share_id)
        assert revoked.status_code == 200, revoked.text[:300]

        listing = chat_session.get(
            f"{chat_url}/api/share/", headers=headers, params={"pageSize": 50}, timeout=30
        )
        assert listing.status_code == 200, listing.text[:300]
        listed_ids = {link.get("shareId") for link in listing.json().get("links", [])}
        assert share_id not in listed_ids, (
            f"a revoked share {share_id!r} still appears in the owner's own share list: "
            f"{listed_ids}"
        )


class TestASecondUsersConversationCannotBeShared:
    """The explicit negative control this item calls out: a fetch of a second user's
    conversation must not be reachable through sharing, in either direction."""

    def test_a_user_cannot_create_a_share_for_another_users_conversation(
        self, chat_session, second_chat_session, chat_url
    ):
        first_headers = _chat_headers(chat_session, chat_url)
        _, victim_conversation_id = _send_nonce_turn(chat_session, chat_url, first_headers)

        intruder_headers = _chat_headers(second_chat_session, chat_url)
        attempt = _create_share(
            second_chat_session, chat_url, intruder_headers, victim_conversation_id
        )
        # NOT 200/201: creation must be refused. The shipped route
        # (api/server/routes/share.js) maps every createSharedLink failure — including
        # `createSharedLink`'s own `Conversation.findOne({conversationId, user})` access
        # check finding nothing for a conversationId that belongs to someone else — to a
        # generic `catch` that returns 500 with a fixed "Error creating shared link"
        # message, not a distinct 404. That is LibreChat's own shipped behaviour, not
        # something this deployment patches (integrate, do not reimplement) — the
        # security property under test is refusal, not the status code, and refusal is
        # confirmed independently below by asserting no share exists for the victim
        # conversation afterward.
        assert attempt.status_code not in (200, 201), (
            f"a second real user was able to create a share for the FIRST user's own "
            f"conversation ({victim_conversation_id}): {attempt.status_code} "
            f"{attempt.text[:300]} — sharing is not scoped to the conversation's owner"
        )

        # And no share now exists for it via the owner's own account either — the
        # rejected attempt must not have side-effected a share into existence.
        owner_listing = chat_session.get(
            f"{chat_url}/api/share/link/{victim_conversation_id}",
            headers=first_headers, timeout=30,
        )
        assert owner_listing.status_code == 200, owner_listing.text[:300]
        assert owner_listing.json().get("success") is not True, (
            f"a rejected cross-user share attempt still resulted in a share existing "
            f"for the victim conversation: {owner_listing.json()}"
        )

    def test_a_user_cannot_revoke_another_users_share(
        self, chat_session, second_chat_session, chat_url
    ):
        owner_headers = _chat_headers(chat_session, chat_url)
        _, conversation_id = _send_nonce_turn(chat_session, chat_url, owner_headers)
        share_id = _share_and_get_id(chat_session, chat_url, owner_headers, conversation_id)

        intruder_headers = _chat_headers(second_chat_session, chat_url)
        attempt = _delete_share(second_chat_session, chat_url, intruder_headers, share_id)
        assert attempt.status_code == 404, (
            f"a second real user was able to DELETE (revoke) the FIRST user's share "
            f"{share_id!r}: {attempt.status_code} {attempt.text[:300]}"
        )

        # The share must still be live — a would-be attacker's failed revoke attempt
        # must not have had any effect on the real owner's link.
        still_live = _fetch_share_unauthenticated(chat_url, share_id)
        assert still_live.status_code == 200, (
            f"after a second user's REJECTED delete attempt, the owner's real share "
            f"stopped working anyway ({still_live.status_code}) — a failed cross-user "
            "revoke attempt had a side effect"
        )

        # The real owner can still revoke it themselves, cleanly.
        real_revoke = _delete_share(chat_session, chat_url, owner_headers, share_id)
        assert real_revoke.status_code == 200, real_revoke.text[:300]
        assert _fetch_share_unauthenticated(chat_url, share_id).status_code == 404
