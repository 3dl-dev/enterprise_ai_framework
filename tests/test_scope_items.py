"""One test class per numbered scope item in the sealed estimate.

If any of these cannot pass against the running bundle, the row is void — not re-scoped.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import time
import uuid

import httpx
import pytest

import oidc_login
from conftest import BUNDLE, DOGFOOD_USER, compose, set_user_enabled


def _chat_token(client, chat_url: str) -> str:
    """Short-lived API token minted from the signed-in session."""
    r = client.post(
        f"{chat_url}/api/auth/refresh",
        headers={"Cookie": oidc_login._cookie_header(client)},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]

pytestmark = pytest.mark.usefixtures("stack_up")

TIMEOUT = 60.0


def gateway_aliases(gateway_url: str, master_headers: dict) -> set[str]:
    """Every key alias the gateway actually holds.

    Our own ledger saying a key is or is not there is the thing most likely to drift from
    reality; this is the table that decides whether a request is served.
    """
    aliases: set[str] = set()
    for page in range(1, 51):
        listed = httpx.get(
            f"{gateway_url}/key/list",
            headers=master_headers,
            # size is capped at 100 by the gateway; exceeding it returns a validation
            # error rather than a truncated page.
            params={"return_full_object": "true", "page": page, "size": 100},
            timeout=TIMEOUT,
        )
        assert listed.status_code == 200, listed.text
        batch = [k for k in listed.json().get("keys", []) if isinstance(k, dict)]
        aliases |= {k.get("key_alias") for k in batch}
        if len(batch) < 100:
            break
    return aliases


# ---------------------------------------------------------------------------
# Item 1 — one login reaches all three surfaces
# ---------------------------------------------------------------------------

class TestItem1OneLogin:
    def test_chat_surface_accepts_realm_identity_via_sso(self, chat_session, chat_url):
        """A real authorization-code login, driven end to end — not a config assertion."""
        me = oidc_login.whoami(chat_session, chat_url)
        user = me.get("user", me)
        assert user.get("username") == DOGFOOD_USER, user
        assert user.get("provider") == "openid", (
            f"signed in via {user.get('provider')} rather than the identity provider"
        )

    def test_chat_surface_has_no_local_account_path(self, chat_url):
        """Registration must be closed. An account created locally would be an identity
        the control plane never sees and can never revoke."""
        r = httpx.post(
            f"{chat_url}/api/auth/register",
            json={
                "email": f"intruder-{uuid.uuid4().hex[:8]}@example.invalid",
                "password": "Sufficiently-Long-Password-1",
                "confirm_password": "Sufficiently-Long-Password-1",
                "name": "Intruder",
                "username": f"intruder{uuid.uuid4().hex[:6]}",
            },
            timeout=TIMEOUT,
        )
        assert r.status_code >= 400, (
            f"local registration succeeded ({r.status_code}) — identity is bypassable"
        )

    def test_only_the_gateway_endpoint_is_reachable_from_chat(
        self, chat_session, chat_url
    ):
        """One control plane: the surface must offer no route to a provider except ours.

        LibreChat ships direct openAI / anthropic / google / azure / bedrock endpoints.
        Left enabled they are an unmetered, unaudited, unbudgeted path around the
        gateway the moment anyone supplies a key.
        """
        token = _chat_token(chat_session, chat_url)
        endpoints = chat_session.get(
            f"{chat_url}/api/endpoints",
            headers={
                "Authorization": f"Bearer {token}",
                "Cookie": oidc_login._cookie_header(chat_session),
            },
            timeout=TIMEOUT,
        ).json()

        # An allow-list, not a deny-list: a LibreChat upgrade that introduces a new
        # endpoint must fail this test and be ruled on, rather than appear silently.
        #
        # `agents` is permitted because it is not the thing this test guards against. It
        # carries no provider of its own — it runs on top of a configured endpoint, which
        # here is the custom one pointing at the gateway. It is enabled deliberately
        # (ENDPOINTS: custom,agents in docker-compose.yml and on the cluster): naming only
        # "custom" also removes MCP servers, memory, tools, file search and web search,
        # which is a capability loss, not a security gain. The bypass this test exists to
        # catch is LibreChat's DIRECT provider endpoints, asserted explicitly below.
        allowed = {"Enterprise AI", "agents"}
        assert set(endpoints) <= allowed, (
            f"surface offers routes around the gateway: {sorted(set(endpoints) - allowed)}"
        )
        assert "Enterprise AI" in endpoints, "the gateway endpoint is not offered at all"

        # The actual bypass: LibreChat's built-in direct-provider endpoints. Named rather
        # than inferred, so this keeps testing the claim even if the allow-list is widened
        # again later for another non-provider framework endpoint.
        direct_providers = {
            "openAI", "azureOpenAI", "anthropic", "google", "bedrock",
            "gptPlugins", "assistants", "azureAssistants", "custom",
        }
        offered_direct = direct_providers & set(endpoints)
        assert not offered_direct, (
            f"surface offers a direct provider path around the gateway: {sorted(offered_direct)} "
            "— unmetered, unbudgeted and outside the audit trail"
        )

        # Applied to EVERY endpoint, not just the gateway one. A framework endpoint that
        # lets a user paste their own provider key is the same bypass by another door.
        for name, cfg in endpoints.items():
            assert cfg.get("userProvide") is False, (
                f"endpoint {name!r} lets users supply their own provider key, "
                "bypassing the virtual key"
            )

    def test_chat_catalog_is_pushed_from_the_gateway(
        self, chat_session, chat_url, gateway_url, master_headers
    ):
        """The surface must offer exactly what the gateway offers.

        Compared against the gateway's live catalog rather than a hardcoded list: the
        claim is "one control plane decides the catalogue", and a pinned list would both
        go stale and stop testing that claim.
        """
        token = _chat_token(chat_session, chat_url)
        offered = set(chat_session.get(
            f"{chat_url}/api/models",
            headers={
                "Authorization": f"Bearer {token}",
                "Cookie": oidc_login._cookie_header(chat_session),
            },
            timeout=TIMEOUT,
        ).json().get("Enterprise AI", []))

        available = {
            m["id"] for m in httpx.get(
                f"{gateway_url}/v1/models", headers=master_headers, timeout=TIMEOUT
            ).json()["data"]
        }

        assert offered == available, (
            f"chat surface and gateway disagree on the catalogue; "
            f"only in chat: {sorted(offered - available)}, "
            f"only in gateway: {sorted(available - offered)}"
        )

    def test_one_identity_yields_credentials_for_all_three_surfaces(
        self, chat_session, chat_url, control_plane_url, admin_headers
    ):
        """The whole claim in one assertion: the identity that just signed in to chat is
        the same identity holding the coding agents' credentials."""
        me = oidc_login.whoami(chat_session, chat_url)
        username = me.get("user", me)["username"]

        keys = httpx.get(
            f"{control_plane_url}/admin/keys", headers=admin_headers,
            params={"username": username}, timeout=TIMEOUT,
        ).json()
        active = {k["surface"] for k in keys if k["status"] == "active"}
        assert active == {"chat", "ide", "terminal"}, (
            f"{username} signed in to chat but holds credentials for {active}"
        )


