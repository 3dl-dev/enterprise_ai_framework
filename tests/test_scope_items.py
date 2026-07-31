"""One test class per numbered scope item in the sealed estimate.

If any of these cannot pass against the running bundle, the row is void — not re-scoped.
"""

import csv
import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import time
import urllib.parse
import uuid

import httpx
import pytest

import chat_turn
import file_upload
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

class TestChatPrincipalOnTheOneBill:
    """Finding 34: the one bill named the same money two different ways.

    `/admin/spend` — the query scope item 4 actually names, and the only bill an operator
    without a browser has — printed the chat surface as a column of LibreChat ObjectIds:

        6a67b18069dba4d1126fef44 / chat   135 req   $0.2247

    while the portal, over the same query and the same rows, printed `baron`. The lookup
    was never broken. It was applied in portal.py at two call sites and nowhere else.

    It survived every test because the compose bundle had no CHAT_MONGO_URL, so no chat
    row here ever carried an ObjectId and the code path that has to translate one was
    never reached. The fixture was more uniform than production, which is the actual
    reason this shipped. The bundle now sets it, exactly as the cluster always has.

    A presence check would pass on the broken code — `username` was always present, and
    always a string. Every assertion below is about WHICH string.

    THE PREMISE IS TESTED, NOT ASSUMED. All of this only matters if LibreChat really does
    put its own Mongo `_id` into `end_user`; if it forwarded an email or an OIDC `sub`,
    translating ObjectIds would be translating something production never sends. So the
    first test drives a genuine message through the chat surface's own API as a
    signed-in user and reads the raw `end_user` the gateway recorded, before anything
    here has had a chance to stage it.

    NOTHING HERE MAY CONTAMINATE THE ARTIFACT UNDER TEST. This class asserts on the bill,
    so a row it leaves behind is a row it corrupts its own subject with. It therefore
    creates no synthetic chat accounts at all — the person it bills is the bundle's real
    bootstrap user, signed in through the real identity provider, whose `baron / chat`
    row is a row the bill SHOULD carry. The one deliberately unattributable row, in the
    unresolvable-principal test, is deleted from the ledger in a `finally` and its
    absence is then asserted, so reversibility is a checked claim rather than a comment.
    """

    # The chat surface refuses a request whose User-Agent is not a browser
    # (api/server/middleware/uaParser.js -> "Illegal request"), so driving its message
    # API from a test client means presenting one. Same class of browser emulation as
    # oidc_login._cookie_header, and for the same reason: the alternative is loosening
    # the surface to suit the test.
    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )

    # The alias the one shared chat key is minted under. metering.SHARED_SURFACE_ALIASES
    # defaults to this, and it is the only alias whose end_user the bill is allowed to
    # trust — so it is also the only alias whose end_user is worth reading here.
    SHARED_CHAT_ALIAS = "chat-surface::chat"

    # ---------------------------------------------------------------- helpers

    def _chat_headers(self, client, chat_url: str) -> dict:
        return {
            "Authorization": f"Bearer {_chat_token(client, chat_url)}",
            "Cookie": oidc_login._cookie_header(client),
            "User-Agent": self.BROWSER_UA,
        }

    def _chat_identity(self, client, chat_url: str) -> tuple[str, str]:
        """(LibreChat's ObjectId for the signed-in user, their username).

        Read from the surface's own /api/user rather than written by us, because the
        whole question this class exists to answer is what identifier that surface
        actually holds for a person.
        """
        me = oidc_login.whoami(client, chat_url)
        user = me.get("user", me)
        oid = str(user.get("_id") or user.get("id") or "")
        assert re.fullmatch(r"[0-9a-f]{24}", oid), (
            f"the chat surface identifies {user.get('username')!r} by {oid!r}, which is "
            "not a Mongo ObjectId — chat_identity's whole premise is that it is one"
        )
        return oid, user["username"]

    def _send_a_real_chat_message(self, client, chat_url: str) -> str:
        """Ask the chat surface a question, as a signed-in person, through its own API.

        Not a gateway call dressed up as one. This goes through LibreChat's message
        route, so whatever LibreChat decides to put in the outgoing request's `user`
        field is LibreChat's decision and not ours — which is the only way to test the
        premise rather than stage it.
        """
        endpoints = client.get(
            f"{chat_url}/api/endpoints", headers=self._chat_headers(client, chat_url),
            timeout=TIMEOUT,
        ).json()
        custom = [n for n, cfg in endpoints.items() if cfg.get("type") == "custom"]
        assert custom, f"no custom endpoint on the chat surface: {list(endpoints)}"
        endpoint = custom[0]

        models = client.get(
            f"{chat_url}/api/models", headers=self._chat_headers(client, chat_url),
            timeout=TIMEOUT,
        ).json().get(endpoint, [])
        assert models, f"the chat surface offers no models on {endpoint!r}"
        model = "fake-large" if "fake-large" in models else models[0]

        text = f"one bill premise {uuid.uuid4().hex}"
        # Routed through chat_turn rather than posting here, because the POST response is
        # NOT the answer on every version. This test was written against v0.8.0, where the
        # POST body WAS the SSE stream and `on_message_delta` appeared in it. v0.8.7 answers
        # {"streamId", "conversationId", "status": "started"} and delivers the reply on a
        # separate GET — so the old assertion failed with "the chat surface never produced
        # an answer, so nothing reached the gateway", blaming the gateway for a protocol
        # change in the surface. chat_turn.send_turn reads the PERSISTED assistant message
        # from GET /api/messages/<conversationId>, which is true on both versions.
        # Same defect as enterpriseaiframework-614; do not reintroduce a protocol-specific
        # assertion here.
        reply = chat_turn.send_turn(
            client, chat_url, text, model=model, endpoint=endpoint,
            headers=self._chat_headers(client, chat_url), timeout=TIMEOUT * 3,
        )
        assert reply, (
            "the chat surface persisted no assistant message, so nothing reached the "
            "gateway for this turn"
        )
        return text

    def _gateway_sql(self, env, sql: str) -> str:
        """One statement against the gateway's own database, from inside the bundle."""
        r = compose("exec", "-T", "postgres", "psql", "-U", env.get("POSTGRES_USER", "eai"),
                    "-d", "gateway", "-tA", "-c", sql, check=False)
        assert r.returncode == 0, f"psql failed: {sql}\n{r.stdout}\n{r.stderr}"
        return r.stdout.strip()

    def _ledger_now(self, env) -> str:
        """The ledger database's own clock. The host's would be a different clock."""
        return self._gateway_sql(env, "SELECT now()")

    def _forwarded_end_users(self, env, since: str) -> set[str]:
        """Every distinct end_user the CHAT SURFACE's shared key sent since `since`."""
        out = self._gateway_sql(env, (
            "SELECT DISTINCT COALESCE(s.end_user, '') FROM \"LiteLLM_SpendLogs\" s "
            f"WHERE s.metadata->>'user_api_key_alias' = '{self.SHARED_CHAT_ALIAS}' "
            f"AND s.\"startTime\" >= '{since}'::timestamptz"
        ))
        return {line.strip() for line in out.splitlines() if line.strip()}

    def _ledger_rows_for(self, env, end_user: str) -> int:
        return int(self._gateway_sql(
            env,
            f"SELECT count(*) FROM \"LiteLLM_SpendLogs\" WHERE end_user = '{end_user}'",
        ) or 0)

    def _purge_ledger_rows_for(self, env, end_user: str) -> None:
        """Remove rows this test wrote, so the bill it inspects is left as it found it.

        Only ever called with a 24-hex identifier this test generated, so it cannot
        reach a real person's spend. It restores every rendering of the bill —
        /admin/spend, the portal and the operator console all read
        "LiteLLM_SpendLogs". It does not roll back the shared chat key's own cumulative
        spend counter inside the gateway, which is a budget counter rather than the
        bill, and which every other spending test in this suite moves too.
        """
        assert re.fullmatch(r"[0-9a-f]{24}", end_user), end_user
        self._gateway_sql(
            env, f"DELETE FROM \"LiteLLM_SpendLogs\" WHERE end_user = '{end_user}'"
        )

    def _chat_key(self, env) -> str:
        key = env.get("CHAT_VIRTUAL_KEY", "").strip()
        assert key, (
            "CHAT_VIRTUAL_KEY missing from bundle/.env — provision-chat-key.sh runs as "
            "part of `make up`; without the shared surface key there is no way to write "
            "a chat-surface spend row and this test proves nothing"
        )
        return key

    def _spend_as_chat_user(self, gateway_url: str, key: str, oid: str):
        """One gateway request through the shared chat key, on behalf of `oid`.

        The same shape the chat surface produces — proven to be the same shape by
        test_the_chat_surface_identifies_a_spender_by_its_own_mongo_id, which reads
        what LibreChat itself forwards. Used where a specific identifier is needed
        (one that cannot be resolved, for instance) and driving the full surface would
        only add ways for the test to fail for the wrong reason.
        """
        return httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "fake-large",
                "messages": [{"role": "user", "content": f"chat bill {uuid.uuid4().hex}"}],
                "user": oid,
            },
            timeout=TIMEOUT,
        )

    def _bill(self, control_plane_url, admin_headers, since: str | None = None) -> dict:
        r = httpx.get(f"{control_plane_url}/admin/spend", headers=admin_headers,
                      params={"since": since} if since else None, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        return r.json()

    def _await_row(self, control_plane_url, admin_headers, predicate, what: str) -> dict:
        """The gateway flushes spend rows on a batch interval; poll, do not sleep-and-hope."""
        deadline = time.monotonic() + 90
        bill = {}
        while time.monotonic() < deadline:
            bill = self._bill(control_plane_url, admin_headers)
            row = next((r for r in bill["by_user_and_surface"] if predicate(r)), None)
            if row:
                return row
            time.sleep(2)
        raise AssertionError(
            f"{what} never appeared on the bill. Last bill: "
            f"{json.dumps(bill.get('by_user_and_surface', []), indent=2)}"
        )

    def _portal_spend(self, username: str) -> dict:
        """This person's own portal page.

        The portal authenticates from its sidecar proxy, which shares the pod's network
        namespace — so it only trusts identity headers arriving from loopback. Reached
        here from inside the container for that reason, not to bypass anything: this is
        the same request the proxy makes, from the same address it makes it from.
        """
        code = (
            "import json,urllib.request;"
            "req=urllib.request.Request("
            "'http://localhost:8000/portal/api/spend',"
            "headers={'X-Auth-Request-Preferred-Username': %r});"
            "print(urllib.request.urlopen(req, timeout=30).read().decode())" % username
        )
        proc = compose("exec", "-T", "control-plane", "python", "-c", code, check=False)
        assert proc.returncode == 0, f"portal call failed: {proc.stdout}\n{proc.stderr}"
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def _agree_on(self, control_plane_url, admin_headers, username: str, surface: str):
        """Read both renderings until they are reading the same settled ledger.

        The gateway writes spend rows on a batch interval, so a row can land between two
        reads and make the operator bill and the portal differ by a request that neither
        is wrong about. Retried a bounded number of times for that reason only: a real
        disagreement is systematic and survives every attempt, and the assertion below
        still fails on the last pair read.
        """
        last = None
        for _ in range(5):
            admin_row = self._await_row(
                control_plane_url, admin_headers,
                lambda r: r["username"] == username and r["surface"] == surface,
                f"{surface} spend for {username}",
            )
            portal = self._portal_spend(username)
            entry = next(
                (s for s in portal["by_surface"] if s["surface"] == surface), None
            )
            last = (admin_row, portal, entry)
            if entry and entry["requests"] == admin_row["requests"]:
                return last
            time.sleep(3)
        return last

    # ---------------------------------------------------------------- the premise

    def test_the_chat_surface_identifies_a_spender_by_its_own_mongo_id(
        self, chat_session, chat_url, env, control_plane_url, admin_headers
    ):
        """The assumption everything else here rests on, driven end to end.

        chat_identity translates a 24-hex ObjectId because that is what LibreChat is
        believed to forward. If it forwarded an email, or the OIDC subject, or nothing,
        then every other test in this class would be translating an identifier that
        never appears in production and the real bill would still read as hex.

        So: a genuine message, sent through the chat surface's own message API by a
        session that authenticated through the real identity provider. Nothing here
        chooses the outgoing `user` field — LibreChat does. The value is then read back
        from the gateway's raw ledger column, not from the bill, because the bill is
        what is under test.
        """
        oid, username = self._chat_identity(chat_session, chat_url)
        me = oidc_login.whoami(chat_session, chat_url)
        user = me.get("user", me)

        since = self._ledger_now(env)
        self._send_a_real_chat_message(chat_session, chat_url)

        deadline = time.monotonic() + 90
        forwarded: set[str] = set()
        while time.monotonic() < deadline:
            forwarded = self._forwarded_end_users(env, since)
            if forwarded:
                break
            time.sleep(2)

        assert forwarded, (
            "a message was answered by the chat surface but the gateway recorded no "
            f"spend row for {self.SHARED_CHAT_ALIAS} — chat traffic is not being metered"
        )
        assert forwarded == {oid}, (
            f"the chat surface identified the spender as {sorted(forwarded)} but "
            f"LibreChat's own record of {username} is {oid}. chat_identity translates "
            "Mongo ObjectIds; if this is anything else, it is translating an identifier "
            "production never sends"
        )
        # Named explicitly so the failure says WHICH wrong thing was forwarded.
        assert user.get("email") not in forwarded, "chat forwards an email, not an ObjectId"
        assert user.get("openidId") not in forwarded, "chat forwards the OIDC sub, not an ObjectId"

        # And the consequence, on the artifact under test: that identifier becomes a name.
        row = self._await_row(
            control_plane_url, admin_headers,
            lambda r: r["username"] == username and r["surface"] == "chat",
            f"chat spend for {username} from a real chat message",
        )
        assert row["requests"] >= 1
        bill = self._bill(control_plane_url, admin_headers)
        assert not any(oid in r["username"] for r in bill["by_user_and_surface"]), (
            f"a real chat message billed to the raw ObjectId {oid}: "
            f"{[r['username'] for r in bill['by_user_and_surface']]}"
        )

    # ---------------------------------------------------------------- the translation

    def test_chat_spend_is_billed_to_the_person_not_to_a_hex_string(
        self, chat_session, chat_url, env, gateway_url, control_plane_url,
        admin_headers, master_headers
    ):
        """A request made by a KNOWN chat user shows THAT user's name on /admin/spend.

        Not "a username field exists" — the field existed throughout the defect. The
        assertion is that the field holds the name of the person who made the request,
        and that the ObjectId they were identified by is nowhere on the bill.

        The identity is the bundle's real bootstrap user, taken from the chat surface
        itself, so this test invents no account and leaves no row the bill should not
        carry.
        """
        oid, username = self._chat_identity(chat_session, chat_url)

        # A per-user IDE key spending in the same window, so the row this change must NOT
        # touch is on the same bill as the row it must fix — and is there because this
        # test put it there, not because some earlier test happened to run first.
        ide_user = f"idetest-{uuid.uuid4().hex[:8]}"
        created = httpx.post(
            f"{gateway_url}/key/generate", headers=master_headers,
            json={"key_alias": f"{ide_user}::ide", "metadata": {"surface": "ide"}},
            timeout=TIMEOUT,
        )
        assert created.status_code == 200, created.text
        ide_spend = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {created.json()['key']}"},
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"ide bill {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert ide_spend.status_code == 200, ide_spend.text

        resp = self._spend_as_chat_user(gateway_url, self._chat_key(env), oid)
        assert resp.status_code == 200, resp.text

        row = self._await_row(
            control_plane_url, admin_headers,
            lambda r: r["username"] == username and r["surface"] == "chat",
            f"chat spend for {username}",
        )
        assert row["requests"] >= 1
        assert row["spend"] > 0

        bill = self._bill(control_plane_url, admin_headers)
        principals = [r["username"] for r in bill["by_user_and_surface"]]
        assert oid not in principals, (
            f"the raw LibreChat ObjectId {oid} is still a principal on the bill: "
            f"{principals} — this is finding 34 exactly"
        )
        assert not any(oid in p for p in principals), (
            f"the ObjectId leaked into a principal label: {principals}"
        )

        # THE CASE THIS CHANGE DID NOT TOUCH, asserted on the same bill: a per-user key
        # already carries the person in its alias and must still read as a plain name.
        # `baron / ide 223 req` was the row that was RIGHT before this fix.
        self._await_row(
            control_plane_url, admin_headers,
            lambda r: r["username"] == ide_user and r["surface"] == "ide",
            f"ide spend for {ide_user}",
        )
        bill = self._bill(control_plane_url, admin_headers)
        ide_rows = [r for r in bill["by_user_and_surface"] if r["surface"] == "ide"]
        assert ide_rows, "no ide spend on the bill to cross-check the unchanged path"
        for r in ide_rows:
            assert not r["username"].startswith("("), (
                f"a per-user ide principal was relabelled: {r['username']}"
            )
            assert "::" not in r["username"]

    def test_the_cli_bill_and_the_portal_agree_about_who_spent_what(
        self, chat_session, chat_url, env, gateway_url, control_plane_url, admin_headers
    ):
        """The two renderings, over the same money, from the same query.

        Finding 34 was not "the CLI is wrong" — it was that two readers of one ledger
        disagreed, and each looked right on its own. So this compares them rather than
        checking either in isolation: the operator's `make spend` row for a person and
        that person's own portal page must carry the same principal, the same request
        count and the same spend.
        """
        oid, username = self._chat_identity(chat_session, chat_url)
        assert self._spend_as_chat_user(
            gateway_url, self._chat_key(env), oid
        ).status_code == 200

        admin_row, portal, portal_chat = self._agree_on(
            control_plane_url, admin_headers, username, "chat"
        )

        assert portal["username"] == username
        assert portal_chat is not None, (
            f"the portal shows this person no chat spend at all while the operator bill "
            f"shows {admin_row['requests']} request(s): {portal}"
        )
        assert portal_chat["requests"] == admin_row["requests"], (
            f"console and CLI disagree on request count for {username}: "
            f"portal={portal_chat['requests']} admin={admin_row['requests']}"
        )
        assert portal_chat["spend"] == pytest.approx(admin_row["spend"]), (
            f"console and CLI disagree on spend for {username}: "
            f"portal={portal_chat['spend']} admin={admin_row['spend']}"
        )

    def test_the_portal_still_names_a_user_who_has_no_chat_identity(
        self, gateway_url, control_plane_url, admin_headers, master_headers
    ):
        """The control case on the path this change DELETED code from.

        `portal.my_spend` used to call `chat_identity.resolve()` on every row before
        filtering; that call is gone, because the query names the principal now. For a
        chat user the two new tests above prove the replacement works. This is the case
        the change did NOT set out to alter and therefore the one most likely to have
        been broken silently: somebody whose spend never went near the chat surface,
        whose principal is an ordinary username, and who has no ObjectId anywhere.

        If the deletion had removed naming rather than relocated it, this user's rows
        would arrive unlabelled, the `who != user` filter would match nothing, and the
        portal would show them a total of zero while the operator bill showed their
        spend — a missing number that reads as a zero, which is the failure mode this
        whole item is about.
        """
        user = f"idesolo-{uuid.uuid4().hex[:8]}"
        created = httpx.post(
            f"{gateway_url}/key/generate", headers=master_headers,
            json={"key_alias": f"{user}::ide", "metadata": {"surface": "ide"}},
            timeout=TIMEOUT,
        )
        assert created.status_code == 200, created.text
        for _ in range(2):
            spent = httpx.post(
                f"{gateway_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {created.json()['key']}"},
                json={"model": "fake-large",
                      "messages": [{"role": "user",
                                    "content": f"solo ide {uuid.uuid4().hex}"}]},
                timeout=TIMEOUT,
            )
            assert spent.status_code == 200, spent.text

        admin_row, portal, portal_ide = self._agree_on(
            control_plane_url, admin_headers, user, "ide"
        )

        assert portal["username"] == user
        assert portal_ide is not None, (
            f"the portal shows {user} no ide spend while the operator bill shows "
            f"{admin_row['requests']} request(s): {portal}"
        )
        assert portal_ide["requests"] == admin_row["requests"], (
            f"portal and CLI disagree for a non-chat user {user}: "
            f"portal={portal_ide['requests']} admin={admin_row['requests']}"
        )
        assert portal_ide["spend"] == pytest.approx(admin_row["spend"])
        assert portal["total"]["requests"] == admin_row["requests"], (
            f"the portal's own total does not match its only surface: {portal}"
        )
        assert portal["total"]["spend"] > 0, (
            f"{user} spent money and the portal shows them nothing: {portal}"
        )
        # A user with no chat identity must be named exactly as they always were, and
        # must not acquire a surface they never used.
        assert not portal["username"].startswith("(")
        assert {s["surface"] for s in portal["by_surface"]} == {"ide"}, (
            f"a non-chat user picked up a surface they never used: {portal['by_surface']}"
        )
        assert admin_row["username"] == user

    def test_the_operator_console_names_the_same_people_as_the_cli_bill(
        self, chat_session, chat_url, env, gateway_url, control_plane_url, admin_headers
    ):
        """The third rendering. `/portal/api/admin/overview` is the page finding 34 says
        showed names while the CLI showed hex; the two must now agree principal for
        principal, over the whole bill and not just one row."""
        oid, username = self._chat_identity(chat_session, chat_url)
        assert self._spend_as_chat_user(
            gateway_url, self._chat_key(env), oid
        ).status_code == 200
        self._await_row(
            control_plane_url, admin_headers,
            lambda r: r["username"] == username and r["surface"] == "chat",
            f"chat spend for {username}",
        )

        operator = env.get("BOOTSTRAP_USER", DOGFOOD_USER)
        code = (
            "import json,urllib.request;"
            "req=urllib.request.Request("
            "'http://localhost:8000/portal/api/admin/overview',"
            "headers={'X-Auth-Request-Preferred-Username': %r});"
            "print(urllib.request.urlopen(req, timeout=30).read().decode())" % operator
        )
        proc = compose("exec", "-T", "control-plane", "python", "-c", code, check=False)
        assert proc.returncode == 0, (
            f"operator console call failed (PORTAL_ADMINS must name {operator}): "
            f"{proc.stdout}\n{proc.stderr}"
        )
        console = json.loads(proc.stdout.strip().splitlines()[-1])

        console_people = {p["username"] for p in console["people"] if p["requests"]}
        bill = self._bill(control_plane_url, admin_headers)
        cli_people = {r["username"] for r in bill["by_user_and_surface"]}
        assert username in console_people
        assert cli_people <= console_people, (
            f"the CLI bill names principals the console does not: "
            f"{sorted(cli_people - console_people)}"
        )

    def test_an_unresolvable_chat_principal_is_labelled_never_dropped(
        self, env, gateway_url, control_plane_url, admin_headers
    ):
        """Spend forwarded for a chat account that does not exist.

        Three wrong answers this rules out, all of which would have looked fine: dropping
        the row (money leaves the bill silently — finding 25's shape), guessing a name
        (attribution becomes fiction), and printing the bare ObjectId (finding 34). The
        money stays, the label says plainly that we could not name it, and the bill warns.

        AND THEN IT IS UNDONE. This is the one test here that deliberately writes a row
        the bill can never name, and the bill is the artifact under test — left behind,
        it would put a permanent `(unresolved chat user ...)` line, and a permanent
        warning, on every `make spend` this bundle ever renders. So the ledger rows are
        deleted in a `finally` and their absence is asserted afterwards: the cleanup is
        a claim under test, not a promise in a comment.
        """
        ghost = uuid.uuid4().hex[:24]
        try:
            assert self._spend_as_chat_user(
                gateway_url, self._chat_key(env), ghost
            ).status_code == 200

            row = self._await_row(
                control_plane_url, admin_headers,
                lambda r: ghost in r["username"],
                f"unresolvable chat principal {ghost}",
            )
            assert row["username"] != ghost, (
                "a bare ObjectId is not a label — it reads as a person's name, which is "
                "the defect this item exists to fix"
            )
            assert row["username"].startswith("(unresolved chat user "), row["username"]
            assert row["requests"] >= 1 and row["spend"] > 0, "the money must stay on the bill"

            bill = self._bill(control_plane_url, admin_headers)
            warning = next(
                (w for w in bill["warnings"] if w["kind"] == "unresolved_chat_principal"),
                None,
            )
            assert warning is not None, (
                f"the bill carries unnamed spend and says nothing about it: {bill['warnings']}"
            )
            assert row["username"] in warning["principals"]
        finally:
            self._purge_ledger_rows_for(env, ghost)

        assert self._ledger_rows_for(env, ghost) == 0
        bill = self._bill(control_plane_url, admin_headers)
        assert not any(ghost in r["username"] for r in bill["by_user_and_surface"]), (
            f"this test permanently disfigured the bill it exists to check: "
            f"{[r['username'] for r in bill['by_user_and_surface']]}"
        )
        for w in bill["warnings"]:
            assert not any(ghost in p for p in w.get("principals", [])), (
                f"a warning this test manufactured outlived it: {w}"
            )


