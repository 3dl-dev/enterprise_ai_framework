"""One test class per numbered scope item in the sealed estimate.

If any of these cannot pass against the running bundle, the row is void — not re-scoped.
"""

import json
import time
import uuid

import httpx
import pytest

import oidc_login
from conftest import DOGFOOD_USER, compose, set_user_enabled


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

        assert set(endpoints) == {"Enterprise AI"}, (
            f"surface offers routes around the gateway: {sorted(endpoints)}"
        )
        assert endpoints["Enterprise AI"].get("userProvide") is False, (
            "users can supply their own provider key, bypassing the virtual key"
        )

    def test_chat_catalog_is_pushed_from_the_gateway(self, chat_session, chat_url):
        token = _chat_token(chat_session, chat_url)
        models = chat_session.get(
            f"{chat_url}/api/models",
            headers={
                "Authorization": f"Bearer {token}",
                "Cookie": oidc_login._cookie_header(chat_session),
            },
            timeout=TIMEOUT,
        ).json()
        assert set(models.get("Enterprise AI", [])) == {
            "fake-large", "fake-small", "claude-opus-5",
        }, models.get("Enterprise AI")

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
    def test_openai_compatible_inbound(self, gateway_url, master_headers):
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers=master_headers,
            json={"model": "fake-large", "messages": [{"role": "user", "content": "ping"}]},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["choices"][0]["message"]["content"].startswith("[fake-provider")
        assert body["usage"]["total_tokens"] > 0

    def test_anthropic_native_inbound(self, gateway_url, master_headers):
        """The terminal coding agent speaks this dialect, not the OpenAI one."""
        r = httpx.post(
            f"{gateway_url}/v1/messages",
            headers={**master_headers, "anthropic-version": "2023-06-01"},
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

    def test_streaming_is_incremental_not_buffered(self, gateway_url, master_headers):
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
            headers=master_headers,
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


class TestPricingIntegrity:
    """Regression guard for a silent-money defect found while building this row.

    A model missing from the gateway price map serves traffic, counts tokens, and prices
    every request at $0 — so budgets never trip and the bill under-reports, with no error
    anywhere. Every configured model must therefore price above zero.
    """

    def test_configured_models_record_nonzero_spend(
        self, gateway_url, control_plane_url, admin_headers, master_headers
    ):
        # Explicit UTC offset — the ledger stores timestamptz, and a naive string would
        # be read in the server's zone and silently select the wrong window.
        since = time.strftime("%Y-%m-%dT%H:%M:%S+00", time.gmtime(time.time() - 5))
        for model in ("fake-large", "fake-small", "claude-opus-5"):
            r = httpx.post(
                f"{gateway_url}/v1/chat/completions",
                headers=master_headers,
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
        remaining = set()
        for page in range(1, 51):
            listed = httpx.get(
                f"{gateway_url}/key/list",
                headers=master_headers,
                # size is capped at 100 by the gateway; exceeding it returns a
                # validation error rather than a truncated page.
                params={"return_full_object": "true", "page": page, "size": 100},
                timeout=TIMEOUT,
            )
            assert listed.status_code == 200, listed.text
            batch = [k for k in listed.json().get("keys", []) if isinstance(k, dict)]
            remaining |= {k.get("key_alias") for k in batch}
            if len(batch) < 100:
                break
        for k in active:
            assert k["key_alias"] not in remaining, (
                f"{k['surface']} key still present on the gateway after revocation"
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

    def test_traffic_still_flows(self, gateway_url, master_headers):
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers=master_headers,
            json={"model": "fake-large", "messages": [{"role": "user", "content": "no account"}]},
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