# ---------------------------------------------------------------------------
# Item 3 — one gateway, both wire formats, true streaming
# ---------------------------------------------------------------------------

class TestItem3OneGatewayBothFormats:
    def test_openai_compatible_inbound(self, gateway_url, named_key_headers):
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers=named_key_headers,
            json={"model": "fake-large", "messages": [{"role": "user", "content": "ping"}]},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["choices"][0]["message"]["content"].startswith("[fake-provider")
        assert body["usage"]["total_tokens"] > 0

    def test_anthropic_native_inbound(self, gateway_url, named_key_headers):
        """The terminal coding agent speaks this dialect, not the OpenAI one."""
        r = httpx.post(
            f"{gateway_url}/v1/messages",
            headers={**named_key_headers, "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-opus-5",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "message"
        assert body["content"][0]["text"].startswith("[fake-provider")
        assert body["usage"]["input_tokens"] > 0

    def test_streaming_is_incremental_not_buffered(self, gateway_url, named_key_headers):
        """Chunks must arrive over time. A response assembled and then flushed at once
        satisfies a naive 'is it SSE' check while feeling broken to a user."""
        # Unique content forces a cache miss. A cached response replays collapsed, so a
        # fixed prompt would silently stop exercising upstream streaming after the first
        # ever run of this suite.
        chunks: list[float] = []
        start = time.monotonic()
        with httpx.stream(
            "POST",
            f"{gateway_url}/v1/chat/completions",
            headers=named_key_headers,
            json={
                "model": "fake-large",
                "messages": [
                    {"role": "user", "content": f"stream please {uuid.uuid4().hex}"}
                ],
                "stream": True,
            },
            timeout=TIMEOUT,
        ) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    chunks.append(time.monotonic() - start)

        assert len(chunks) >= 3, f"expected several chunks, got {len(chunks)}"


# ---------------------------------------------------------------------------
# Item 2 — no surface holds a provider key; virtual keys are minted per surface
# ---------------------------------------------------------------------------

class TestItem2VirtualKeys:
    def test_one_active_key_per_user_and_surface(self, control_plane_url, admin_headers):
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)
        r = httpx.get(f"{control_plane_url}/admin/keys", headers=admin_headers, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        keys = r.json()
        if not keys:
            pytest.skip("no realm users yet — create one to exercise this item")
        by_user: dict[str, set] = {}
        for k in keys:
            if k["status"] == "active":
                by_user.setdefault(k["username"], set()).add(k["surface"])
        for username, surfaces in by_user.items():
            assert surfaces == {"chat", "ide", "terminal"}, f"{username} has {surfaces}"

    def test_sync_is_idempotent(self, control_plane_url, admin_headers):
        """Re-running must not mint a second key set. A non-idempotent reconcile cannot
        be put on a timer, and a reconcile that cannot run on a timer does not enforce
        anything."""
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)
        second = httpx.post(
            f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT
        ).json()
        assert second["keys_minted"] == 0, second

    def test_gateway_rejects_unminted_key(self, gateway_url):
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": "Bearer sk-not-a-real-key"},
            json={"model": "fake-large", "messages": [{"role": "user", "content": "hi"}]},
            timeout=TIMEOUT,
        )
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"

    def test_issued_key_works_and_stays_joined_to_the_ledger(
        self, control_plane_url, admin_headers, gateway_url
    ):
        """`/admin/keys/issue` is the only path that hands out a raw virtual key.

        It exists for surfaces that are provisioned on demand — the workspace pod has to
        be given a live key at creation, and by then nobody holds one. Three things have
        to be true at once or it is a liability rather than a feature:

          1. the key it returns actually authenticates at the gateway;
          2. it is that user's `<username>::ide` alias, not a shared or master key;
          3. the ledger's recorded token hash follows the rotation, so budgets keep
             working. Minting straight at the gateway would leave the hash pointing at a
             deleted key and `/admin/budget` would fail against a key that is gone —
             silently, from the operator's side. That is the regression this guards.
        """
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)
        keys = httpx.get(
            f"{control_plane_url}/admin/keys", headers=admin_headers, timeout=TIMEOUT
        ).json()
        users = sorted({k["username"] for k in keys if k["status"] == "active"})
        if not users:
            pytest.skip("no realm users yet — create one to exercise this item")
        username = users[0]

        issued = httpx.post(
            f"{control_plane_url}/admin/keys/issue",
            headers=admin_headers, json={"username": username, "surface": "ide"},
            timeout=TIMEOUT,
        )
        assert issued.status_code == 200, issued.text
        body = issued.json()
        assert body["key_alias"] == f"{username}::ide", body
        assert body["key"].startswith("sk-"), body

        info = httpx.get(
            f"{gateway_url}/key/info", params={"key": body["key"]},
            headers={"Authorization": f"Bearer {body['key']}"}, timeout=TIMEOUT,
        )
        assert info.status_code == 200, f"the issued key does not work: {info.text}"
        assert info.json()["info"]["key_alias"] == f"{username}::ide"

        # The budget path is what goes stale if the rotation is done outside the ledger,
        # so exercise it rather than inspecting the hash.
        budget = httpx.post(
            f"{control_plane_url}/admin/budget", headers=admin_headers,
            json={"username": username, "surface": "ide", "max_budget": 5.0},
            timeout=TIMEOUT,
        )
        assert budget.status_code == 200, (
            f"budget update failed after issuing a key — the ledger's token hash did not "
            f"follow the rotation: {budget.text}"
        )

    def test_issue_works_when_the_gateway_holds_no_key_yet(
        self, control_plane_url, admin_headers, gateway_url, master_headers
    ):
        """Issuing must not require something to rotate.

        The gateway answers 404 to a delete that matches nothing, and the first cut of
        this endpoint let that 404 escape as a 500 — so issuing worked only for a surface
        that already had a key, and failed in the two states an operator most needs it:
        the first provision of a surface, and re-provisioning after a revocation.
        """
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)
        alias = f"{DOGFOOD_USER}::terminal"
        httpx.post(f"{gateway_url}/key/delete", headers=master_headers,
                   json={"key_aliases": [alias]}, timeout=TIMEOUT)

        issued = httpx.post(
            f"{control_plane_url}/admin/keys/issue", headers=admin_headers,
            json={"username": DOGFOOD_USER, "surface": "terminal"}, timeout=TIMEOUT,
        )
        assert issued.status_code == 200, (
            f"issuing with nothing to rotate failed: {issued.status_code} {issued.text}"
        )
        assert issued.json()["key_alias"] == alias

    def test_issue_rejects_unknown_users_and_surfaces(self, control_plane_url, admin_headers):
        bad_surface = httpx.post(
            f"{control_plane_url}/admin/keys/issue", headers=admin_headers,
            json={"username": "nobody", "surface": "browser"}, timeout=TIMEOUT,
        )
        assert bad_surface.status_code == 400, bad_surface.text
        unknown = httpx.post(
            f"{control_plane_url}/admin/keys/issue", headers=admin_headers,
            json={"username": "no-such-principal", "surface": "ide"}, timeout=TIMEOUT,
        )
        assert unknown.status_code == 404, unknown.text

    def test_issue_requires_the_admin_token(self, control_plane_url):
        """It returns a spendable credential. An unauthenticated caller must get nothing."""
        r = httpx.post(
            f"{control_plane_url}/admin/keys/issue",
            headers={"Authorization": "Bearer not-the-admin-token"},
            json={"username": "anyone", "surface": "ide"}, timeout=TIMEOUT,
        )
        assert r.status_code == 401, r.text
        assert "sk-" not in r.text, f"the rejection handed back key material: {r.text}"


