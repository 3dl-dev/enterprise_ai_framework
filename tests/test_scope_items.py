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
import uuid

import httpx
import pytest

import oidc_login
from conftest import BUNDLE, DOGFOOD_USER, compose, portal_get, set_user_enabled


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

# Put this in a prompt and the stand-in provider answers 500. Kept in step with
# fakeprovider/app.py's FAKE_MARKER; it is the only way to exercise the gateway's failure
# path, because a provider that always succeeds cannot produce one.
FAKE_FAIL_MARKER = "__fakeprovider_fail_500__"


def _table_headers(html: str, table_id: str) -> list[str]:
    """The column headings a table declares, in order."""
    start = html.index(f'id="{table_id}"')
    thead = html.index("<thead>", start)
    end = html.index("</thead>", thead)
    return [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<th\b[^>]*>(.*?)</th>", html[thead:end], re.S)
    ]


def _row_template_cells(js: str, marker: str) -> list[str]:
    """The cells one `tr.innerHTML = ...` template emits, in order.

    Whole-line // comments are dropped first. They are prose, they sit in the middle of
    these templates, and a semicolon inside one would otherwise look like the end of the
    statement and silently truncate the count — which would make this helper report a
    column mismatch that is not there, or miss one that is.
    """
    body = "\n".join(
        line for line in js.splitlines() if not line.strip().startswith("//")
    )
    at = body.index(marker)
    start = body.rindex("tr.innerHTML", 0, at)
    end = body.index(";", at)
    return ["<td" + part for part in body[start:end].split("<td")[1:]]


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

        # Unique content, so this asserts on a row this call produced rather than on one
        # left by an earlier run. A cache hit does write its own spend row (measured —
        # see finding 10), but it writes $0 against the same alias, which would make the
        # spend assertions below depend on suite ordering.
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
            # Every probe must be a cache miss, because this test is about spend
            # accumulating until it crosses the cap, and a cached reply adds nothing to
            # spend — the loop below would run to its deadline and fail for the wrong
            # reason. It is NOT because the cache escapes enforcement: it does not, and
            # TestCacheHitsBudgetAndTheBill measures that directly.
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