class TestSubagentAttribution:
    """enterpriseaiframework-00d: a request the model decomposes into subagents runs as
    subagents, and every constituent call is attributed and billed to the REQUESTING
    user — not to a synthetic agent principal and not unattributed.

    WHY THIS HAD TO BE MEASURED, NOT REASONED ABOUT. LibreChat v0.8.7 advertises
    `subagents` in its agents-endpoint capability list (asserted already in
    test_chat_surface_version.py) but nothing in this bundle configured or exercised
    the mechanism, and enterpriseaiframework-f50's own investigation could not settle
    the billing question: "an actual subagent delegation was not exercised because the
    bundle has no tool-calling model." enterpriseaiframework-d98 is the reason this
    matters rather than being a curiosity — the gateway's own root credential was once a
    valid, unbudgeted, unattributable inference credential, and a subagent fan-out that
    bills to nobody (or to the agent's own id, or to the surface as a whole) would be the
    identical hole with a friendlier name.

    HOW A DELEGATION IS FORCED WITHOUT A REAL TOOL-CALLING MODEL. Same inversion this
    bundle already uses for bash_tool (082), MCP tools (471) and file_search (c7c):
    fakeprovider recognizes a `CALL_SUBAGENT:<type>:<task>` marker in the user's own
    message and turns it into a real `subagent` tool call (Constants.SUBAGENT in
    @librechat/agents) with the exact argument shape SubagentToolSchema requires. From
    there LibreChat's own SubagentExecutor does the real work: it spawns a genuine child
    graph, which makes its OWN, SEPARATE completion request back to fakeprovider (a
    second, distinct HTTP call this stub cannot avoid making real), and the child's
    reply is relayed back to the parent verbatim by the SAME generic tool-result relay
    branch bash_tool/MCP/file_search already rely on (`find_tool_result` only checks
    `role == "tool"`, never the tool's name). See tests/test_fakeprovider_subagent.py for
    the marker/relay logic proven in isolation.

    WHAT WAS MEASURED, on this running bundle, reading `LiteLLM_SpendLogs` directly
    rather than trusting a rendering of it: a single subagent-delegating turn writes
    THREE distinct completion rows under the model `fake-gpt-large` — the parent's own
    decision call, the child subagent's own completion (a genuinely separate HTTP
    request the child graph makes on its own), and the parent's post-relay completion.
    `LiteLLM_SpendLogs.messages` is `{}` on every row this bundle writes (verified by
    psql against this exact table), so the child's row cannot be identified by reading
    its prompt text back off the ledger — that claim is NOT made here. What IS measured
    and asserted is the row count itself: fewer than three rows means no distinct child
    completion happened, only the ordinary parent-call/relay-call pair an ordinary tool
    turn already produces (see the negative control below, which reproduces exactly that
    two-row shape when no subagent tool is bound at all). EVERY one of the three rows
    carries the SAME `end_user` (LibreChat's own Mongo _id for the signed-in user) and
    the SAME `metadata.user_api_key_alias` (the shared `chat-surface::chat` key every
    chat turn authenticates with) as an ordinary, non-delegating turn. Translated through
    the SAME `chat_identity` mechanism TestChatPrincipalOnTheOneBill already proves,
    every constituent call therefore lands on `/admin/spend` under the requesting
    person's own name and the `chat` surface — not a synthetic agent principal, not the
    agent's own id, and not `(unattributed)`.

    THE NEGATIVE CONTROL BELOW IS WHAT MAKES THIS MORE THAN AN ARGUMENT. An earlier
    version of this class asserted only `minimum=2` constituent rows, `tool_calls(reply)`
    non-empty, and nothing about the reply's content. Run against an agent with NO
    `subagents` config bound (so LibreChat has no `subagent` tool at all), the identical
    marker turn persists a tool_call block named `subagent` whose own output is
    `Error processing tool: Tool "subagent" not found.`, the visible reply is the string
    `Error: Tool "subagent" not found. Please fix your mistakes.`, and the ledger shows
    FEWER THAN THREE `fake-gpt-large` rows — measured directly against this bundle
    (three separate runs, one an 80-second patient poll with no early exit) as a
    reproducible, steady-state ONE row, the parent's own decision call; LibreChat
    synthesizes the tool-not-found error locally rather than round-tripping the model a
    second time. (The rework item that produced this class quotes an earlier adversary
    run of the same scenario at two rows — this measurement disagrees with that number
    and is the one this test is built on; flagged rather than silently overwritten,
    see the item's own trail.) Either way, that shape satisfied every assertion the old
    test made. The assertions below — `minimum=3`, the tool_call's output must not be
    an error, the reply text must not be an error — are written so that same
    negative-control turn, exercised as an actual test method
    (`test_a_turn_with_no_subagents_configured_gets_an_error_the_assertions_reject`),
    fails them. That test asserts `pytest.raises(AssertionError)` around the shared
    helper, so the discrimination is measured, not claimed.

    BOTH SHAPES SubagentConfig SUPPORTS ARE MEASURED, because the mechanism differs
    enough between them that proving one would not have proven the other:
    `self: true` reuses the PARENT's own AgentInputs (its `clientOptions` — where the
    requesting user's identity is threaded in — never gets rebuilt), while an explicit
    `agent_ids` entry names a WHOLLY SEPARATE, independently persisted Agent document
    (a different author, potentially a different model). If attribution held only
    because a self-spawned child was never really a new AgentInputs, the explicit case
    would have been the one that leaked.
    """

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )

    # The only alias whose end_user the bill trusts — see TestChatPrincipalOnTheOneBill.
    SHARED_CHAT_ALIAS = "chat-surface::chat"

    # The model fakeprovider's CALL_SUBAGENT marker (and every completion it triggers,
    # parent and child alike) always answers as. Filtering the ledger to this exact
    # model excludes the zero-token real-provider rows LibreChat's own title/routing
    # logic writes on every agents-endpoint turn against `glm-*@deepinfra` (unreachable
    # in this hermetic bundle) — noise this test does not exist to characterize.
    SUBAGENT_MODEL = "fake-gpt-large"

    # ---------------------------------------------------------------- helpers

    def _chat_headers(self, client, chat_url: str) -> dict:
        return {
            "Authorization": f"Bearer {_chat_token(client, chat_url)}",
            "Cookie": oidc_login._cookie_header(client),
            "User-Agent": self.BROWSER_UA,
        }

    def _chat_identity(self, client, chat_url: str) -> tuple[str, str]:
        """(LibreChat's ObjectId for the signed-in user, their username)."""
        me = oidc_login.whoami(client, chat_url)
        user = me.get("user", me)
        oid = str(user.get("_id") or user.get("id") or "")
        assert re.fullmatch(r"[0-9a-f]{24}", oid), user
        return oid, user["username"]

    def _create_agent(self, client, chat_url: str, **fields) -> str:
        r = client.post(
            f"{chat_url}/api/agents",
            json={"provider": "Enterprise AI", "model": "fake-large", **fields},
            headers=self._chat_headers(client, chat_url), timeout=TIMEOUT,
        )
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    def _delete_agent(self, client, chat_url: str, agent_id: str) -> None:
        client.delete(
            f"{chat_url}/api/agents/{agent_id}",
            headers=self._chat_headers(client, chat_url), timeout=TIMEOUT,
        )

    def _send_subagent_turn(self, client, chat_url: str, agent_id: str, marker: str) -> dict:
        """One real turn against a persisted Agent, driving `marker` as the user's own
        message. Returns the persisted assistant message."""
        payload = {
            "text": marker,
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
        headers = self._chat_headers(client, chat_url)
        conversation_id, _ = chat_turn.start_turn(
            client, chat_url, payload, headers=headers, timeout=TIMEOUT * 3
        )
        return chat_turn.wait_for_reply(
            client, chat_url, conversation_id, headers=headers, timeout=TIMEOUT * 3
        )

    def _gateway_sql(self, env, sql: str) -> str:
        r = compose("exec", "-T", "postgres", "psql", "-U", env.get("POSTGRES_USER", "eai"),
                    "-d", "gateway", "-tA", "-c", sql, check=False)
        assert r.returncode == 0, f"psql failed: {sql}\n{r.stdout}\n{r.stderr}"
        return r.stdout.strip()

    def _ledger_now(self, env) -> str:
        return self._gateway_sql(env, "SELECT now()")

    def _spend_rows_since(self, env, since: str) -> list[dict]:
        """Every `LiteLLM_SpendLogs` row the shared chat key wrote for the fakeprovider
        subagent model since `since` — request id, end_user, alias, and token counts, so
        a caller can both count constituent calls and check every one's attribution."""
        out = self._gateway_sql(env, (
            "SELECT s.request_id || '|' || COALESCE(s.end_user, '') || '|' || "
            "COALESCE(s.metadata->>'user_api_key_alias', '') || '|' || "
            "s.prompt_tokens || '|' || s.completion_tokens "
            'FROM "LiteLLM_SpendLogs" s '
            f"WHERE s.model = '{self.SUBAGENT_MODEL}' "
            f"AND s.\"startTime\" >= '{since}'::timestamptz "
            'ORDER BY s."startTime"'
        ))
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            request_id, end_user, alias, prompt_tokens, completion_tokens = line.split("|")
            rows.append({
                "request_id": request_id,
                "end_user": end_user,
                "alias": alias,
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
            })
        return rows

    def _await_constituent_rows(self, env, since: str, minimum: int) -> list[dict]:
        """Poll rather than sleep-and-hope: the gateway flushes spend rows on a batch
        interval, so reading once risks seeing the parent's call but not the child's
        yet, and reporting an attribution gap that is really just a race."""
        deadline = time.monotonic() + 60
        rows: list[dict] = []
        while time.monotonic() < deadline:
            rows = [
                r for r in self._spend_rows_since(env, since)
                if r["prompt_tokens"] + r["completion_tokens"] > 0
            ]
            if len(rows) >= minimum:
                return rows
            time.sleep(2)
        raise AssertionError(
            f"expected at least {minimum} constituent completion(s) on the ledger, "
            f"found {len(rows)}: {rows}"
        )

    def _await_bill_row(self, control_plane_url, admin_headers, username: str) -> dict:
        deadline = time.monotonic() + 60
        bill: dict = {}
        while time.monotonic() < deadline:
            bill = httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()
            row = next(
                (r for r in bill["by_user_and_surface"]
                 if r["username"] == username and r["surface"] == "chat"),
                None,
            )
            if row:
                return row
            time.sleep(2)
        raise AssertionError(
            f"no chat spend ever appeared for {username}: "
            f"{bill.get('by_user_and_surface')}"
        )

    # A tool-not-found error is what a `subagent` tool_call looks like when no
    # delegation happened at all — this is the exact string LibreChat's tool-call
    # relay writes when the model asks for a tool the agent has no binding for.
    # Measured on this bundle against an agent with no `subagents` config.
    _NOT_FOUND_MARKERS = ("not found", "please fix your mistakes")

    def _assert_genuine_delegation(self, reply: dict, rows: list[dict], oid: str,
                                    minimum: int) -> None:
        """The shared assertion set both real-delegation tests apply to a passing turn,
        and the negative-control test below applies to a turn that never delegated —
        proving, by actually raising on that second input, that these assertions
        discriminate rather than merely accepting the good case.

        Checks, in order: (1) a tool_call literally named `subagent` was persisted —
        not just SOME tool_call, closing the gap where an ordinary tool-relay turn with
        no delegation at all could satisfy a bare `tool_calls(reply)` check; (2) that
        tool_call's own output is not the tool-not-found error string, i.e. a subagent
        actually ran rather than failing to bind; (3) the visible reply text is not the
        matching error string either — the 'coherent decomposed result' half of the
        done-condition, which the prior version of this class never checked at all;
        (4) at least `minimum` distinct completion rows landed on the ledger — the real,
        successful case always produces three (parent decision, child's own call, parent
        relay); a subagents-disabled turn produces fewer (measured steady-state on this
        bundle: exactly one, the parent's own decision call), which is why the negative
        control below calls this with `minimum=3` and expects it to raise;
        (5) every one of those rows is attributed to the requesting user's own identity
        on the shared chat credential — unchanged from the original assertion, kept
        because it was never in question.
        """
        calls = chat_turn.tool_calls(reply)
        subagent_calls = [
            c for c in calls if (c.get("tool_call") or {}).get("name") == "subagent"
        ]
        assert subagent_calls, (
            f"no tool_call named 'subagent' was persisted on the reply — a generic "
            f"tool_calls(reply) check would not have caught this: {calls}"
        )
        tool_call = subagent_calls[0]["tool_call"]
        output = (tool_call.get("output") or "").lower()
        assert not any(marker in output for marker in self._NOT_FOUND_MARKERS), (
            f"the subagent tool_call's own output is an error, not a delegation "
            f"result: {tool_call}"
        )

        text = chat_turn.reply_text(reply).lower()
        assert not any(marker in text for marker in self._NOT_FOUND_MARKERS), (
            f"the assistant's visible reply is an error string, not a coherent "
            f"decomposed result: {chat_turn.reply_text(reply)!r}"
        )

        assert len(rows) >= minimum, (
            f"expected >= {minimum} constituent completions (parent decision call, "
            f"the child subagent's own completion, and the parent's post-relay call) "
            f"on the ledger, found {len(rows)}: {rows}"
        )
        distinct_prompt_token_counts = {r["prompt_tokens"] for r in rows}
        assert len(distinct_prompt_token_counts) > 1, (
            f"every constituent row shares the same prompt_tokens count — that is the "
            f"signature of the same completion counted twice, not {minimum} genuinely "
            f"separate requests with different context depths: {rows}"
        )
        for row in rows:
            assert row["alias"] == self.SHARED_CHAT_ALIAS, (
                f"a subagent constituent call authenticated with an unexpected "
                f"credential: {row}"
            )
            assert row["end_user"] == oid, (
                f"a subagent constituent call was not attributed to the requesting "
                f"user's own identity ({oid}): {row}"
            )

    # ---------------------------------------------------------------- the tests

    def test_a_self_spawned_subagent_calls_own_completion_is_billed_to_the_requesting_user(
        self, chat_session, chat_url, env, control_plane_url, admin_headers
    ):
        """`self: true` — the subagent reuses the parent's own AgentInputs in an
        isolated child graph. The child's completion request is still a wholly separate
        HTTP call to the model; this proves it is not a separate PRINCIPAL."""
        oid, username = self._chat_identity(chat_session, chat_url)
        agent_id = self._create_agent(
            chat_session, chat_url,
            name=f"subagent-self-{uuid.uuid4().hex[:8]}",
            subagents={"enabled": True, "allowSelf": True},
        )
        try:
            since = self._ledger_now(env)
            nonce = uuid.uuid4().hex
            reply = self._send_subagent_turn(
                chat_session, chat_url, agent_id, f"CALL_SUBAGENT:self:self task {nonce}"
            )

            # Three: the parent's own decision-to-delegate call, the child subagent's
            # OWN completion, and the parent's post-relay call — the exact shape
            # measured on this bundle. `_assert_genuine_delegation` also checks the
            # tool_call is actually named `subagent`, that neither its output nor the
            # visible reply is the tool-not-found error, and that the rows are not all
            # the same completion counted twice — see the negative control test below
            # for proof this rejects a turn where no delegation happened.
            rows = self._await_constituent_rows(env, since, minimum=3)
            self._assert_genuine_delegation(reply, rows, oid, minimum=3)

            bill_row = self._await_bill_row(control_plane_url, admin_headers, username)
            assert bill_row["requests"] >= len(rows), (
                f"the bill undercounts the subagent's constituent calls: {bill_row} vs "
                f"{len(rows)} rows on the raw ledger"
            )

            bill = httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()
            principals = [r["username"] for r in bill["by_user_and_surface"]]
            assert agent_id not in principals, (
                f"a subagent call billed to the agent's own id, a synthetic principal "
                f"the requesting user never held: {principals}"
            )
            assert not any(
                r["username"] == "(unattributed)" and r["spend"] > 0
                for r in bill["by_user_and_surface"]
            ), (
                "money from this turn's subagent landed on the bill with no principal "
                f"at all: {bill['by_user_and_surface']}"
            )
        finally:
            self._delete_agent(chat_session, chat_url, agent_id)

    def test_an_explicit_subagent_on_a_separate_agent_document_is_also_billed_to_the_requesting_user(
        self, chat_session, chat_url, env, control_plane_url, admin_headers
    ):
        """`agent_ids: [...]` — the subagent is a wholly separate, independently
        persisted Agent document, not a reuse of the parent's own config. Proven
        distinctly from the self-spawn case above: if attribution only held because
        `self` never really built a new `AgentInputs`, this is the case that would leak
        — a subagent config resolved from a different Agent, potentially a different
        author, must still carry the REQUESTING user's identity through to its own
        completion call, not the child agent's author's.
        """
        oid, username = self._chat_identity(chat_session, chat_url)
        child_id = self._create_agent(
            chat_session, chat_url, name=f"subagent-child-{uuid.uuid4().hex[:8]}",
        )
        parent_id = self._create_agent(
            chat_session, chat_url,
            name=f"subagent-parent-{uuid.uuid4().hex[:8]}",
            subagents={"enabled": True, "allowSelf": False, "agent_ids": [child_id]},
        )
        try:
            since = self._ledger_now(env)
            nonce = uuid.uuid4().hex
            reply = self._send_subagent_turn(
                chat_session, chat_url, parent_id,
                f"CALL_SUBAGENT:{child_id}:explicit child task {nonce}",
            )

            rows = self._await_constituent_rows(env, since, minimum=3)
            self._assert_genuine_delegation(reply, rows, oid, minimum=3)

            bill_row = self._await_bill_row(control_plane_url, admin_headers, username)
            assert bill_row["requests"] >= len(rows)

            bill = httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()
            principals = [r["username"] for r in bill["by_user_and_surface"]]
            assert child_id not in principals and parent_id not in principals, (
                f"a subagent call billed to an agent id rather than a person: {principals}"
            )
        finally:
            self._delete_agent(chat_session, chat_url, parent_id)
            self._delete_agent(chat_session, chat_url, child_id)

    def test_a_turn_with_no_subagents_configured_gets_an_error_the_assertions_reject(
        self, chat_session, chat_url, env
    ):
        """Negative control (enterpriseaiframework-00d rework, adversary CHALLENGE 1).

        An agent with NO `subagents` config bound at all has no `subagent` tool for the
        model to call. The `CALL_SUBAGENT:...` marker is identical to the two tests
        above; only the agent's config differs. Two things are proven here, both by
        measurement rather than restated as fact:

        1. What actually happens without the feature: a tool_call named `subagent` IS
           persisted (fakeprovider still asks for it — that part doesn't depend on the
           agent's config), but its own recorded output is LibreChat's tool-not-found
           error, the visible reply is the matching error string, and FEWER THAN THREE
           `fake-gpt-large` rows land on the ledger — not the three-row delegated shape.
           Measured directly against this running bundle (three separate runs, including
           an 80-second patient poll with no early exit): the steady-state count is
           exactly ONE row — the parent's own decision call. LibreChat's tool-not-found
           error is synthesized locally once the bound-tool lookup fails; it does not
           make a second model round-trip the way a genuine tool-result relay does. The
           assertion below is written as `< 3` rather than pinned to `== 1` so it states
           the invariant that actually matters (this is not the three-row delegated
           shape) without being brittle to an incidental extra row.

        2. That `_assert_genuine_delegation` — the exact helper the two real-delegation
           tests above rely on — REJECTS this turn. `pytest.raises` around the call is
           not decorative: this is the same assertion set run over data it must fail on,
           which is the only way "the assertions can see the delegation they claim to
           prove" is something other than an unverified claim.
        """
        oid, username = self._chat_identity(chat_session, chat_url)
        agent_id = self._create_agent(
            chat_session, chat_url, name=f"subagent-none-{uuid.uuid4().hex[:8]}",
        )
        try:
            since = self._ledger_now(env)
            nonce = uuid.uuid4().hex
            reply = self._send_subagent_turn(
                chat_session, chat_url, agent_id, f"CALL_SUBAGENT:self:self task {nonce}"
            )

            calls = chat_turn.tool_calls(reply)
            subagent_calls = [
                c for c in calls if (c.get("tool_call") or {}).get("name") == "subagent"
            ]
            assert subagent_calls, (
                f"expected a tool_call named 'subagent' even without delegation "
                f"(fakeprovider still asks for it): {calls}"
            )
            output = (subagent_calls[0]["tool_call"].get("output") or "").lower()
            assert any(marker in output for marker in self._NOT_FOUND_MARKERS), (
                f"expected the tool-not-found error on the tool_call's own output, "
                f"measured on this bundle without a subagents config: {subagent_calls[0]}"
            )
            text = chat_turn.reply_text(reply)
            assert any(marker in text.lower() for marker in self._NOT_FOUND_MARKERS), (
                f"expected the visible reply to be the tool-not-found error string: "
                f"{text!r}"
            )

            rows = self._await_constituent_rows(env, since, minimum=1)
            assert len(rows) < 3, (
                f"expected fewer than the 3-row delegated shape without a subagent "
                f"actually running (measured steady-state on this bundle: exactly 1 "
                f"row, the parent's own decision call — no second model round-trip): "
                f"{rows}"
            )
            for row in rows:
                assert row["end_user"] == oid, (
                    "even a rejected tool call must still be attributed to the "
                    f"requesting user, not left unattributed: {row}"
                )

            # The actual discrimination proof: the SAME helper the real-delegation
            # tests trust must refuse to call this a genuine delegation.
            with pytest.raises(AssertionError):
                self._assert_genuine_delegation(reply, rows, oid, minimum=3)
        finally:
            self._delete_agent(chat_session, chat_url, agent_id)


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
# enterpriseaiframework-282 — a model picker, reasoning effort, a priced vision model
# ---------------------------------------------------------------------------

class TestModelPickerAndReasoningEffort:
    """The surface offers the gateway's catalogue: a model picker, reasoning-effort
    control, and at least one vision-capable model priced and selectable.

    DONE-CONDITION IS BEHAVIOURAL, not config: switch model in the UI, send a turn, and
    assert the ledger row names the model picked. A config assertion cannot see a surface
    that ignores the setting — which is exactly what this bundle was doing before this
    item: `interface.modelSelect` and `interface.parameters` were silently `false` in the
    served `/api/config` (zod's `.default({modelSelect:true,...})` only fires when the
    whole `interface:` key is absent, and librechat.yaml has always set one for
    privacyPolicy/artifacts/memories), so the model dropdown and the Parameters panel
    that carries `reasoning_effort` were both hidden regardless of what the gateway
    catalogue or the modelSpecs offered. Fixed in librechat.yaml alongside this test.

    Every turn below is driven through the real `/api/agents/chat` route with an
    explicit `model=`, exactly the wire shape LibreChat's own model dropdown sends when a
    user picks a catalogue entry that is not the modelSpec default — so a regression back
    to `modelSelect: false` (which does not change what a raw API call can request, only
    what the UI renders) would NOT be caught by driving the API directly. That half of
    the claim — the picker is actually rendered — is covered separately in
    tests/test_chat_surface_version.py against the served `/api/config`. This file covers
    the other half: a model a user picks is the model that gets billed.
    """

    def _chat_headers(self, client, chat_url: str) -> dict:
        return {
            "Authorization": f"Bearer {_chat_token(client, chat_url)}",
            "Cookie": oidc_login._cookie_header(client),
        }

    @staticmethod
    def _fakeprovider_url(env: dict) -> str:
        return f"http://localhost:{env.get('FAKEPROVIDER_PORT', '8090')}"

    def _prompts_containing(self, env: dict, nonce: str) -> list[dict]:
        """The raw captured-request entries fakeprovider kept for a nonce.

        Ground truth for "did the surface actually send X", not just "did a turn
        happen" — see fakeprovider/app.py's `_capture_prompt`/`/debug/prompts`, the same
        seam tests/test_skill_corpus.py already relies on for the identical reason.
        """
        r = httpx.get(
            f"{self._fakeprovider_url(env)}/debug/prompts",
            params={"contains": nonce}, timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        return r.json()

    def _since(self) -> str:
        """An explicit UTC-offset timestamp taken immediately before a turn.

        Not "the newest row for this model_group": the fakes are shared upstreams that
        every test in this class (and TestPricingIntegrity before it) sends traffic
        through, so a row that already existed before THIS turn was sent — e.g. an
        earlier $0 cache hit — would satisfy a plain "latest row" query without this
        turn's own row ever having landed yet, and the assertion would be reading
        somebody else's history. `startTime` is a `timestamptz`, so a naive string here
        would be read in the server's zone; the explicit `+00` is required, not cosmetic
        (same reasoning as TestPricingIntegrity.test_configured_models_record_nonzero_spend
        above).
        """
        return time.strftime("%Y-%m-%dT%H:%M:%S+00", time.gmtime(time.time() - 2))

    def _spend_row_since(self, env, model_group: str, since: str, timeout: float = 90.0):
        """Poll `LiteLLM_SpendLogs` for a row under `model_group` no older than `since`.

        `model_group` carries the ALIAS a caller requested (litellm's `model_name` in
        the gateway config), not the upstream id `litellm_params.model` resolves to — the
        two diverge for the bundle's fake upstreams by design (`fake-vision-large` maps
        to `openai/fake-gpt-vision`, matching every other fake entry in
        bundle/litellm/config.base.yaml), so `model_group` is the only column that
        answers "which catalogue entry did the picker actually send" — the literal
        DONE-CONDITION — rather than "which upstream served it". For a real Forge model
        the two are the same string, so this holds there too.

        Polls rather than reads once: the gateway batches spend rows for 7-13s
        (flush_spend_on_shutdown.handler's own comment) before they land in postgres.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = compose(
                "exec", "-T", "postgres", "psql", "-U", env.get("POSTGRES_USER", "eai"),
                "-d", "gateway", "-tA", "-F", "|", "-c",
                "SELECT spend, prompt_tokens, completion_tokens FROM "
                f"\"LiteLLM_SpendLogs\" WHERE model_group = '{model_group}' "
                f"AND \"startTime\" >= '{since}'::text::timestamptz "
                "ORDER BY \"startTime\" DESC LIMIT 1",
                check=False,
            )
            assert result.returncode == 0, f"psql failed\n{result.stdout}\n{result.stderr}"
            line = result.stdout.strip()
            if line:
                spend_s, prompt_s, completion_s = line.split("|")
                return float(spend_s), int(prompt_s), int(completion_s)
            time.sleep(3)
        pytest.fail(
            f"no LiteLLM_SpendLogs row landed under model_group={model_group!r} since "
            f"{since} — the turn was either never sent under that model, or the picker "
            "silently sent something else"
        )

    def test_switching_the_model_in_a_turn_bills_the_ledger_under_that_model(
        self, chat_session, chat_url, env
    ):
        """Switch to a catalogue model that is NOT the modelSpec default; the ledger
        must name it. `glm-5-2` is the `default: true` spec — always picking it would
        prove nothing about the picker, only that the pinned default still works."""
        nonce = uuid.uuid4().hex
        since = self._since()
        headers = self._chat_headers(chat_session, chat_url)
        chat_turn.send_turn(
            chat_session, chat_url, f"switch model {nonce}", "fake-small",
            "Enterprise AI", headers=headers, timeout=TIMEOUT,
        )
        spend, prompt_tokens, completion_tokens = self._spend_row_since(
            env, "fake-small", since
        )
        assert prompt_tokens > 0 and completion_tokens > 0, (
            "the picked model recorded no tokens — the turn did not actually reach it"
        )
        assert spend > 0, (
            "fake-small metered at $0 — an unpriced model would let this same test pass "
            "while the bill silently under-reported"
        )

    def test_the_vision_capable_model_is_offered_priced_and_selectable(
        self, chat_session, chat_url, env
    ):
        """`fake-vision-large` (bundle/litellm/config.base.yaml) is the no-account
        stand-in for the vision-language family Forge prices automatically the moment a
        real key is configured (qwen3-vl-30b-a3b@deepinfra and friends — see the entry's
        own comment). Proven here exactly like any other catalogue model: offered,
        selectable by picking it explicitly, and priced above zero once picked —
        enterpriseaiframework-020 (pasting a screenshot / uploading a PDF) is blocked on
        this being true.

        THE VISION CLAIM ITSELF (enterpriseaiframework-282 rework). Offered+priced+
        selectable alone proves a text echo, not vision — "vision-capable" was
        previously a substring of a model_name and nothing else: fakeprovider dropped
        every `image_url` content block before it ever computed the fake reply, and no
        request in this test sent one. Fixed on both sides:
          - bundle/litellm/config.base.yaml now sets `model_info.supports_vision: true`
            on this entry, so LiteLLM (and, through `/v1/models`, the surface) is told
            this model is vision-capable, not just named as though it were.
          - fakeprovider/app.py's `_message_text`/`extract_prompt` (see
            `_image_marker`) now turn an `image_url` block into a short deterministic
            marker instead of silently erasing it, so an image that reaches fakeprovider
            is observable rather than invisible.
        This test now actually uploads a real image the way LibreChat's own client does
        (`file_upload.upload_image` — reproduced from the running v0.8.7 image's
        `useFileHandling.ts`, a materially different upload path than the file_search
        document upload every other test in this suite uses) and attaches it to the
        turn, then reads fakeprovider's own `/debug/prompts` capture to prove the image
        actually reached the model's request — not merely that a turn against this
        model_name billed successfully, which a text-only turn would also do.
        """
        headers = self._chat_headers(chat_session, chat_url)
        token = _chat_token(chat_session, chat_url)
        offered = chat_session.get(
            f"{chat_url}/api/models",
            headers={
                "Authorization": f"Bearer {token}",
                "Cookie": oidc_login._cookie_header(chat_session),
            },
            timeout=TIMEOUT,
        ).json().get("Enterprise AI", [])
        assert "fake-vision-large" in offered, (
            f"the vision-capable stand-in is not in the offered catalogue: {offered}"
        )

        image_record = file_upload.upload_image(
            chat_session, chat_url, headers, "probe.png",
        )
        assert image_record.get("file_id"), (
            f"image upload carried no file_id: {image_record}"
        )
        assert str(image_record.get("type", "")).startswith("image/"), (
            f"the uploaded file was not stored as an image, so it would never be routed "
            f"to addImageURLs: {image_record}"
        )

        nonce = uuid.uuid4().hex
        since = self._since()
        chat_turn.send_turn(
            chat_session, chat_url, f"pick the vision model {nonce}", "fake-vision-large",
            "Enterprise AI", files=[image_record], headers=headers, timeout=TIMEOUT,
        )
        spend, prompt_tokens, completion_tokens = self._spend_row_since(
            env, "fake-vision-large", since
        )
        assert prompt_tokens > 0 and completion_tokens > 0
        assert spend > 0, (
            "the vision-capable model metered at $0 — an unpriced model meters at $0, "
            "so budgets never trip and the bill under-reports (the exact failure "
            "/admin/unpriced exists to catch)"
        )

        captured = self._prompts_containing(env, nonce)
        assert captured, (
            f"fakeprovider never saw a request carrying nonce {nonce!r} — the turn did "
            "not actually reach it"
        )
        assert "[image:" in captured[-1]["prompt"], (
            f"fakeprovider's own capture of the request it received has no image marker "
            f"in it: {captured[-1]['prompt']!r} — the uploaded image never reached the "
            "model's request, so 'vision-capable' is still unproven"
        )

    def test_reasoning_effort_does_not_undo_token_accounting(
        self, chat_session, chat_url, env
    ):
        """The regression this item's CONSTRAINTS section warns against by name:
        deploy/gateway/strip_reasoning.py strips reasoning CONTENT from the response but
        deliberately leaves token counts alone, and a reasoning-effort control must not
        undo that — a turn sent with `reasoning_effort` set must still record real,
        nonzero prompt/completion tokens and nonzero spend, exactly like one sent without
        it. `reasoning_effort` is a genuine `tConversationSchema` field (verified against
        the running v0.8.7 image's data-provider bundle) that a `custom`-endpoint turn
        forwards straight through to `litellm_params`, so this sends it exactly as the
        Parameters panel would rather than as a synthetic shape.

        WHY THIS ALSO CHECKS fakeprovider'S RING BUFFER, NOT JUST TOKENS
        (enterpriseaiframework-282 rework). Nonzero tokens/spend are insensitive to the
        feature under test: they pass identically whether `reasoning_effort` actually
        left the surface or was silently dropped anywhere between the Parameters panel
        and the upstream request — a plain turn with no `reasoning_effort` at all would
        satisfy the same two assertions. `GET /debug/prompts` is fakeprovider's own
        record of what it actually received on the wire (the same seam
        tests/test_skill_corpus.py already uses to prove an exact string reached the
        upstream), so asserting on the captured `reasoning_effort` is ground truth for
        "the value left the surface", not an inference from a side effect of it.
        """
        nonce = uuid.uuid4().hex
        since = self._since()
        headers = self._chat_headers(chat_session, chat_url)
        chat_turn.send_turn(
            chat_session, chat_url, f"reasoning effort {nonce}", "fake-large",
            "Enterprise AI", reasoning_effort="low", headers=headers, timeout=TIMEOUT,
        )
        spend, prompt_tokens, completion_tokens = self._spend_row_since(
            env, "fake-large", since
        )
        assert prompt_tokens > 0 and completion_tokens > 0, (
            "a turn sent with reasoning_effort recorded no tokens — a reasoning-effort "
            "control must not undo strip_reasoning's token accounting"
        )
        assert spend > 0

        captured = self._prompts_containing(env, nonce)
        assert captured, (
            f"fakeprovider never saw a request carrying nonce {nonce!r} — the turn did "
            "not actually reach it"
        )
        assert captured[-1]["reasoning_effort"] == "low", (
            f"fakeprovider's own capture of the request it received shows "
            f"reasoning_effort={captured[-1]['reasoning_effort']!r}, not 'low' — the "
            "Parameters panel's setting did not reach the upstream request, even though "
            "the turn still billed and recorded tokens normally"
        )


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
        # rag-api holds a virtual key too (enterpriseaiframework-c7c), and the exit path
        # revokes EVERY key, not just chat's. Restoring only chat left file search dead
        # for the next run: a second consecutive full-suite run failed 4/4 on a 500 from
        # the upload endpoint, with rag-api logging 401 token_not_found_in_db - a failure
        # that names the wrong thing entirely. Within a single run the ordering hides it
        # (file search at 29%, the exit test at 51%), so only a re-run surfaces it.
        # provision-rag-key.sh is idempotent and re-mints on an invalid key.
        subprocess.run([str(BUNDLE / "bin" / "provision-rag-key.sh")], check=False,
                       capture_output=True)
        compose("up", "-d", "rag-api", check=False)
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

    # -----------------------------------------------------------------------
    # The archive must still name people after the exit has emptied the key table
    # -----------------------------------------------------------------------

    def _spend_rows(self, control_plane_url, admin_headers) -> list[dict]:
        r = httpx.get(f"{control_plane_url}/admin/export/spend",
                      headers=admin_headers, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        return list(csv.DictReader(io.StringIO(r.text)))

    def test_the_export_still_names_the_spender_after_the_exit_revokes_every_key(
        self, tmp_path, gateway_url, control_plane_url, admin_headers, master_headers
    ):
        """spend.csv is the one rendering of the ledger that outlives the deployment, and
        it was the least attributed of the three — because of the ORDER the exit runs in.

        `exit.sh full` revokes every virtual key, and revocation DELETEs from
        LiteLLM_VerificationToken. An export that attributes through that table therefore
        empties out at precisely the moment it becomes the customer's only record.
        Measured on the cluster before this test existed: 265 of 477 exported rows with no
        principal at all, over a ledger whose bill attributed all but 42 requests.

        Finding 25 established the join is wrong and fixed the bill. The export kept its
        own copy of it and stayed broken through two more findings, which is why the fix
        under test is a SHARED expression rather than a third correct query.

        The existing export tests assert spend.csv EXISTS. A presence check is not an
        attribution check — that is what let this survive. So this exports, revokes
        through the real exit endpoint, exports again, and asserts the archive still names
        the person who spent the money.
        """
        alias = f"exitattr-{uuid.uuid4().hex[:8]}::ide"
        username = alias.split("::")[0]
        created = httpx.post(
            f"{gateway_url}/key/generate", headers=master_headers,
            json={"key_alias": alias}, timeout=TIMEOUT,
        )
        assert created.status_code == 200, created.text
        key = created.json()["key"]

        spent = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "fake-large",
                  "messages": [{"role": "user", "content": f"exit-attr {uuid.uuid4().hex}"}]},
            timeout=TIMEOUT,
        )
        assert spent.status_code == 200, spent.text

        # The gateway writes the spend row asynchronously.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            rows = self._spend_rows(control_plane_url, admin_headers)
            if any(r["key_alias"] == alias for r in rows):
                break
            time.sleep(2)
        else:
            pytest.fail(f"{alias} never reached the exported ledger; nothing to test")

        before_dir = tmp_path / "before"
        before_dir.mkdir()
        before = {r["request_id"]: r
                  for r in csv.DictReader(
                      (self._export(before_dir) / "spend.csv").open(newline=""))}
        mine_before = {rid for rid, r in before.items() if r["key_alias"] == alias}
        assert mine_before, (
            f"the pre-revocation export does not carry {alias}; the rest of this test "
            "would prove nothing"
        )
        for rid in mine_before:
            assert before[rid]["principal"] == username, before[rid]

        # The real thing, not a key/delete shortcut: this is the endpoint exit.sh calls,
        # and it deletes the whole table the old query attributed from.
        revoked = httpx.post(f"{control_plane_url}/admin/exit/revoke-all",
                             headers=admin_headers, timeout=TIMEOUT)
        assert revoked.status_code == 200, revoked.text
        assert alias not in gateway_aliases(gateway_url, master_headers), (
            f"{alias} survived revoke-all, so this proves nothing about attribution "
            "surviving a deleted key"
        )

        after_dir = tmp_path / "after"
        after_dir.mkdir()
        after = {r["request_id"]: r
                 for r in csv.DictReader(
                     (self._export(after_dir) / "spend.csv").open(newline=""))}

        # 1. The row this test created still names its spender.
        missing = mine_before - set(after)
        assert not missing, (
            f"{len(missing)} of this test's requests vanished from the export after "
            "revocation; the archive lost rows, not just names"
        )
        for rid in sorted(mine_before):
            row = after[rid]
            assert row["key_alias"] == alias, (
                f"request {rid} exported with key_alias {row['key_alias']!r} after the "
                f"exit revoked {alias}. The exit path revokes BEFORE it exports, so this "
                "is the state every real archive is written in — the customer would leave "
                "with a ledger that cannot say who spent the money"
            )
            assert row["principal"] == username, (
                f"request {rid} is billed to {row['principal']!r} rather than {username!r}"
            )
            assert row["surface"] == "ide", (
                f"request {rid} lost its surface ({row['surface']!r}); the surface is "
                "carried by the same alias and dies with the same join"
            )

        # 2. THE ROWS THIS TEST DID NOT CREATE. The measured defect was population-wide —
        #    half the ledger, most of it written by other surfaces long before. Revocation
        #    must not push a single further row into the anonymous bucket.
        common = set(before) & set(after)
        blank_before = {rid for rid in common if not before[rid]["key_alias"]}
        blank_after = {rid for rid in common if not after[rid]["key_alias"]}
        assert blank_after <= blank_before, (
            f"revoking every key stripped the alias from {len(blank_after - blank_before)} "
            f"row(s) that had one a moment earlier (of {len(common)} in both exports). "
            "This is the cluster measurement reproduced: attribution that evaporates on "
            "the way out"
        )
        unnamed = {rid for rid in common if not after[rid]["principal"]}
        assert not unnamed, (
            f"{len(unnamed)} exported row(s) have an empty principal. A blank cell reads "
            "as no data; a row that genuinely cannot be attributed must say so"
        )

        # 3. THE COLUMNS THIS CHANGE DID NOT TOUCH must be byte-identical across the two
        #    exports. The ledger is immutable; only the query over it changed.
        for rid in sorted(common):
            for col in ("start_time", "end_time", "model", "end_user", "spend",
                        "prompt_tokens", "completion_tokens", "total_tokens", "cache_hit"):
                assert before[rid][col] == after[rid][col], (
                    f"request {rid} column {col} changed between exports: "
                    f"{before[rid][col]!r} -> {after[rid][col]!r}"
                )

        # 4. The archive and the bill must agree about this person, over the same money.
        #    Two renderings that disagree is finding 34; the export was the third one.
        bill = httpx.get(f"{control_plane_url}/admin/spend", headers=admin_headers,
                         timeout=TIMEOUT).json()["by_user_and_surface"]
        billed = [r for r in bill if r["username"] == username]
        assert billed, (
            f"the bill has no row for {username} after revocation while the export does; "
            "the two renderings of the ledger disagree about who spent the money"
        )
        csv_requests = sum(1 for r in after.values() if r["principal"] == username)
        assert sum(r["requests"] for r in billed) == csv_requests, (
            f"bill says {sum(r['requests'] for r in billed)} requests for {username}, "
            f"exported ledger says {csv_requests}"
        )