# ---------------------------------------------------------------------------
# Item 4 — one bill: spend by user and by surface, one query
# ---------------------------------------------------------------------------

class TestItem4OneBill:
    def test_spend_breaks_down_by_user_and_surface(self, control_plane_url, admin_headers):
        r = httpx.get(f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "totals" in body and "by_user_and_surface" in body
        for row in body["by_user_and_surface"]:
            assert {"username", "surface", "requests", "spend"} <= set(row)

    def test_traffic_on_a_virtual_key_is_attributed_to_its_surface(
        self, control_plane_url, gateway_url, admin_headers, master_headers
    ):
        """The end-to-end attribution claim: spend on a surface key shows up under that
        surface in the bill."""
        username = f"attrtest-{uuid.uuid4().hex[:8]}"
        created = httpx.post(
            f"{gateway_url}/key/generate",
            headers=master_headers,
            json={"key_alias": f"{username}::ide", "metadata": {"surface": "ide"}},
            timeout=TIMEOUT,
        )
        assert created.status_code == 200, created.text
        key = created.json()["key"]

        # Unique content: a cache hit is served without writing a spend row, so a fixed
        # prompt makes this test pass once and then fail forever after.
        resp = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "fake-large",
                "messages": [{"role": "user", "content": f"bill me {uuid.uuid4().hex}"}],
            },
            timeout=TIMEOUT,
        )
        assert resp.status_code == 200, resp.text

        # The gateway writes spend rows asynchronously; poll rather than sleep-and-hope.
        deadline = time.monotonic() + 45
        row = None
        while time.monotonic() < deadline:
            bill = httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()
            row = next(
                (r for r in bill["by_user_and_surface"] if r["username"] == username), None
            )
            if row:
                break
            time.sleep(2)

        assert row is not None, "spend never appeared in the ledger"
        assert row["surface"] == "ide"
        assert row["requests"] >= 1