class TestCacheHitsBudgetAndTheBill:
    """The gateway's own response cache, against refusal and against the bill.

    enterpriseaiframework-d58 / finding 10. Two claims were open about it:

      1. that an over-budget key is still served when the answer is sitting in the
         cache, because the budget is not consulted on a hit;
      2. that the $0 those rows carry means the bill under-reports.

    Both are false, and the tests below are how that is known rather than believed. The
    first was inferred from a probe that only ever showed a cache hit costing $0 — which
    it does — and never once put an over-budget key in front of a cached prompt. It is
    checked in `user_api_key_auth`, strictly before any cache lookup, so refusal wins.
    The second is answered by the same fact that makes $0 correct: no provider was
    called, so no money was spent.

    THE TRAP THESE TESTS HAVE TO AVOID. The fake provider's reply — text, completion id,
    token counts — is a pure function of (model, prompt). A cached reply and a fresh one
    are byte-identical, so "the id matched" is not evidence of a cache hit, and a test
    resting on it passes whether the cache works or not. Every assertion here about
    something being cached is therefore made against the provider's own call counter.

    And the refusal test needs the converse control: a 400 proves nothing if the entry
    had quietly gone. So after the refusal it restores the budget, asks again, and
    requires both a 200 and an unchanged provider call count — the answer was there the
    whole time, and the budget is what stood between the caller and it.
    """

    # fake-large's configured price, from bundle/litellm/config.base.yaml. Hardcoded on
    # purpose: reading it from the same config the gateway read would make the check
    # circular, and this asserts the money, not the plumbing.
    INPUT_COST = 0.000003
    OUTPUT_COST = 0.000015

    @staticmethod
    def _new_key(gateway_url, master_headers, alias, max_budget=None):
        body = {"key_alias": alias, "models": ["fake-large"]}
        if max_budget is not None:
            body["max_budget"] = max_budget
        r = httpx.post(
            f"{gateway_url}/key/generate", headers=master_headers, json=body, timeout=TIMEOUT
        )
        assert r.status_code == 200, r.text
        return r.json()["key"]

    @staticmethod
    def _ask(gateway_url, key, prompt):
        return httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "fake-large", "messages": [{"role": "user", "content": prompt}]},
            timeout=TIMEOUT,
        )

    @staticmethod
    def _provider_calls(fakeprovider_url, prompt):
        r = httpx.get(
            f"{fakeprovider_url}/debug/calls", params={"prompt": prompt}, timeout=TIMEOUT
        )
        assert r.status_code == 200, r.text
        return r.json()["calls"]

    @staticmethod
    def _provider_boot(fakeprovider_url):
        """Which incarnation of the fake provider did the counting.

        The counter is in-process, so a restart resets it to zero — and the direction that
        breaks is the silent one: a LOW count is how "the gateway did not call the provider,
        so this was a cache hit" is proved, and a restarted provider produces a low count
        with the cache switched off entirely. Every assertion that reads a low count as
        evidence of caching pairs it with this, so a restart mid-measurement fails the test
        instead of confirming its hypothesis.
        """
        r = httpx.get(f"{fakeprovider_url}/debug/calls", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        boot = r.json().get("boot_id")
        assert boot, (
            "the fake provider does not publish which process counted, so a restart "
            "mid-test would zero the counter and read as a cache hit"
        )
        return boot

    def test_a_repeated_request_is_answered_without_calling_the_provider(
        self, gateway_url, master_headers, fakeprovider_url
    ):
        """The premise every other test in this class rests on."""
        key = self._new_key(gateway_url, master_headers, f"cache-{uuid.uuid4().hex[:8]}::chat")
        prompt = f"cache me {uuid.uuid4().hex}"

        boot = self._provider_boot(fakeprovider_url)
        assert self._provider_calls(fakeprovider_url, prompt) == 0, "prompt was not fresh"

        first = self._ask(gateway_url, key, prompt)
        assert first.status_code == 200, first.text
        assert self._provider_calls(fakeprovider_url, prompt) == 1

        second = self._ask(gateway_url, key, prompt)
        assert second.status_code == 200, second.text
        assert self._provider_calls(fakeprovider_url, prompt) == 1, (
            "the gateway called the provider again for a prompt it had already answered "
            "— the response cache is not doing anything, and every cache assertion in "
            "this class is vacuous"
        )
        # A restarted provider zeroes the counter, and a zeroed counter is exactly what a
        # working cache looks like. Checked last, so it guards every count above it.
        assert self._provider_boot(fakeprovider_url) == boot, (
            "the fake provider restarted mid-test, so its call counter was reset and the "
            "assertions above proved nothing about the cache"
        )
        assert second.json()["choices"][0]["message"]["content"] == \
            first.json()["choices"][0]["message"]["content"]

    def test_an_over_budget_key_is_refused_even_when_the_answer_is_cached(
        self, gateway_url, master_headers, fakeprovider_url
    ):
        """The claim the product is sold on: past the budget, the layer says no.

        Not "says no unless it happens to have the answer lying around".
        """
        alias = f"cachebudget-{uuid.uuid4().hex[:8]}::chat"
        # One fake-large exchange costs ~$0.000192, so the second one crosses this.
        budget = 0.00025
        key = self._new_key(gateway_url, master_headers, alias, max_budget=budget)
        prompt = f"cached and over budget {uuid.uuid4().hex}"

        assert self._ask(gateway_url, key, prompt).status_code == 200
        assert self._ask(gateway_url, key, prompt).status_code == 200
        assert self._provider_calls(fakeprovider_url, prompt) == 1, \
            "the answer is not in the cache, so this test would prove nothing"

        # Spend past the cap on a different prompt, so the cached one is untouched.
        assert self._ask(gateway_url, key, f"push over {uuid.uuid4().hex}").status_code == 200

        # Enforcement reads spend the gateway flushes on a batch interval, so allow it
        # to lag. Each attempt re-asks the cached prompt: if the cache did outrank the
        # budget, every one of these would come back 200 until the deadline.
        deadline = time.monotonic() + 90
        refusal = None
        while time.monotonic() < deadline:
            r = self._ask(gateway_url, key, prompt)
            if r.status_code >= 400:
                refusal = r
                break
            time.sleep(3)

        assert refusal is not None, (
            "an over-budget key was served the cached answer — budget enforcement is "
            "happening after the cache lookup instead of before it"
        )
        assert refusal.status_code == 400, refusal.text
        assert "budget" in refusal.text.lower(), refusal.text

        # THE CONTROL. Without this, the 400 above is equally consistent with the cache
        # entry having expired or been evicted, and the test would be asserting nothing.
        # Lift the budget and ask again: a 200 whose answer still did not cost a provider
        # call proves the entry was there throughout, and that the budget is what refused.
        raised = httpx.post(
            f"{gateway_url}/key/update",
            headers=master_headers,
            json={"key": key, "max_budget": 100.0},
            timeout=TIMEOUT,
        )
        assert raised.status_code == 200, raised.text

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            after = self._ask(gateway_url, key, prompt)
            if after.status_code == 200:
                break
            time.sleep(3)
        assert after.status_code == 200, (
            f"budget raised but the key is still refused: {after.text}"
        )
        assert self._provider_calls(fakeprovider_url, prompt) == 1, (
            "the provider was called again, so the cache entry had gone — the refusal "
            "above cannot be attributed to the budget"
        )

    def test_a_cache_hit_bills_zero_and_the_bill_says_so(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url
    ):
        """The billing ruling, locked in.

        A cache hit costs the company nothing, so it bills $0 — putting a list price
        there would put a number in the ledger that no invoice will ever confirm. But it
        is not silent: the row is written, attributed to the key that asked, with the
        real token counts, and the bill reports how many of a person's requests were
        answered that way. A line reading "2 requests, $0.000192" with no further
        explanation is what got these rows read as lost money in the first place.
        """
        username = f"cachebill-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")
        prompt = f"bill the cache {uuid.uuid4().hex}"

        first = self._ask(gateway_url, key, prompt)
        assert first.status_code == 200, first.text
        usage = first.json()["usage"]
        expected_spend = (
            usage["prompt_tokens"] * self.INPUT_COST
            + usage["completion_tokens"] * self.OUTPUT_COST
        )

        second = self._ask(gateway_url, key, prompt)
        assert second.status_code == 200, second.text
        assert self._provider_calls(fakeprovider_url, prompt) == 1, \
            "second call reached the provider; there is no cache hit to bill"

        deadline = time.monotonic() + 90
        row = None
        while time.monotonic() < deadline:
            bill = httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()
            row = next(
                (r for r in bill["by_user_and_surface"] if r["username"] == username), None
            )
            if row and row["requests"] >= 2:
                break
            time.sleep(3)

        assert row is not None, "the cache-hit traffic never reached the bill at all"
        assert row["requests"] == 2, f"expected the miss and the hit, got {row}"
        assert row["cached_requests"] == 1, (
            f"the bill cannot say which of these requests were free: {row}"
        )
        # The money: one upstream call was made and one was avoided, so the line carries
        # exactly one call's cost. This is the assertion that would catch a cache hit
        # being priced at list, and the one that keeps the bill reconcilable against a
        # provider invoice that never saw the second request.
        assert row["spend"] == pytest.approx(expected_spend, rel=1e-6), (
            f"expected exactly one call's cost ({expected_spend}), got {row['spend']}"
        )
        # Usage is not lost: both requests' tokens are counted, so an operator who wants
        # to charge back at list price can, from this row, without the cost column lying.
        assert row["prompt_tokens"] == 2 * usage["prompt_tokens"]
        assert row["completion_tokens"] == 2 * usage["completion_tokens"]

        assert "cached_requests" in bill["totals"], bill["totals"]
        assert bill["totals"]["cached_requests"] >= 1

        # The other rendering of the same money. Finding f8c is that `make spend` and the
        # portal, built from one query, still managed to disagree — so a number added to
        # one of them gets checked against the other here rather than assumed.
        #
        status, body = portal_get("/portal/api/spend", username)
        assert status == 200, body[:300]
        portal = json.loads(body)
        chat_row = next(
            (s for s in portal["by_surface"] if s["surface"] == "chat"), None
        )
        assert chat_row is not None, f"the portal shows this user nothing: {portal}"
        assert chat_row["requests"] == row["requests"], (portal, row)
        assert chat_row["cached_requests"] == row["cached_requests"], (
            f"the operator's bill and the user's own page disagree about how many of "
            f"their requests were free: portal {chat_row}, /admin/spend {row}"
        )
        assert chat_row["spend"] == pytest.approx(row["spend"], rel=1e-6), (portal, row)
        assert portal["total"]["cached_requests"] == 1, portal

    def _bill_row(self, control_plane_url, admin_headers, username, settle=25):
        """This user's line on the operator's bill, after the ledger has had time to flush.

        The gateway batches its writes, so reading immediately measures the batch interval
        rather than the behaviour.
        """
        time.sleep(settle)
        bill = httpx.get(
            f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
        ).json()
        return next(
            (r for r in bill["by_user_and_surface"] if r["username"] == username), None
        )

    # THREE WAYS A REQUEST CAN END WITHOUT AN ANSWER, AND THEY DO NOT AGREE
    #
    # The three tests below replace one that asserted "a refusal is not billed" as a flat
    # claim, checked it against a single budget refusal, and was therefore both passing
    # and wrong: it advertised a general rule while exercising one special case.
    #
    #   budget exceeded      refused in user_api_key_auth, before the router
    #   model not entitled   refused in user_api_key_auth, before the router
    #   upstream 500         past the router, onto the failure callback
    #
    # ALL THREE WRITE A ROW. That is a correction: this block used to say the first two
    # reached the ledger "not at all", on the strength of a measurement that read the
    # REFUSED USER's line on the bill and found the right number there. Re-measured against
    # the raw ledger, the refusals do write rows — status 'failure', spend 0, zero tokens —
    # but with NO key alias, so they land under `(unattributed)` rather than on the user's
    # line, which is why looking at the user's line could not see them. The tests below are
    # still correct about the user's line; what they never checked, and
    # TestARefusalIsNotAFailedRequestAndIsNotUsage now does, is the rest of the ledger.
    #
    # The ruling is that a refusal is a request nowhere, on any line: `requests` counts
    # what the gateway ADMITTED, provider failures included and named by failed_requests,
    # while a refusal is reported beside it as refused_requests. See metering._FAILED for
    # the ruling and the option the founder may still take instead.

    def test_a_budget_refusal_is_not_billed_as_usage(
        self, gateway_url, control_plane_url, master_headers, admin_headers
    ):
        """Past the cap, the refusals themselves cost nothing and count as nothing."""
        username = f"refusedbill-{uuid.uuid4().hex[:8]}"
        key = self._new_key(
            gateway_url, master_headers, f"{username}::chat", max_budget=0.00025
        )
        served = 0
        refusals = 0
        # Six refusals, not one. With a single refusal, "billed" and "not billed" differ
        # by one request, which is indistinguishable from a row that had not flushed yet;
        # at six the two answers are 6 apart and no timing story explains the gap.
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline and refusals < 6:
            # Unique every time: this test is about what a refusal writes, so no request
            # may be answered from the cache and skew the served count.
            r = self._ask(gateway_url, key, f"refused {uuid.uuid4().hex}")
            if r.status_code >= 400:
                assert "budget" in r.text.lower(), f"refused for some other reason: {r.text}"
                refusals += 1
            else:
                # Counted, not forbidden. "Never served again after the first refusal" is
                # a stricter claim than this test needs, and enforcement reads a spend
                # figure the gateway refreshes on an interval — so a 200 arriving just
                # after a 400 is a plausible race rather than a defect, and asserting it
                # away would buy nothing but an intermittent failure. What is asserted
                # below holds whatever order they arrive in: every served request appears
                # on the bill and no refused one does.
                served += 1
            time.sleep(2)
        assert refusals == 6, f"only {refusals} refusals; nothing to measure"
        assert served >= 1, "nothing was served, so there is no baseline to compare against"

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the served calls are missing from the bill"
        assert row["requests"] == served, (
            f"{served} requests were served and {refusals} refused, but the bill counts "
            f"{row['requests']} — a budget refusal was written to the ledger as usage: {row}"
        )
        assert row["cached_requests"] == 0, row

    def test_a_request_the_key_may_not_make_is_not_billed_as_usage(
        self, gateway_url, control_plane_url, master_headers, admin_headers
    ):
        """The other pre-router refusal: a model this key was never entitled to.

        Separate from the budget test on purpose. Both are refused by the same dependency
        today, but they are different rules and could stop sharing a code path; a test
        that covers one and speaks for both is how the claim got wrong the first time.
        """
        username = f"denied-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")

        assert self._ask(gateway_url, key, f"allowed {uuid.uuid4().hex}").status_code == 200

        for _ in range(3):
            denied = httpx.post(
                f"{gateway_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                # The key was issued for fake-large only.
                json={"model": "fake-small",
                      "messages": [{"role": "user", "content": f"nope {uuid.uuid4().hex}"}]},
                timeout=TIMEOUT,
            )
            assert denied.status_code >= 400, denied.text
            assert "model" in denied.text.lower(), denied.text

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the one allowed call is missing from the bill"
        assert row["requests"] == 1, (
            f"one call was served and three were denied at the key's model list, but the "
            f"bill counts {row['requests']} requests: {row}"
        )

    def test_an_upstream_failure_is_billed_as_a_request_at_zero_and_is_not_a_cache_hit(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url
    ):
        """The class that IS written to the ledger — and the guard on cached_requests.

        When the provider itself fails, the request is past the router and the failure
        callback writes a spend row: status 'failure', spend 0, no tokens. The bill counts
        it in `requests`. That is asserted here as the behaviour it is, not as the
        behaviour it should be — whether a request nobody was served belongs in a request
        count is a product decision, raised as enterpriseaiframework-e69 / finding 36.

        THE PART THAT BELONGS TO THIS ITEM. Those rows are $0 and they are NOT cache hits,
        so the bill now has two kinds of zero in it and cached_requests must only ever
        claim the one it means. If the predicate behind cached_requests were ever loosened
        from the cache_hit column to something like "spend = 0", every upstream failure
        would be reported to the operator as a request that was free — money that was
        never spent because nothing was delivered, dressed up as an efficiency. This test
        fails the moment that happens.
        """
        username = f"upfail-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")

        assert self._ask(gateway_url, key, f"good {uuid.uuid4().hex}").status_code == 200

        failures = 3
        for _ in range(failures):
            prompt = f"{FAKE_FAIL_MARKER} {uuid.uuid4().hex}"
            broke = self._ask(gateway_url, key, prompt)
            assert broke.status_code >= 500, (
                f"the provider was asked to fail and did not: {broke.status_code} "
                f"{broke.text[:200]}"
            )
            # The upstream really was reached — which is what puts this request on the
            # failure-callback path rather than the pre-router refusal path. Without this
            # the test would pass just as well if the gateway had rejected it locally,
            # and it would then be measuring the wrong class entirely.
            assert self._provider_calls(fakeprovider_url, prompt) >= 1, (
                "the gateway never called the provider, so this is not an upstream failure"
            )

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the successful call is missing from the bill"
        assert row["requests"] == 1 + failures, (
            f"one call was served and {failures} failed at the provider; the bill counts "
            f"{row['requests']} requests. e69 ruled that `requests` counts what the "
            f"gateway ADMITTED and names the failures separately, so this must stay "
            f"{1 + failures} and failed_requests must be {failures}: {row}"
        )
        assert row["failed_requests"] == failures, (
            f"the failures are in `requests` but nothing says so — which is the whole "
            f"defect finding 41 recorded: {row}"
        )
        assert row["cached_requests"] == 0, (
            f"a failed request was reported to the operator as a FREE request. Nothing "
            f"was delivered and nothing was cached; cached_requests must count cache hits "
            f"and only cache hits: {row}"
        )
        # The money is still right even though the count is not: a failure buys nothing,
        # so exactly one call's worth of spend is on the line.
        assert row["spend"] > 0, row
        assert row["prompt_tokens"] > 0 and row["completion_tokens"] > 0, row

    def test_the_operator_console_reports_cached_requests(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url, env
    ):
        """/portal/api/admin/overview — the operator's own view of the same free requests.

        This endpoint had never been touched by a test, and could not be: the bundle set
        no PORTAL_ADMINS, so require_admin_user answered 404 to every name in it,
        including the account the bundle bootstraps. Only the k8s deployment set it. It is
        set in the bundle now, defaulted to BOOTSTRAP_USER, which is what makes this test
        able to exist at all — the endpoint was previously verified by reading portal.py,
        which is not verification.

        The 404 for a non-operator is asserted in the same test rather than a separate
        one, because a 200 here means nothing if it would also be 200 for everybody: the
        assertion that matters is that the console distinguishes the two.
        """
        admin = env.get("BOOTSTRAP_USER", "baron")
        username = f"adminview-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")
        prompt = f"operator sees this {uuid.uuid4().hex}"

        assert self._ask(gateway_url, key, prompt).status_code == 200
        assert self._ask(gateway_url, key, prompt).status_code == 200
        assert self._provider_calls(fakeprovider_url, prompt) == 1, \
            "the second call reached the provider, so there is no free request to report"

        # THE CONTROL. A stranger must not see the operator console, and must not learn
        # that there is one — 404, not 403.
        status, body = portal_get("/portal/api/admin/overview", username)
        assert status == 404, (
            f"{username} is not in PORTAL_ADMINS but was served the operator console "
            f"anyway: {status} {body[:300]}"
        )

        # Now as the operator. Poll: this reads the same batched ledger as the bill.
        deadline = time.monotonic() + 90
        person = None
        while time.monotonic() < deadline:
            status, body = portal_get("/portal/api/admin/overview", admin)
            assert status == 200, (
                f"{admin} is PORTAL_ADMINS but the console refused them: {status} "
                f"{body[:300]}"
            )
            overview = json.loads(body)
            person = next(
                (p for p in overview["people"] if p["username"] == username), None
            )
            if person and person["requests"] >= 2:
                break
            time.sleep(3)

        assert person is not None, (
            f"the operator console does not show this user at all: "
            f"{[p['username'] for p in overview['people']]}"
        )
        assert person["requests"] == 2, person
        assert person["cached_requests"] == 1, (
            f"the operator cannot see which of this person's requests were free: {person}"
        )
        # Per surface as well as per person. The two are summed from the same rows, so a
        # column added to one and not the other is the exact shape of finding f8c.
        chat = next((s for s in person["surfaces"] if s["surface"] == "chat"), None)
        assert chat is not None, person
        assert chat["cached_requests"] == 1, chat
        assert overview["totals"]["cached_requests"] >= 1, overview["totals"]

        # e69's column, on the endpoint d58's veracity gate faulted for being verified by
        # READING portal.py rather than by reaching it. This person's two requests both
        # succeeded, so the operator console must say zero failed — at the person level,
        # the surface level and in the totals, all three of which e69 had to touch
        # separately. A KeyError here means the console was left behind again.
        assert person["failed_requests"] == 0, (
            f"two successful requests, one of them from cache, reported to the operator "
            f"as having failed: {person}"
        )
        assert chat["failed_requests"] == 0, chat
        assert "failed_requests" in overview["totals"], overview["totals"]

        # And it must agree with the operator's other rendering of the same money.
        bill = httpx.get(
            f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
        ).json()
        row = next(
            (r for r in bill["by_user_and_surface"] if r["username"] == username), None
        )
        assert row is not None, "on the console but not on the bill"
        assert person["cached_requests"] == row["cached_requests"], (
            f"the console and the bill disagree about how many requests were free: "
            f"console {person}, bill {row}"
        )

    def test_the_portal_page_has_a_column_for_the_free_requests(self):
        """The number has to reach the operator's eyes, not just their JSON.

        cached_requests was added to three JSON endpoints and rendered by none of them, so
        "the bill reports how many requests were free" was true only of `make spend` and
        of anyone reading the API by hand. The person the ruling was written for — an
        operator looking at a $0 line and wondering where the money went — still saw a
        table with no explanation in it.

        WHAT THIS TEST DOES NOT DO, stated plainly rather than left to be discovered. It
        does not execute the page. `make test` has no DOM: the only browser harness in
        this repo is tests-live/test_browser.py, which drives a real Chromium against the
        CLUSTER, needs kubectl to read its credentials, and needs browser binaries the
        test venv does not install. It also cannot be pointed at the bundle, because the
        portal takes its identity from an authenticating proxy the bundle does not run —
        there is no browser path to this page here at all. So what is checked is the two
        things that are checkable without a DOM, against the files the control-plane
        actually SERVES rather than the ones in the working tree:

          1. the script writes cached_requests into a cell, and
          2. that cell has a heading — the header and the row still declare the same
             number of columns.

        (2) is the load-bearing half. Adding a cell to the row template and forgetting the
        <th> silently shifts every column after it, so the operator reads the free count
        under "Tokens" and the spend under nothing. That is a rendering bug this test
        catches; whether the browser paints it is not.

        THE PER-SURFACE SUBTOTALS ARE NOT COVERED HERE, and cannot be: there is no
        per-surface table in the operator console to parse. `/portal/api/admin/overview`
        carries requests / cached / failed / refused per surface inside each person, and the
        admin table renders surfaces as a comma-joined list of names only. That is the same
        shape as d58's defect — a number carried in JSON and rendered nowhere — and it is
        stated rather than left to be discovered. The per-surface numbers ARE asserted at the
        API level by
        TestARefusalIsNotAFailedRequestAndIsNotUsage::test_the_portal_and_the_bill_agree_about_the_refusals,
        so they cannot drift from the person-level ones; what is not proven is that any
        operator ever sees them.
        """
        admin = "anyone"  # /portal/ and its assets need a user, not an operator.
        status, page = portal_get("/portal/", admin)
        assert status == 200, page[:300]
        status, script = portal_get("/portal/static/app.js", admin)
        assert status == 200, script[:300]

        for table_id, row_marker, prefix in (
            ("spend-table", "surface-tag", "r."),
            ("admin-table", "class='surfaces'", "p."),
        ):
            headers = _table_headers(page, table_id)
            cells = _row_template_cells(script, row_marker)
            # Every way a request can cost nothing needs a heading and a cell. "Free" is
            # d58's cache-hit count; "Failed" is e69's upstream-failure count; "Refused" is
            # the count of requests the gateway declined itself, which is NOT inside
            # Requests. They are checked in one loop because the failure mode is identical
            # for all three and was real for the first: a number added to the JSON and
            # never rendered.
            for heading, field in (("free", "cached_requests"),
                                   ("failed", "failed_requests"),
                                   ("refused", "refused_requests")):
                assert any(heading == h.lower() for h in headers), (
                    f"the {table_id} header has no '{heading}' column, so the "
                    f"{field} count never reaches the operator's eyes: {headers}"
                )
                assert prefix + field in " ".join(cells), (
                    f"the {table_id} row never writes {prefix + field}, so the column "
                    f"the header promises is empty: {cells}"
                )
            assert len(headers) == len(cells), (
                f"{table_id} declares {len(headers)} columns but its rows emit "
                f"{len(cells)} cells, so every column after the mismatch renders under "
                f"the wrong heading.\nheaders: {headers}\ncells: {cells}"
            )