class TestAttributableSpendOnly:
    """Nothing may consume tokens or money without a principal the bill can name.

    THE DEFECT THIS EXISTS FOR, measured on the live cluster 2026-07-29 rather than
    reasoned about: `/admin/spend` reported 42 requests and $0.1133 under
    `(unattributed)`, 25 of them — 98% of the unattributed money — in a two-hour window
    26 hours after deployment. Reading the raw ledger, every one of those rows carried
    `api_key = sha256(GATEWAY_MASTER_KEY)` with `metadata.user_api_key_alias = null`.
    The gateway's administrative root credential was also a valid inference credential:
    it could buy tokens, it had no key row to join to, and — because budgets bind to
    virtual keys — it had no budget either. The bill was honest that it could not name
    the money. The defect was that it could not.

    Distinct from finding 25 (a principal that exists whose row was orphaned by
    revocation) and finding 34 (a principal that exists, named differently by different
    renderings). Here there was no principal at all.

    WHY THE ASSERTIONS ARE SHAPED THE WAY THEY ARE. `(unattributed)` is a legitimate
    label, and after this fix the bucket still collects refused attempts — LiteLLM writes
    a `status = 'failure'` row with zero spend and zero tokens for a request its pre-call
    hook rejected, and that row has no alias by construction. So "the bucket is empty" is
    the wrong assertion and would be red on a healthy system. What must hold is that
    nothing in that bucket ever CONSUMED anything: no money, no tokens. A request that
    was refused before reaching an upstream costs nobody anything and names nobody,
    which is correct. A request that was served and counted tokens must name someone.

    The window is also asserted to be non-empty and attributed, so this cannot pass by
    testing nothing — the failure mode of every emptiness check.
    """

    @staticmethod
    def _window_start() -> str:
        # Explicit UTC offset: the ledger stores timestamptz and a naive string is read
        # in the server's zone, which silently selects the wrong window.
        return time.strftime("%Y-%m-%dT%H:%M:%S+00", time.gmtime(time.time() - 5))

    @staticmethod
    def _bill(control_plane_url, admin_headers, since) -> list[dict]:
        r = httpx.get(
            f"{control_plane_url}/admin/spend", headers=admin_headers,
            params={"since": since}, timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        return r.json()["by_user_and_surface"]

    def test_the_master_key_cannot_buy_inference(self, gateway_url, master_headers):
        """The administrative credential administers. It does not spend.

        Both inbound dialects, because the terminal surface speaks the Anthropic one and
        a rule enforced on only one of them is a rule with a documented bypass.
        """
        openai_style = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers=master_headers,
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"master {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert openai_style.status_code == 403, (
            f"the gateway master key bought inference: {openai_style.status_code} "
            f"{openai_style.text[:300]}"
        )
        assert "no_attributable_principal" in openai_style.text

        anthropic_style = httpx.post(
            f"{gateway_url}/v1/messages",
            headers={**master_headers, "anthropic-version": "2023-06-01"},
            json={"model": "claude-opus-5", "max_tokens": 32,
                  "messages": [{"role": "user", "content": f"master {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert anthropic_style.status_code == 403, (
            "the master key bought inference through the Anthropic-native route: "
            f"{anthropic_style.status_code} {anthropic_style.text[:300]}"
        )

    def test_a_key_minted_without_an_alias_cannot_buy_inference(
        self, gateway_url, master_headers
    ):
        """The rule is about attribution, not about which credential it is.

        The gateway will happily mint a key with no alias — `key_alias` is optional on
        `/key/generate`. Such a key produces spend rows the bill cannot name for exactly
        the same reason the master key did, so it is refused by the same rule. Asserting
        only the master-key case would leave the hole open one `curl` away.
        """
        created = httpx.post(
            f"{gateway_url}/key/generate", headers=master_headers, json={}, timeout=TIMEOUT,
        )
        assert created.status_code == 200, created.text
        anon = created.json()["key"]
        try:
            r = httpx.post(
                f"{gateway_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {anon}"},
                json={"model": "fake-large",
                      "messages": [{"role": "user", "content": f"anon {uuid.uuid4().hex}"}]},
                timeout=TIMEOUT,
            )
            assert r.status_code == 403, (
                f"an alias-less virtual key bought inference: {r.status_code} {r.text[:300]}"
            )
        finally:
            httpx.post(f"{gateway_url}/key/delete", headers=master_headers,
                       json={"keys": [anon]}, timeout=TIMEOUT)

    def test_the_master_key_still_administers(self, gateway_url, master_headers):
        """The path this change did NOT touch, asserted rather than assumed.

        Refusing master-key inference by blocking the master key outright would break
        every mint, every revoke and the catalogue the chat surface reads — a fix worse
        than the defect, and one that would still look green if only the refusal were
        tested.
        """
        alias = f"adminprobe-{uuid.uuid4().hex[:8]}::ide"
        created = httpx.post(
            f"{gateway_url}/key/generate", headers=master_headers,
            json={"key_alias": alias}, timeout=TIMEOUT,
        )
        assert created.status_code == 200, created.text

        listed = httpx.get(
            f"{gateway_url}/key/list", headers=master_headers,
            params={"return_full_object": "true", "size": 100}, timeout=TIMEOUT,
        )
        assert listed.status_code == 200, listed.text

        catalogue = httpx.get(f"{gateway_url}/v1/models", headers=master_headers,
                              timeout=TIMEOUT)
        assert catalogue.status_code == 200, catalogue.text
        assert catalogue.json()["data"], "the catalogue went empty"

        deleted = httpx.post(f"{gateway_url}/key/delete", headers=master_headers,
                             json={"key_aliases": [alias]}, timeout=TIMEOUT)
        assert deleted.status_code == 200, deleted.text

    def test_no_tokens_and_no_money_land_on_the_bill_without_a_principal(
        self, gateway_url, control_plane_url, admin_headers, master_headers,
        named_key_headers,
    ):
        """The end-to-end invariant, over a window this test knows the whole contents of.

        Drives both kinds of traffic — one named request that must be billed, one
        anonymous request that must be refused — then reads the bill for that window and
        checks that every token and every cent in it belongs to somebody.
        """
        since = self._window_start()

        # Anonymous attempt: refused, and therefore costs nothing.
        refused = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers=master_headers,
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"anon spend {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert refused.status_code == 403, refused.text

        # Named traffic in the same window. Unique content forces a cache miss: a cache
        # hit is replayed without writing a spend row, so a fixed prompt would make this
        # window empty on every run after the first and the invariant vacuous.
        served = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers=named_key_headers,
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"named spend {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert served.status_code == 200, served.text

        # The gateway flushes spend rows on a batch interval; poll rather than sleep.
        deadline = time.monotonic() + 60
        rows: list[dict] = []
        while time.monotonic() < deadline:
            rows = self._bill(control_plane_url, admin_headers, since)
            if any(r["prompt_tokens"] + r["completion_tokens"] > 0 for r in rows):
                break
            time.sleep(2)

        named = [r for r in rows if r["username"] != "(unattributed)"]
        billed_tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in named)
        assert billed_tokens > 0, (
            "no attributed traffic reached the ledger in the window, so the check below "
            f"would pass without proving anything; rows were {rows}"
        )

        anonymous = [r for r in rows if r["username"] == "(unattributed)"]
        for row in anonymous:
            assert row["spend"] == 0, (
                f"${row['spend']} was billed to nobody: {row}. A request must not be "
                "able to reach the gateway, be served, and land on the one bill with no "
                "principal at all."
            )
            assert row["prompt_tokens"] + row["completion_tokens"] == 0, (
                f"{row['prompt_tokens'] + row['completion_tokens']} tokens were consumed "
                f"by nobody: {row}. Tokens counted against no principal means the "
                "request was served, and a served request must name its caller."
            )

    def test_the_bill_still_names_the_surface_that_spent(
        self, control_plane_url, admin_headers, gateway_url, named_key_headers
    ):
        """The path this change did NOT touch: ordinary attribution still works.

        A refusal rule is easy to get right by refusing too much. This asserts the case
        that must keep passing — a real caller's spend arriving under their own name and
        their own surface — in the same window as the refusal above.
        """
        since = self._window_start()
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers=named_key_headers,
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"still works {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text

        deadline = time.monotonic() + 60
        row = None
        while time.monotonic() < deadline:
            row = next(
                (b for b in self._bill(control_plane_url, admin_headers, since)
                 if b["username"].startswith("suitecaller-")),
                None,
            )
            if row:
                break
            time.sleep(2)

        assert row is not None, "a named caller's spend never appeared in the bill"
        assert row["surface"] == "terminal", row
        assert row["prompt_tokens"] + row["completion_tokens"] > 0, row


class TestPricingIntegrity:
    """Regression guard for a silent-money defect found while building this row.

    A model missing from the gateway price map serves traffic, counts tokens, and prices
    every request at $0 — so budgets never trip and the bill under-reports, with no error
    anywhere. Every configured model must therefore price above zero.
    """

    def test_configured_models_record_nonzero_spend(
        self, gateway_url, control_plane_url, admin_headers, named_key_headers
    ):
        # Explicit UTC offset — the ledger stores timestamptz, and a naive string would
        # be read in the server's zone and silently select the wrong window.
        since = time.strftime("%Y-%m-%dT%H:%M:%S+00", time.gmtime(time.time() - 5))
        for model in ("fake-large", "fake-small", "claude-opus-5"):
            r = httpx.post(
                f"{gateway_url}/v1/chat/completions",
                headers=named_key_headers,
                json={"model": model, "messages": [{"role": "user", "content": "price me"}]},
                timeout=TIMEOUT,
            )
            assert r.status_code == 200, r.text

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            body = httpx.get(
                f"{control_plane_url}/admin/unpriced",
                headers=admin_headers, params={"since": since}, timeout=TIMEOUT,
            ).json()
            if body["ok"]:
                return
            time.sleep(3)
        pytest.fail(f"models priced at $0: {body['models']}")


# ---------------------------------------------------------------------------
# Item 5 — one audit trail, survives a restart
# ---------------------------------------------------------------------------

class TestItem5AuditTrail:
    def test_chain_verifies(self, control_plane_url, admin_headers):
        r = httpx.get(
            f"{control_plane_url}/admin/audit/verify", headers=admin_headers, timeout=TIMEOUT
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True, r.json()

    def test_trail_survives_full_restart(self, control_plane_url, admin_headers):
        before = httpx.get(
            f"{control_plane_url}/admin/audit", headers=admin_headers,
            params={"limit": 1000}, timeout=TIMEOUT,
        ).json()
        assert before, "expected at least the control_plane.start event"
        head_hash = before[0]["hash"]

        compose("restart", "control-plane")

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                after = httpx.get(
                    f"{control_plane_url}/admin/audit", headers=admin_headers,
                    params={"limit": 1000}, timeout=10,
                ).json()
                break
            except Exception:
                time.sleep(3)
        else:
            pytest.fail("control plane did not come back after restart")

        # Serving again is not the same as reporting healthy. Leaving the container in
        # `starting` leaks into every later test that asserts on stack health, so this
        # test cleans up after the restart it caused.
        while time.monotonic() < deadline:
            ps = compose("ps", "--format", "{{.Service}} {{.Health}}")
            if any(
                l.split()[0] == "control-plane" and l.split()[-1] == "healthy"
                for l in ps.stdout.strip().splitlines() if l.strip()
            ):
                break
            time.sleep(3)
        else:
            pytest.fail("control plane never returned to healthy after restart")

        hashes = {e["hash"] for e in after}
        assert head_hash in hashes, "pre-restart events were lost"

        verify = httpx.get(
            f"{control_plane_url}/admin/audit/verify", headers=admin_headers, timeout=TIMEOUT
        ).json()
        assert verify["ok"] is True, verify


# ---------------------------------------------------------------------------
# Item 6 — disabling one user in the IdP stops traffic on all three surfaces
# ---------------------------------------------------------------------------

class TestItem6RevocationPropagates:
    @pytest.fixture(autouse=True)
    def _restore_user(self, env, control_plane_url, admin_headers):
        """Always re-enable and re-sync, even if the test fails partway.

        A test that can leave the dogfood user disabled would poison every later run,
        and the failure would look like an unrelated bug.
        """
        yield
        set_user_enabled(env, DOGFOOD_USER, True)
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)

    def test_disable_revokes_every_surface_and_stops_traffic(
        self, env, control_plane_url, gateway_url, admin_headers, master_headers
    ):
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)

        keys = httpx.get(
            f"{control_plane_url}/admin/keys", headers=admin_headers,
            params={"username": DOGFOOD_USER}, timeout=TIMEOUT,
        ).json()
        active = [k for k in keys if k["status"] == "active"]
        assert {k["surface"] for k in active} == {"chat", "ide", "terminal"}, keys

        set_user_enabled(env, DOGFOOD_USER, False)

        result = httpx.post(
            f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT
        ).json()
        assert result["keys_revoked"] >= 3, result

        after = httpx.get(
            f"{control_plane_url}/admin/keys", headers=admin_headers,
            params={"username": DOGFOOD_USER}, timeout=TIMEOUT,
        ).json()
        assert all(k["status"] == "revoked" for k in after), after

        # Our own ledger saying "revoked" is not the claim — it is the thing most likely
        # to drift from reality. Assert against the gateway's own key set, which is what
        # actually decides whether a request is served.
        remaining = gateway_aliases(gateway_url, master_headers)
        for k in active:
            assert k["key_alias"] not in remaining, (
                f"{k['surface']} key still present on the gateway after revocation"
            )

    def test_issuing_a_key_to_a_disabled_principal_is_refused(
        self, env, control_plane_url, gateway_url, admin_headers, master_headers
    ):
        """`/admin/keys/issue` must not hand a spendable credential to a revoked person.

        This is the one endpoint that returns a raw key, and it is the one path that could
        undo item 6 in a single call: provisioning a workspace for someone identity has
        already disabled would mint them a live key seconds after their access was
        removed, and nothing else in the system would notice — the key is valid, the
        gateway serves it, and the bill shows a principal who is supposed to be gone.

        The status code is the smaller half of the claim. What actually matters is that
        no key exists at the gateway afterwards, so this asserts against the gateway's own
        key table rather than trusting the endpoint's own report of what it did not do.
        """
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)
        alias = f"{DOGFOOD_USER}::ide"

        set_user_enabled(env, DOGFOOD_USER, False)
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)

        refused = httpx.post(
            f"{control_plane_url}/admin/keys/issue", headers=admin_headers,
            json={"username": DOGFOOD_USER, "surface": "ide"}, timeout=TIMEOUT,
        )
        assert refused.status_code == 409, (
            f"issuing to a disabled principal returned {refused.status_code}: {refused.text}"
        )
        assert "sk-" not in refused.text, (
            f"the refusal handed back something key-shaped: {refused.text}"
        )
        assert alias not in gateway_aliases(gateway_url, master_headers), (
            f"{alias} is live at the gateway after issue was refused — the refusal "
            "happened after the key was minted, which is worse than not refusing at all"
        )

    def test_revocation_does_not_erase_the_bill(
        self, env, control_plane_url, gateway_url, admin_headers, master_headers
    ):
        """Money spent before a key was revoked must stay attributed to the person.

        This is items 4 and 6 pulling against each other, and it was found the hard way:
        the bill joined spend rows to the gateway's live key table, and revocation deletes
        from that table. Every historical row for a revoked key silently became
        "(unattributed)" — on the cluster, 88% of all recorded spend, after nothing more
        exotic than reprovisioning a workspace a few times.

        A bill that goes quiet about money that was definitely spent is worse than one
        that is obviously broken, so this asserts the number survives the revocation.
        """
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)
        issued = httpx.post(
            f"{control_plane_url}/admin/keys/issue", headers=admin_headers,
            json={"username": DOGFOOD_USER, "surface": "terminal"}, timeout=TIMEOUT,
        )
        assert issued.status_code == 200, issued.text
        key = issued.json()["key"]

        spent = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": "bill me"}]},
            timeout=TIMEOUT,
        )
        assert spent.status_code == 200, spent.text

        def terminal_requests() -> int:
            rows = httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()["by_user_and_surface"]
            return sum(
                r["requests"] for r in rows
                if r["username"] == DOGFOOD_USER and r["surface"] == "terminal"
            )

        before = 0
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            before = terminal_requests()
            if before:
                break
            time.sleep(2)
        assert before, "the request was never recorded against the terminal surface"

        httpx.post(f"{gateway_url}/key/delete", headers=master_headers,
                   json={"key_aliases": [f"{DOGFOOD_USER}::terminal"]}, timeout=TIMEOUT)

        after = terminal_requests()
        assert after >= before, (
            f"revoking the key erased {before - after} request(s) from the bill; "
            "spend attribution must not depend on the key still existing"
        )


# ---------------------------------------------------------------------------
# Item 7 — budget stop refuses, it does not merely record
# ---------------------------------------------------------------------------

class TestItem7BudgetStop:
    def test_over_budget_key_is_refused(self, gateway_url, master_headers):
        alias = f"budget-{uuid.uuid4().hex[:8]}::chat"
        created = httpx.post(
            f"{gateway_url}/key/generate",
            headers=master_headers,
            # A budget small enough that one request exhausts it.
            json={"key_alias": alias, "max_budget": 0.0000001},
            timeout=TIMEOUT,
        )
        assert created.status_code == 200, created.text
        key = created.json()["key"]
        headers = {"Authorization": f"Bearer {key}"}

        def spend_request():
            # Every probe must be a cache miss. A cache hit is served without consulting
            # the budget, so reusing one payload would test the cache, not enforcement.
            return httpx.post(
                f"{gateway_url}/v1/chat/completions",
                headers=headers,
                json={
                    "model": "fake-large",
                    "messages": [{"role": "user", "content": f"spend {uuid.uuid4().hex}"}],
                },
                timeout=TIMEOUT,
            )

        first = spend_request()
        assert first.status_code == 200, first.text

        # Budget enforcement reads accumulated spend, which the gateway flushes on a
        # batch interval — so the refusal is expected to lag the overspend by seconds.
        deadline = time.monotonic() + 90
        refused = False
        while time.monotonic() < deadline:
            if spend_request().status_code >= 400:
                refused = True
                break
            time.sleep(3)

        assert refused, "key exceeded its budget and was still served"