class TestFailedRequestsAreNamedNotErased:
    """enterpriseaiframework-e69 / finding 41 — the second kind of $0 row.

    THE RULING THIS CLASS LOCKS IN, and the founder may reverse it. `requests` counts
    every request the gateway ADMITTED, and each way of costing nothing gets a named
    subtotal beside it: `cached_requests` for served-from-cache, `failed_requests` for
    the-provider-returned-nothing. `requests` does NOT become a count of successes.

    The rejected alternative was to subtract failures out of `requests`. The full
    reasoning is in metering._FAILED; the part that needs a test is that the alternative
    is LOSSY — once failures are subtracted at the source, no consumer can recover the
    count, and a provider erroring on half its traffic shows up as a quiet dip in usage
    rather than as an error rate. So `test_the_request_count_is_not_quietly_reduced`
    asserts the losing option was not taken, deliberately. If a later change implements
    it, that test fails and sends whoever did it back to finding 41 to reverse the ruling
    on purpose rather than by accident.

    WHY THE GUARD TESTS LOOK PARANOID. d58 shipped `cached_requests` and the adversary's
    first move was to loosen its predicate to `spend = 0`, which reported three failed
    requests to the operator as FREE ones. `failed_requests` is one column over and has
    the same hazard in reverse. Both predicates are therefore pinned from both sides: a
    cache hit must not be counted as a failure, and a failure must not be counted as free.
    """

    # Reused rather than re-declared. Copies of this harness are how the two renderings of
    # the attribution join drifted apart twice (see metering.ledger_attribution_sql), and
    # the fake provider's call counter in particular is the only ground truth in the file
    # for "was this cached" — a second, subtly different copy of it is worth nothing.
    _new_key = TestCacheHitsBudgetAndTheBill.__dict__["_new_key"]
    _ask = TestCacheHitsBudgetAndTheBill.__dict__["_ask"]
    _provider_calls = TestCacheHitsBudgetAndTheBill.__dict__["_provider_calls"]
    _provider_boot = TestCacheHitsBudgetAndTheBill.__dict__["_provider_boot"]
    _bill_row = TestCacheHitsBudgetAndTheBill.__dict__["_bill_row"]
    INPUT_COST = TestCacheHitsBudgetAndTheBill.INPUT_COST
    OUTPUT_COST = TestCacheHitsBudgetAndTheBill.OUTPUT_COST

    def test_the_request_count_is_not_quietly_reduced(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url
    ):
        """The rejected option, asserted against so it cannot be taken by accident.

        One served call and three upstream failures. `requests` must read 4, not 1. This
        is the assertion that distinguishes the ruling from its alternative, and it is the
        only test in the suite that would notice if someone "cleaned up" the bill by
        filtering failures out of the count.
        """
        username = f"notreduced-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")

        good = self._ask(gateway_url, key, f"served {uuid.uuid4().hex}")
        assert good.status_code == 200, good.text

        for _ in range(3):
            prompt = f"{FAKE_FAIL_MARKER} {uuid.uuid4().hex}"
            broke = self._ask(gateway_url, key, prompt)
            assert broke.status_code >= 500, f"{broke.status_code} {broke.text[:200]}"
            # Without this the request could have been refused locally, which is a
            # different class entirely and writes no row at all.
            assert self._provider_calls(fakeprovider_url, prompt) >= 1, (
                "the gateway never called the provider, so this is not an upstream failure"
            )

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the served call is missing from the bill"
        assert row["requests"] == 4, (
            f"e69 ruled that `requests` counts admitted requests, failures included, and "
            f"names them in failed_requests. This reads {row['requests']}. If that was "
            f"deliberate, finding 41's ruling has been reversed — say so there and in "
            f"metering._FAILED, and fix this test on purpose: {row}"
        )
        assert row["failed_requests"] == 3, row
        # The net count the losing option would have shown is still available, which is
        # the argument that decided it: this subtraction is possible, the reverse is not.
        assert row["requests"] - row["failed_requests"] == 1, row

    def test_a_failure_is_not_reported_as_a_free_request(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url
    ):
        """The two $0 columns must not leak into each other. Failure side.

        A key whose only traffic is failures: cached_requests must be 0. If either
        predicate were ever written against the spend column instead of its own, these
        rows would be reported to the operator as requests that were FREE — money not
        spent because nothing was delivered, dressed up as a cache efficiency.
        """
        username = f"failnotfree-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")

        for _ in range(3):
            prompt = f"{FAKE_FAIL_MARKER} {uuid.uuid4().hex}"
            broke = self._ask(gateway_url, key, prompt)
            assert broke.status_code >= 500, f"{broke.status_code} {broke.text[:200]}"
            assert self._provider_calls(fakeprovider_url, prompt) >= 1

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "three failures wrote no ledger row at all"
        assert row["requests"] == 3, row
        assert row["failed_requests"] == 3, row
        assert row["cached_requests"] == 0, (
            f"a failed request was reported to the operator as a FREE request: {row}"
        )
        # Nothing was delivered, so nothing was bought and nothing was counted. This is
        # what keeps the failure rows out of the reconciliation against a provider
        # invoice (finding 9) — they contribute 0 to spend under this ruling and under
        # the rejected one alike.
        assert row["spend"] == 0, (
            f"a request the provider refused to answer was charged for: {row}"
        )
        assert row["prompt_tokens"] == 0 and row["completion_tokens"] == 0, row

    def test_a_cache_hit_is_not_reported_as_a_failure(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url
    ):
        """The same guard from the other side — the case this change did NOT touch.

        A key whose only traffic is a miss and a hit. failed_requests must be 0. The
        `status` column reads 'success' on both rows, and this fails if the new predicate
        were ever widened to something like `spend = 0`, which is exactly the mistake that
        was made against cached_requests one column over.
        """
        username = f"freenotfail-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")
        prompt = f"cached not failed {uuid.uuid4().hex}"

        boot = self._provider_boot(fakeprovider_url)
        first = self._ask(gateway_url, key, prompt)
        assert first.status_code == 200, first.text
        usage = first.json()["usage"]
        second = self._ask(gateway_url, key, prompt)
        assert second.status_code == 200, second.text
        assert self._provider_calls(fakeprovider_url, prompt) == 1, (
            "the second call reached the provider, so there is no cache hit here and this "
            "test is not measuring what it claims"
        )
        assert self._provider_boot(fakeprovider_url) == boot, (
            "the fake provider restarted mid-test; its counter was zeroed, so 'one call' "
            "is not evidence that the second request was cached"
        )

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the cache traffic never reached the bill"
        assert row["requests"] == 2, row
        assert row["cached_requests"] == 1, row
        assert row["failed_requests"] == 0, (
            f"a request that was served — one of them from cache — was reported to the "
            f"operator as having FAILED: {row}"
        )
        # d58's money assertion, restated here rather than assumed: adding a column to
        # this query must not disturb the cost of the case that already worked.
        expected = (usage["prompt_tokens"] * self.INPUT_COST
                    + usage["completion_tokens"] * self.OUTPUT_COST)
        assert row["spend"] == pytest.approx(expected, rel=1e-6), (
            f"expected exactly one upstream call's cost ({expected}), got {row['spend']}"
        )

    def test_a_refusal_the_gateway_issued_is_still_no_request_at_all(
        self, gateway_url, control_plane_url, master_headers, admin_headers
    ):
        """The line the ruling draws, asserted on the side it did not move.

        A request the gateway refuses at the door — here, a model the key was never
        entitled to — is not admitted, and appears in neither `requests` nor
        `failed_requests`. That asymmetry with an upstream failure is deliberate and is the
        reason `requests` can be described in one sentence: it counts what the gateway
        admitted. A refusal was never admitted; a failure was admitted and then came back
        empty.

        IT DOES WRITE A ROW, contrary to what this docstring used to say. The row carries
        no key alias, so it lands under `(unattributed)` and not on this user's line — which
        is why an earlier version of this test passed while believing no row existed. This
        test is still correct about THIS user's line and is left asserting exactly that;
        the whole-ledger claim it cannot make is in
        TestARefusalIsNotAFailedRequestAndIsNotUsage.
        """
        username = f"refusednotfailed-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")

        assert self._ask(gateway_url, key, f"allowed {uuid.uuid4().hex}").status_code == 200

        for _ in range(3):
            denied = httpx.post(
                f"{gateway_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                # The key was issued for fake-large only.
                json={"model": "fake-small",
                      "messages": [{"role": "user", "content": f"nope {uuid.uuid4().hex}"}]},
                timeout=TIMEOUT,
            )
            assert denied.status_code >= 400, denied.text

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the one allowed call is missing from the bill"
        assert row["requests"] == 1, (
            f"three requests were refused before the router and must not be on the bill "
            f"at all; the bill counts {row['requests']}: {row}"
        )
        assert row["failed_requests"] == 0, (
            f"a refusal the gateway issued itself was reported as an upstream failure. "
            f"The provider was never called: {row}"
        )
        assert row["cached_requests"] == 0, row

    def test_the_totals_and_the_portal_agree_about_the_failures(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url
    ):
        """Every rendering of the ledger carries the number, and they agree.

        Finding f8c is that `make spend` and the portal, built from one query, still
        managed to disagree. So a column added to one is checked against the others here
        rather than assumed — which is also how d58's `cached_requests` turned out to be
        present in three JSON endpoints and rendered by none.
        """
        username = f"failtotals-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")

        assert self._ask(gateway_url, key, f"ok {uuid.uuid4().hex}").status_code == 200
        prompt = f"{FAKE_FAIL_MARKER} {uuid.uuid4().hex}"
        assert self._ask(gateway_url, key, prompt).status_code >= 500
        assert self._provider_calls(fakeprovider_url, prompt) >= 1

        time.sleep(25)
        bill = httpx.get(
            f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
        ).json()
        row = next(
            (r for r in bill["by_user_and_surface"] if r["username"] == username), None
        )
        assert row is not None, "the traffic never reached the bill"
        assert row["failed_requests"] == 1, row

        assert "failed_requests" in bill["totals"], (
            f"the per-user lines name the failures but the total does not, so the two "
            f"halves of the same page disagree: {bill['totals']}"
        )
        assert bill["totals"]["failed_requests"] >= 1, bill["totals"]

        # The user's own view of their own usage.
        status, body = portal_get("/portal/api/spend", username)
        assert status == 200, body[:300]
        portal = json.loads(body)
        assert portal["total"]["failed_requests"] == row["failed_requests"], (
            f"the bill says {row['failed_requests']} failed and the portal says "
            f"{portal['total']['failed_requests']}"
        )
        assert portal["total"]["requests"] == row["requests"], (
            "the two renderings disagree about the request count itself"
        )

    def test_the_exit_archive_says_which_rows_the_provider_failed(
        self, gateway_url, control_plane_url, master_headers, admin_headers,
        fakeprovider_url, tmp_path,
    ):
        """The third rendering of the ledger — the CSV a departing customer keeps.

        Per-request, so there is no `requests` count to explain. The defect is the same
        one anyway: without `status` a $0 row that the provider failed is
        indistinguishable from a served row that was never priced, and `cache_hit` cannot
        separate them because a failure carries cache_hit='False' exactly like an unpriced
        success. This archive outlives the deployment; it is the one rendering nobody can
        come back and correct.

        IT MAKES ITS OWN FAILURE ROW rather than borrowing one from the tests above it.
        An earlier draft asserted `failed` was non-empty while relying on its siblings to
        have populated the ledger, which passes in a whole-suite run and fails under `-k`
        for reasons that have nothing to do with the behaviour. Both halves of the
        assertion are unconditional here — the column must exist, AND a row marked
        'failure' must be in the archive — because a version that skipped the second half
        when the file happened to hold no failures would pass on the exact defect it
        exists to catch.
        """
        prompt = f"{FAKE_FAIL_MARKER} archive {uuid.uuid4().hex}"
        broke = self._ask(gateway_url, self._new_key(
            gateway_url, master_headers, f"archivefail-{uuid.uuid4().hex[:8]}::chat"
        ), prompt)
        assert broke.status_code >= 500, f"{broke.status_code} {broke.text[:200]}"
        assert self._provider_calls(fakeprovider_url, prompt) >= 1, (
            "the gateway never called the provider, so no failure row will be written"
        )

        # Poll rather than sleep a flat interval: in a whole-suite run the siblings above
        # have already flushed failures and this returns at once, while in isolation it
        # waits for the one this test just made. Asserting on the total (not on a delta)
        # is what makes both cases the same assertion.
        deadline = time.monotonic() + 90
        failed_total = 0
        while time.monotonic() < deadline:
            totals = httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()["totals"]
            failed_total = totals.get("failed_requests") or 0
            if failed_total >= 1:
                break
            time.sleep(3)
        assert failed_total >= 1, (
            "the failure never reached the ledger, so the export below could not carry it "
            "and this test would be asserting nothing"
        )

        # `exit.sh export` is the non-destructive mode — it does not revoke, so this can
        # run here rather than inside TestItem9ExitPath, which genuinely breaks the bundle
        # and has to run last.
        out = tmp_path / "export"
        out.mkdir()
        result = subprocess.run(
            [str(BUNDLE / "bin" / "exit.sh"), "export"],
            capture_output=True, text=True, cwd=str(BUNDLE),
            env={**os.environ, "EXPORT_DIR": str(out)},
        )
        assert result.returncode == 0, f"export failed:\n{result.stdout}\n{result.stderr}"
        latest = (out / "latest").resolve()
        with (latest / "spend.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))

        assert rows, "the export carried no spend rows, so nothing here is being checked"
        assert "status" in rows[0], (
            f"the archive hands the customer $0 rows with no way to tell a failure from "
            f"an unpriced success: {sorted(rows[0])}"
        )

        failed = [r_ for r_ in rows if (r_["status"] or "").lower() == "failure"]
        assert failed, (
            "no row in the export is marked 'failure'. Either the failure rows are "
            "missing from the archive or the column is not being populated — both are "
            "defects, and a passing test here would hide either."
        )
        for f in failed:
            assert float(f["spend"]) == 0, f
            # The point of the column: these rows were NOT cache hits, so cache_hit alone
            # could never have explained their zero.
            assert (f["cache_hit"] or "").lower() != "true", f

        # `status` alone is not enough, which is the correction this class carries. It says
        # "did not succeed", and that covers a provider fault AND the gateway declining to
        # serve — opposite meanings for a customer reconciling against an invoice.
        assert "outcome" in rows[0], (
            f"the archive marks a row 'failure' without saying whether a provider was ever "
            f"called, so a fault the provider may have charged for is indistinguishable "
            f"from the gateway refusing service: {sorted(rows[0])}"
        )
        provider_failed = [r_ for r_ in failed if r_["outcome"] == "provider_failed"]
        assert provider_failed, (
            f"the failure this test made was a real upstream 500, and no row in the "
            f"archive is labelled provider_failed: "
            f"{sorted({r_['outcome'] for r_ in failed})}"
        )
        for f in provider_failed:
            assert f["status"].lower() == "failure", (
                f"outcome and status contradict each other, so one of them is derived "
                f"wrong: {f}"
            )
        # And the vocabulary is closed — an unrecognised value means a class nobody
        # classified, which is how a refusal would end up read as a fault.
        assert {r_["outcome"] for r_ in rows} <= {
            "served", "provider_failed", "gateway_refused"
        }, sorted({r_["outcome"] for r_ in rows})
        for r_ in rows:
            if r_["outcome"] == "served":
                assert r_["status"].lower() != "failure", (
                    f"a row the provider failed is labelled 'served' in the archive: {r_}"
                )


class TestARefusalIsNotAFailedRequestAndIsNotUsage:
    """enterpriseaiframework-e69, second pass — the correction the first pass needed.

    WHAT WAS WRONG WITH THE FIRST PASS, and it was the shape of the claim rather than the
    code. `status = 'failure'` was treated as synonymous with "the provider was called and
    did not answer", because the only failure any test could reach was an injected upstream
    500 (fakeprovider FAIL_MARKER). Re-measured on the bundle, one fresh key per class,
    with the RAW ledger row dumped rather than the bill read: FOUR more things write
    status='failure', and none of them is a provider fault.

        HTTPException 403     require_principal refused it     finding 36, no alias on row
        HTTPException 429     the GATEWAY's own rate limiter   alias present
        BudgetExceededError   over the key's budget            no alias on row
        ProxyException        model not on the key's list      no alias on row
        ProxyModelNotFoundError  no such model in the catalogue  alias present

    And the premise that made the first pass look safe was false. It was recorded as
    measured that over-budget and model-not-permitted refusals "never reach the ledger at
    all". They do. The earlier measurement looked at the refused user's line on the bill,
    which was right, and never looked at the whole ledger — those rows carry no key alias,
    so they land under `(unattributed)`, which is the bucket finding 36 exists to EMPTY.
    Counting refusals refills it with a phantom principal that made requests and failed
    every one of them.

    Two operator-visible consequences, both measured on this bundle before the fix:
      - a key with rpm_limit=1 sending four requests billed as "4 requests, 3 failed". The
        gateway refused three; the provider failed none.
      - `(unattributed)` reading "5 requests, 5 failed, $0.00" where all five were the
        layer correctly refusing to serve a credential it could not name.

    THE RULING, UNCHANGED IN SUBSTANCE, SHARPER IN PREDICATE. `requests` still counts what
    the gateway ADMITTED and still keeps provider failures inside it, named by
    failed_requests — all three reasons in metering._FAILED survive. What changes is that
    "admitted" is now computed rather than assumed: a refusal is not admitted, so it is
    reported BESIDE requests as refused_requests, never inside it. Charging a request count
    to somebody the layer just denied is the same defect as billing them for it.

    THE FOUNDER MAY STILL PICK THE OTHER OPTION for the failure half — exclude
    status='failure' from `requests` entirely. Nothing here forecloses it, and this class
    would need only its expected numbers changed. The refusal half is not the same question
    and is not offered as a choice: a refusal was never served, never priced, and never
    reached a provider, so there is no reading of "requests" under which it is one.
    """

    _new_key = TestCacheHitsBudgetAndTheBill.__dict__["_new_key"]
    _ask = TestCacheHitsBudgetAndTheBill.__dict__["_ask"]
    _provider_calls = TestCacheHitsBudgetAndTheBill.__dict__["_provider_calls"]
    _provider_boot = TestCacheHitsBudgetAndTheBill.__dict__["_provider_boot"]
    _bill_row = TestCacheHitsBudgetAndTheBill.__dict__["_bill_row"]
    INPUT_COST = TestCacheHitsBudgetAndTheBill.INPUT_COST
    OUTPUT_COST = TestCacheHitsBudgetAndTheBill.OUTPUT_COST

    @staticmethod
    def _rate_limited_key(gateway_url, master_headers, alias, rpm):
        """A key the GATEWAY will refuse past `rpm` requests a minute.

        LiteLLM enforces this in its own pre-call hook, past authentication and past the
        point where a key alias is stamped on the row — which is what makes this the one
        refusal class that lands on a REAL person's line instead of in `(unattributed)`.
        That is why it is the primary test here: the defect is visible on the bill of
        somebody who exists.
        """
        r = httpx.post(
            f"{gateway_url}/key/generate",
            headers=master_headers,
            json={"key_alias": alias, "models": ["fake-large"], "rpm_limit": rpm},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        return r.json()["key"]

    def test_a_rate_limit_the_gateway_imposed_is_not_a_provider_failure(
        self, gateway_url, control_plane_url, master_headers, admin_headers
    ):
        """The measured defect, on a named person's own line.

        Counts are derived from the responses actually received rather than hardcoded. The
        rate-limit window is per minute, so a run straddling a minute boundary legitimately
        gets a different split — hardcoding it would buy an intermittent failure, and
        deriving it keeps the assertion exact.
        """
        username = f"gwratelimit-{uuid.uuid4().hex[:8]}"
        key = self._rate_limited_key(gateway_url, master_headers, f"{username}::chat", 1)

        served = refused = 0
        for _ in range(6):
            r = self._ask(gateway_url, key, f"ratelimited {uuid.uuid4().hex}")
            if r.status_code == 429:
                assert "rate limit" in r.text.lower(), f"refused for another reason: {r.text}"
                refused += 1
            else:
                assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
                served += 1
        assert refused >= 3, f"only {refused} refusals; nothing to measure"
        assert served >= 1, "nothing was served, so there is no baseline"

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the served call is missing from the bill"
        assert row["requests"] == served, (
            f"{served} requests were served and {refused} were refused by the gateway's "
            f"own rate limiter, which never called a provider. The bill counts "
            f"{row['requests']} requests — a refusal is being billed as usage: {row}"
        )
        assert row["failed_requests"] == 0, (
            f"the gateway refusing to serve was reported to the operator as the PROVIDER "
            f"failing. Nothing was called; there is no fault here to see: {row}"
        )
        assert row["refused_requests"] == refused, (
            f"{refused} refusals, {row['refused_requests']} reported. A client stuck in "
            f"refusal has to be visible somewhere or it leaves no trace at all: {row}"
        )
        assert row["cached_requests"] == 0, row
        assert row["spend"] > 0, (
            f"the served request lost its price when the refusals were separated out: {row}"
        )

    def test_a_credential_the_ledger_cannot_name_makes_no_phantom_request(
        self, gateway_url, control_plane_url, master_headers, admin_headers
    ):
        """finding 36's refusal must not reappear as usage under `(unattributed)`.

        The master key is refused by deploy/gateway/require_principal.py before any
        provider is called, and the row it writes carries no alias — so it lands in exactly
        the bucket that finding 36 exists to empty. Asserted as a DELTA across the refusal,
        because the bundle's own bootstrap and other tests populate that bucket too and an
        absolute count here would be measuring the rest of the suite.
        """
        def unattributed():
            bill = httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()
            rows = [r for r in bill["by_user_and_surface"]
                    if r["username"] == "(unattributed)"]
            return {
                "requests": sum(r["requests"] for r in rows),
                "failed_requests": sum(r["failed_requests"] for r in rows),
                "refused_requests": sum(r["refused_requests"] for r in rows),
                "spend": sum(r["spend"] for r in rows),
            }

        time.sleep(25)
        before = unattributed()

        for _ in range(3):
            # The master key itself, via the fixture that carries it. This is finding 36's
            # exact scenario: the gateway's administrative root credential asking for
            # inference, refused because no bill could ever name it.
            r = httpx.post(
                f"{gateway_url}/v1/chat/completions",
                headers=master_headers,
                json={"model": "fake-large",
                      "messages": [{"role": "user", "content": f"root {uuid.uuid4().hex}"}]},
                timeout=TIMEOUT,
            )
            assert r.status_code == 403, f"{r.status_code} {r.text[:200]}"
            assert "no_attributable_principal" in r.text, r.text[:300]

        after = self._wait_for_refusals(unattributed, before, 3)

        assert after["requests"] == before["requests"], (
            f"the gateway refused three requests it could not attribute, and the bill "
            f"gained {after['requests'] - before['requests']} requests under "
            f"'(unattributed)'. That is the bucket finding 36 exists to empty, now being "
            f"refilled by the refusals that emptied it: {before} -> {after}"
        )
        assert after["failed_requests"] == before["failed_requests"], (
            f"a credential the layer correctly refused was reported as the provider "
            f"failing: {before} -> {after}"
        )
        assert after["refused_requests"] - before["refused_requests"] >= 3, (
            f"the refusals are counted nowhere at all, so a script wired to the master "
            f"key would be invisible on every operator surface: {before} -> {after}"
        )
        assert after["spend"] == pytest.approx(before["spend"], abs=1e-12), (
            f"a refusal moved money: {before} -> {after}"
        )

    @staticmethod
    def _wait_for_refusals(read, before, n, timeout=90):
        """Poll until at least n more refusals have flushed, then return the reading.

        The gateway batches spend rows on a 7-13s timer, so a flat sleep is either slow or
        flaky. Returning the LAST reading either way is deliberate: if the refusals never
        arrive, the caller's own assertion about refused_requests is what fails, and it
        says something useful.
        """
        deadline = time.monotonic() + timeout
        seen = read()
        while time.monotonic() < deadline:
            seen = read()
            if seen["refused_requests"] - before["refused_requests"] >= n:
                break
            time.sleep(3)
        return seen

    def test_a_provider_failure_is_still_counted_and_still_named(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url
    ):
        """The path this change must NOT move — asserted, not assumed.

        Separating refusals out of `requests` is one predicate away from separating
        genuine provider failures out too, which is the option e69 rejected. This is the
        same claim TestFailedRequestsAreNamedNotErased makes, restated against the new
        predicate: an upstream 500 is still ADMITTED, still inside `requests`, still named
        by failed_requests, and is NOT a refusal.
        """
        username = f"stillfailed-{uuid.uuid4().hex[:8]}"
        key = self._new_key(gateway_url, master_headers, f"{username}::chat")

        assert self._ask(gateway_url, key, f"ok {uuid.uuid4().hex}").status_code == 200
        for _ in range(3):
            prompt = f"{FAKE_FAIL_MARKER} {uuid.uuid4().hex}"
            broke = self._ask(gateway_url, key, prompt)
            assert broke.status_code >= 500, f"{broke.status_code} {broke.text[:200]}"
            assert self._provider_calls(fakeprovider_url, prompt) >= 1, (
                "the gateway never called the provider, so this is a refusal and the test "
                "is measuring the wrong class"
            )

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the served call is missing from the bill"
        assert row["requests"] == 4, (
            f"an upstream failure was reclassified as a refusal and dropped out of the "
            f"request count. The provider WAS called and may have charged for the "
            f"attempt; this count is the only trace: {row}"
        )
        assert row["failed_requests"] == 3, row
        assert row["refused_requests"] == 0, (
            f"the gateway did not refuse anything here — it called the provider four "
            f"times and got three errors: {row}"
        )
        assert row["requests"] - row["failed_requests"] == 1, row

    def test_the_bill_tells_the_two_kinds_of_failure_apart_on_one_line(
        self, gateway_url, control_plane_url, master_headers, admin_headers, fakeprovider_url
    ):
        """Both classes on ONE key, because a per-class test cannot catch a merge.

        A predicate that classified everything as a refusal would pass
        test_a_rate_limit... and a predicate that classified everything as a fault would
        pass test_a_provider_failure..., each in isolation. This is the test that fails if
        they are ever collapsed into each other.

        Counts derived from the observed responses, for the minute-window reason above.
        """
        username = f"bothkinds-{uuid.uuid4().hex[:8]}"
        key = self._rate_limited_key(gateway_url, master_headers, f"{username}::chat", 2)

        served = provider_failed = refused = 0
        # The first two attempts are inside the limit: one served, one an upstream 500.
        # Everything after is refused before the provider is reached.
        plan = [("ok", 0), ("fail", 0)] + [("ok", 0)] * 6
        for kind, _ in plan:
            prompt = (f"{FAKE_FAIL_MARKER} {uuid.uuid4().hex}" if kind == "fail"
                      else f"both {uuid.uuid4().hex}")
            r = self._ask(gateway_url, key, prompt)
            if r.status_code == 429:
                refused += 1
            elif r.status_code >= 500:
                assert self._provider_calls(fakeprovider_url, prompt) >= 1, (
                    "a 5xx that never reached the provider is not an upstream failure"
                )
                provider_failed += 1
            else:
                assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
                served += 1
        assert provider_failed >= 1, "the upstream failure was refused before it happened"
        assert refused >= 3, f"only {refused} refusals; nothing to distinguish"
        assert served >= 1, "nothing was served"

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the traffic never reached the bill"
        assert row["requests"] == served + provider_failed, (
            f"`requests` must count what the gateway admitted: {served} served + "
            f"{provider_failed} the provider failed. It reads {row['requests']}, so the "
            f"{refused} refusals are either in it or the failures have fallen out: {row}"
        )
        assert row["failed_requests"] == provider_failed, (
            f"the two kinds of failure have been merged: {provider_failed} provider "
            f"faults and {refused} gateway refusals, reported as "
            f"{row['failed_requests']} failed and {row['refused_requests']} refused: {row}"
        )
        assert row["refused_requests"] == refused, row
        # The arithmetic the ruling promises, on a row that holds all three classes.
        assert row["requests"] - row["failed_requests"] == served, row

    def test_an_over_budget_refusal_is_a_request_nowhere_on_the_whole_ledger(
        self, gateway_url, control_plane_url, master_headers, admin_headers
    ):
        """The assertion whose absence let the false premise stand for two dispatches.

        The existing budget test checks the REFUSED USER's line and is right about it —
        those rows carry no key alias, so they never appear there. What it never checked is
        the rest of the ledger, and that is exactly where they went. This asserts the
        whole-ledger totals, which is the only place the claim "a refusal is not a request"
        can actually be falsified.
        """
        username = f"budgetnowhere-{uuid.uuid4().hex[:8]}"
        key = self._new_key(
            gateway_url, master_headers, f"{username}::chat", max_budget=0.00025
        )

        def totals():
            return httpx.get(
                f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
            ).json()["totals"]

        time.sleep(25)
        before = totals()

        served = refused = 0
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline and refused < 4:
            r = self._ask(gateway_url, key, f"budgetnowhere {uuid.uuid4().hex}")
            if r.status_code >= 400:
                assert "budget" in r.text.lower(), f"refused for another reason: {r.text}"
                refused += 1
            else:
                served += 1
            time.sleep(2)
        assert refused == 4, f"only {refused} refusals; nothing to measure"
        assert served >= 1, "nothing was served, so there is no baseline"

        after = self._wait_for_refusals(totals, before, refused)
        gained = after["requests"] - before["requests"]
        assert gained == served, (
            f"{served} requests were served past the cap and {refused} were refused at "
            f"it. The whole-ledger request count went up by {gained}. A refusal is not a "
            f"request anywhere: {before} -> {after}"
        )
        assert after["failed_requests"] == before["failed_requests"], (
            f"a budget refusal was reported as the provider failing: {before} -> {after}"
        )
        assert after["refused_requests"] - before["refused_requests"] >= refused, (
            f"{refused} budget refusals are counted nowhere, so an operator cannot see "
            f"that somebody is stuck at their cap: {before} -> {after}"
        )

    def test_the_portal_and_the_bill_agree_about_the_refusals(
        self, gateway_url, control_plane_url, master_headers, admin_headers
    ):
        """All three renderings, because two of them have drifted before (finding f8c).

        d58's cached_requests reached three JSON endpoints and was rendered by none;
        finding 34's attribution was fixed in the portal and left wrong in /admin/spend.
        A number added to one rendering gets checked against the others here.
        """
        username = f"refusedviews-{uuid.uuid4().hex[:8]}"
        key = self._rate_limited_key(gateway_url, master_headers, f"{username}::chat", 1)

        refused = 0
        for _ in range(5):
            r = self._ask(gateway_url, key, f"views {uuid.uuid4().hex}")
            if r.status_code == 429:
                refused += 1
        assert refused >= 3, f"only {refused} refusals"

        row = self._bill_row(control_plane_url, admin_headers, username)
        assert row is not None, "the traffic never reached the bill"
        assert row["refused_requests"] == refused, row

        bill = httpx.get(
            f"{control_plane_url}/admin/spend", headers=admin_headers, timeout=TIMEOUT
        ).json()
        assert "refused_requests" in bill["totals"], (
            f"the per-user lines name the refusals and the total does not, so the two "
            f"halves of the same page disagree: {bill['totals']}"
        )
        assert bill["totals"]["refused_requests"] >= refused, bill["totals"]

        status, body = portal_get("/portal/api/spend", username)
        assert status == 200, body[:300]
        portal = json.loads(body)
        assert portal["total"]["refused_requests"] == row["refused_requests"], (
            f"the bill says {row['refused_requests']} refused and the user's own page "
            f"says {portal['total']['refused_requests']}"
        )
        assert portal["total"]["requests"] == row["requests"], (
            "the two renderings disagree about the request count itself"
        )

        status, body = portal_get("/portal/api/admin/overview", "baron")
        assert status == 200, body[:300]
        overview = json.loads(body)
        assert "refused_requests" in overview["totals"], overview["totals"]
        person = next(
            (p for p in overview["people"] if p["username"] == username), None
        )
        assert person is not None, (
            f"the operator console has no line for a user whose only traffic was refused, "
            f"which is the case an operator most needs to see: "
            f"{[p['username'] for p in overview['people']]}"
        )
        assert person["refused_requests"] == refused, person
        assert person["requests"] == row["requests"], person
        # Per-surface, not just per-person: d58's column reached the person level and
        # stopped there in one of the three renderings.
        surfaces = {s["surface"]: s for s in person["surfaces"]}
        assert "chat" in surfaces, person
        assert surfaces["chat"]["refused_requests"] == refused, surfaces["chat"]


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