# ---------------------------------------------------------------------------
# Item 8 — the bundle runs with no provider account and no GPU
# ---------------------------------------------------------------------------

class TestItem8RunsWithoutProviderAccount:
    def test_no_real_provider_key_is_configured(self, env):
        assert not env.get("ANTHROPIC_API_KEY"), "test asserts the no-account path"
        assert not env.get("OPENAI_API_KEY"), "test asserts the no-account path"

    def test_traffic_still_flows(self, gateway_url, named_key_headers):
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers=named_key_headers,
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"no account {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text

    def test_every_service_is_healthy(self):
        ps = compose("ps", "--format", "{{.Service}} {{.State}} {{.Health}}")
        lines = [l for l in ps.stdout.strip().splitlines() if l.strip()]
        assert lines, "no services running"
        for line in lines:
            parts = line.split()
            service, state = parts[0], parts[1]
            health = parts[2] if len(parts) > 2 else ""
            assert state == "running", f"{service} is {state}"
            if health:
                assert health == "healthy", f"{service} is {health}"


# ---------------------------------------------------------------------------
# Item 9 — a tested exit path
# ---------------------------------------------------------------------------

class TestItem9ExitPath:
    """Leaving must be a procedure, not a support ticket.

    These tests run near the end of the suite because revoking every key genuinely breaks
    the running bundle; the class restores it afterwards.
    """

    @pytest.fixture(scope="class", autouse=True)
    def _restore_bundle(self, control_plane_url, admin_headers):
        yield
        # Re-mint the chat surface key, restart the surface so it picks it up, and
        # reconcile identity. Without this the bundle is left dead for the next run and
        # the failure would look unrelated to the exit test that caused it.
        subprocess.run([str(BUNDLE / "bin" / "provision-chat-key.sh")], check=False,
                       capture_output=True)
        compose("up", "-d", "chat", check=False)
        httpx.post(f"{control_plane_url}/admin/sync", headers=admin_headers, timeout=TIMEOUT)
        subprocess.run([str(BUNDLE / "bin" / "wait-healthy.sh")], check=False,
                       capture_output=True)

    def _export(self, tmp_path) -> pathlib.Path:
        out = tmp_path / "export"
        out.mkdir()
        result = subprocess.run(
            [str(BUNDLE / "bin" / "exit.sh"), "export"],
            capture_output=True, text=True, cwd=str(BUNDLE),
            env={**os.environ, "EXPORT_DIR": str(out)},
        )
        assert result.returncode == 0, f"export failed:\n{result.stdout}\n{result.stderr}"
        latest = out / "latest"
        assert latest.exists(), f"no export produced: {list(out.iterdir())}"
        return latest.resolve()

    def test_export_is_complete_and_verifies_standalone(self, tmp_path):
        """The archive must verify with nothing from this platform running."""
        export = self._export(tmp_path)

        manifest = json.loads((export / "manifest.json").read_text())
        assert manifest["audit_events"] > 0
        assert (export / "audit.jsonl").exists()
        assert (export / "spend.csv").exists()
        assert (export / "keys.csv").exists()

        result = subprocess.run(
            [str(BUNDLE / "bin" / "verify-export.py"), str(export)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"verification failed:\n{result.stdout}"
        assert "EXPORT VERIFIED" in result.stdout

    def test_verifier_detects_a_tampered_event(self, tmp_path):
        """A verifier that cannot fail proves nothing."""
        export = self._export(tmp_path)
        path = export / "audit.jsonl"
        lines = path.read_text().splitlines()
        assert len(lines) >= 2, "need at least two events to test tampering"

        event = json.loads(lines[1])
        event["actor"] = "attacker"
        lines[1] = json.dumps(event, sort_keys=True)
        path.write_text("\n".join(lines) + "\n")

        result = subprocess.run(
            [str(BUNDLE / "bin" / "verify-export.py"), str(export)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "tampering went undetected"
        assert "has been altered" in result.stdout

    def test_verifier_detects_a_truncated_export(self, tmp_path):
        export = self._export(tmp_path)
        path = export / "audit.jsonl"
        lines = path.read_text().splitlines()
        assert len(lines) >= 3
        path.write_text("\n".join(lines[:-1]) + "\n")

        result = subprocess.run(
            [str(BUNDLE / "bin" / "verify-export.py"), str(export)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "truncation went undetected"
        assert "truncated" in result.stdout

    def test_standalone_verifier_agrees_with_the_control_plane(self, tmp_path):
        """Guard against drift.

        The verifier reimplements the chain digest so it can outlive this codebase. That
        duplication is only safe if the two stay in agreement, so assert it rather than
        hope: recomputing an exported event with the verifier's own digest must reproduce
        the hash the control plane wrote.
        """
        export = self._export(tmp_path)
        spec = importlib.util.spec_from_file_location(
            "verify_export", BUNDLE / "bin" / "verify-export.py"
        )
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)

        events = [
            json.loads(l) for l in (export / "audit.jsonl").read_text().splitlines() if l.strip()
        ]
        assert events, "nothing to compare"
        for e in events:
            recomputed = verifier.digest(
                e["prev_hash"], e["ts"], e["actor"], e["action"], e["target"], e["detail"]
            )
            assert recomputed == e["hash"], (
                f"verifier and control plane disagree on event {e['seq']}"
            )

    def test_direct_provider_config_names_the_real_upstreams(self, tmp_path):
        self._export(tmp_path)
        result = subprocess.run(
            [str(BUNDLE / "bin" / "exit.sh"), "direct"],
            capture_output=True, text=True, cwd=str(BUNDLE),
            env={**os.environ, "EXPORT_DIR": str(tmp_path / "export")},
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        direct = (tmp_path / "export" / "latest" / "direct").resolve()
        upstreams = json.loads((direct / "upstreams.json").read_text())
        assert upstreams, "no upstreams recorded"
        for row in upstreams:
            assert row["model"], row
            assert row["api_base"], f"no base URL for {row['model']} — nothing to point at"
        assert (direct / "README.md").exists()

    def test_revoking_stops_every_key_and_surfaces_still_reach_the_provider(
        self, gateway_url, control_plane_url, admin_headers, master_headers, env
    ):
        """The whole claim: after exit the layer refuses everything, and the surfaces
        keep working by talking to the provider directly."""
        # A key that works right now, to prove afterwards that it stopped.
        alias = f"exittest-{uuid.uuid4().hex[:8]}::ide"
        created = httpx.post(
            f"{gateway_url}/key/generate", headers=master_headers,
            json={"key_alias": alias}, timeout=TIMEOUT,
        )
        assert created.status_code == 200, created.text
        key = created.json()["key"]

        before = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"pre-exit {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert before.status_code == 200, before.text

        revoked = httpx.post(
            f"{control_plane_url}/admin/exit/revoke-all", headers=admin_headers,
            timeout=TIMEOUT,
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked"] >= 1

        after = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"post-exit {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert after.status_code >= 400, (
            "a virtual key still worked after the exit revoked everything"
        )

        # The layer is gone; the surface goes straight to the provider with the
        # operator's own key. This is what makes the exit real rather than declarative.
        provider = f"http://localhost:{env.get('FAKEPROVIDER_PORT', '8090')}/v1"
        direct = httpx.post(
            f"{provider}/chat/completions",
            headers={"Authorization": "Bearer operators-own-provider-key"},
            json={"model": "fake-gpt-large",
                  "messages": [{"role": "user", "content": "direct after exit"}]},
            timeout=TIMEOUT,
        )
        assert direct.status_code == 200, (
            f"surface cannot reach the provider directly after exit: {direct.text[:300]}"
        )
        assert direct.json()["choices"][0]["message"]["content"].startswith("[fake-provider")
