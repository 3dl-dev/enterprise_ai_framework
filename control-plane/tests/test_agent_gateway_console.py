"""The Agents-pillar console proxy (agent_gateway_console.py), against a fake hermes dashboard.

The sibling of test_agent_console.py, and it reuses that suite's fake cluster and ledger
stubs (imported, not copied, so the two cannot drift). The difference is the upstream: a
fake DASHBOARD that authenticates with the form-login + session cookie the real hermes
dashboard uses (confirmed against the image by enterpriseaiframework-2ba/-8e4), not
opencode's HTTP Basic. The claims under test are Contract C's:

  * the owner reaches the dashboard, and the proxy authenticated for them (the browser never
    saw the dashboard's own login);
  * a non-owner gets the same 404 as a non-existent agent;
  * the SPA base path is forwarded (X-Forwarded-Prefix), so no rewrite shim is needed;
  * one login is reused across a page's many requests, and a stale cookie triggers exactly
    one transparent re-login.
"""
import asyncio
import base64
import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from test_portal_agents import (  # noqa: E402 - path set up by that module's import
    AUDIT,
    ISSUED,
    _SA_DIR,
    FakeCluster,
    _stub_ledger,
)

from app import agent_console, agent_gateway_console, agent_usage, agents, portal  # noqa: E402


class FakeDashboard:
    """A hermes dashboard: 401 without a session, a form-login that mints one, and authed
    responses that echo the path and the X-Forwarded-Prefix the proxy sent."""

    def __init__(self, username: str, password: str, address: str = "127.0.0.1"):
        self.username = username
        self.password = password
        self.address = address
        self.port = agents.DASHBOARD_PORT
        self.logins = 0
        self.model_sets: list[dict] = []   # bodies POSTed to /api/model/set
        self.restarts = 0
        self.env_puts: list[dict] = []     # bodies PUT to /api/env
        self.env: dict[str, str] = {}      # the resident process's own env override store
        self.requests: list[tuple[str, str, bool]] = []  # (method, path, authenticated)
        self.fail_next_authed = threading.Event()  # forces one 401 to test re-login
        self.fail_env = threading.Event()  # forces /api/env to refuse, for the error path
        self._token = "sess-" + uuid.uuid4().hex
        try:
            self.srv = ThreadingHTTPServer((address, self.port), self._handler())
        except OSError as exc:  # pragma: no cover - environment, not behaviour
            raise pytest.skip.Exception(
                f"cannot bind {address}:{self.port} for the fake dashboard ({exc}). The "
                "console proxy dials the agent Service's real port, so this suite needs it "
                "free on loopback."
            ) from exc
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def paths(self) -> list[str]:
        return [p for _m, p, _a in self.requests]

    def _handler(dash):  # noqa: N805 - the closure is the handler's access to the dashboard
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, ctype, body: bytes, cookie: str = ""):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(body)

            def _has_session(self) -> bool:
                # Quote-agnostic: the real dashboard sets a quoted value and the proxy
                # forwards it verbatim, so match on the unique token itself.
                return dash._token in (self.headers.get("Cookie") or "")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                path = urlparse(self.path).path
                # Contract D API the model picker (-840) drives, authenticated by cookie.
                if path in ("/api/model/set", "/api/gateway/restart"):
                    if not self._has_session():
                        self._send(401, "application/json", b"{}")
                        return
                    if path == "/api/model/set":
                        dash.model_sets.append(json.loads(raw or b"{}"))
                        self._send(200, "application/json", b'{"ok":true}')
                    else:
                        dash.restarts += 1
                        self._send(200, "application/json", b'{"ok":true}')
                    return
                if path == "/auth/password-login":
                    body = json.loads(raw or b"{}")
                    ok = (body.get("username") == dash.username
                          and body.get("password") == dash.password
                          and body.get("provider") == "basic")
                    dash.requests.append(("POST", path, ok))
                    if not ok:
                        self._send(401, "application/json", b'{"detail":"Invalid credentials"}')
                        return
                    dash.logins += 1
                    self._send(
                        200, "application/json", b'{"ok":true,"next":"/"}',
                        cookie=f'hermes_session_at="{dash._token}"; HttpOnly; Path=/; SameSite=lax',
                    )
                    return
                self._authed("POST", path)

            def do_GET(self):
                self._authed("GET", urlparse(self.path).path)

            def do_PUT(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                path = urlparse(self.path).path
                # PUT /api/env: the reprovision path (-b00) writes the new key straight into
                # the RESIDENT gateway's own environment — distinct from the pod's envFrom,
                # which never updates after start (see agents.py's Connector.annotation).
                if path == "/api/env":
                    if not self._has_session():
                        self._send(401, "application/json", b"{}")
                        return
                    body = json.loads(raw or b"{}")
                    dash.env_puts.append(body)
                    if dash.fail_env.is_set():
                        self._send(502, "application/json", b'{"ok":false}')
                        return
                    dash.env.update(body)
                    self._send(200, "application/json", b'{"ok":true}')
                    return
                self._authed("PUT", path)

            def _authed(self, method, path):
                ok = self._has_session()
                dash.requests.append((method, path, ok))
                if not ok:
                    # A public bind's unauthenticated answer is a redirect to the login page.
                    self.send_response(302)
                    self.send_header("Location", "/login?next=%2F")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if dash.fail_next_authed.is_set():
                    # Simulate an expired session server-side to exercise the re-login path.
                    dash.fail_next_authed.clear()
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps({
                    "path": path,
                    "prefix": self.headers.get("X-Forwarded-Prefix", ""),
                    "fwd_host": self.headers.get("X-Forwarded-Host", ""),
                }).encode()
                self._send(200, "application/json", payload)

            def log_message(self, *a):
                pass

        return Handler


@pytest.fixture()
def world(monkeypatch):
    _stub_ledger(monkeypatch)
    AUDIT.clear()
    ISSUED.clear()
    # The module-global session cache must not leak a cookie between tests (each test gets a
    # fresh dashboard with a fresh token), so it is cleared per test.
    agent_gateway_console._SESSIONS.clear()
    agent_gateway_console._LOCKS.clear()

    cluster = FakeCluster()
    monkeypatch.setattr(agents, "KUBE_API", cluster.url)
    monkeypatch.setattr(agent_usage, "TOKEN_FILE", _SA_DIR / "token")
    monkeypatch.setattr(agent_usage, "CA_FILE", _SA_DIR / "ca.crt")
    monkeypatch.setattr(agent_usage, "NAMESPACE_FILE", _SA_DIR / "namespace")
    monkeypatch.setenv("GATEWAY_URL", cluster.url)

    username = "console"
    password = "dash-" + uuid.uuid4().hex[:8]
    dash = FakeDashboard(username, password)

    hosts: dict[str, str] = {}
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        key = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if key in hosts:
            return real_getaddrinfo(hosts[key], port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def add_agent(user: str, name: str, *, reachable: bool = True):
        cluster.add_agent(user, name, agent_type="hermes")
        obj = f"agent-{user}-{name}"
        import base64
        cluster.put("secrets", {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": f"{obj}-key"},
            "data": {
                "OPENAI_API_KEY": base64.b64encode(b"sk-x").decode(),
                "DASHBOARD_USERNAME": base64.b64encode(username.encode()).decode(),
                "DASHBOARD_PASSWORD": base64.b64encode(password.encode()).decode(),
            },
        })
        hosts[obj] = dash.address if reachable else "127.0.0.3"

    world = type("World", (), {})()
    world.cluster = cluster
    world.dash = dash
    world.add_agent = add_agent
    world.hosts = hosts
    try:
        yield world
    finally:
        dash.stop()
        cluster.stop()


def _api() -> FastAPI:
    api = FastAPI()
    api.include_router(portal.router)
    api.include_router(agent_console.router)
    return api


def app_client(user: str, *, peer=("127.0.0.1", 41000)) -> TestClient:
    client = TestClient(_api(), client=peer, raise_server_exceptions=False)
    client.headers.update({"X-Auth-Request-Preferred-Username": user})
    return client


def test_the_owner_reaches_the_native_dashboard_authenticated_for_them(world):
    world.add_agent("alice", "athena")
    r = app_client("alice").get("/agents/athena/api/config")
    assert r.status_code == 200, r.text
    body = r.json()
    # The proxy authenticated to the dashboard (a login happened) and forwarded the request.
    assert world.dash.logins == 1, "the proxy must form-login to the dashboard, once"
    assert ("GET", "/api/config", True) in world.dash.requests, (
        "the authed request must reach the dashboard carrying the session cookie"
    )
    # The browser reached the dashboard's real response, not its login page.
    assert body["path"] == "/api/config"


def test_the_spa_base_path_is_forwarded_so_no_shim_is_needed(world):
    world.add_agent("alice", "athena")
    body = app_client("alice").get("/agents/athena/").json()
    assert body["prefix"] == "/agents/athena", (
        "X-Forwarded-Prefix must carry the mount point — it is what makes the dashboard "
        "resolve its own SPA under /agents/athena/ with no URL-rewrite shim"
    )


def test_one_login_is_reused_across_a_pages_many_requests(world):
    world.add_agent("alice", "athena")
    c = app_client("alice")
    for path in ("api/config", "api/model/options", "assets/index.js"):
        assert c.get(f"/agents/athena/{path}").status_code == 200
    assert world.dash.logins == 1, (
        "a console page makes many requests; re-logging-in on each would hammer scrypt and "
        "the login audit log — the session cookie is cached per agent"
    )


def test_a_stale_session_triggers_exactly_one_transparent_relogin(world):
    world.add_agent("alice", "athena")
    c = app_client("alice")
    assert c.get("/agents/athena/api/config").status_code == 200
    assert world.dash.logins == 1
    # The dashboard will reject the next authed request once, as an expired cookie would.
    world.dash.fail_next_authed.set()
    r = c.get("/agents/athena/api/config")
    assert r.status_code == 200, "a stale cookie must be re-minted, invisibly to the user"
    assert world.dash.logins == 2, "exactly one extra login, not a login per request"


def test_a_non_owner_gets_404_not_another_users_console(world):
    world.add_agent("alice", "athena")
    r = app_client("bob").get("/agents/athena/api/config")
    assert r.status_code == 404, (
        "a request for an agent the caller does not own is a 404 — a 403 would confirm the "
        "agent exists (Contract 1 owner-scoping, reused from console_target)"
    )
    assert world.dash.logins == 0, "a non-owner must never cause a login to someone's console"


def test_a_stopped_agent_reads_as_unreachable_not_as_a_broken_page(world):
    world.add_agent("alice", "athena", reachable=False)
    r = app_client("alice").get("/agents/athena/api/config")
    assert r.status_code in (502, 504), r.status_code
    assert "athena" in r.text


# ---- model picker (-840), driving the dashboard's Contract D API -----------------------

def test_setting_the_model_drives_the_console_api_and_restarts(world):
    world.add_agent("alice", "athena")
    r = app_client("alice").post(
        "/portal/api/agents/athena/model", json={"model": agents.DEFAULT_MODEL})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == agents.DEFAULT_MODEL and body["restarted"] is True
    # The change goes through the dashboard's OWN writer (not a config clobber, not exec),
    # as a ModelAssignment — the endpoint constructs the shape, it does not pass the user
    # body through. provider is the seeded integrated gateway, NOT "nous".
    assert len(world.dash.model_sets) == 1
    sent = world.dash.model_sets[0]
    assert sent == {
        "scope": "main", "provider": "gateway",
        "model": agents.DEFAULT_MODEL, "confirm_expensive_model": True,
    }
    # /api/model/set applies to new sessions only, so the gateway is restarted to pick it up.
    assert world.dash.restarts == 1


def test_an_unknown_model_is_refused_and_never_reaches_the_agent(world):
    world.add_agent("alice", "athena")
    r = app_client("alice").post(
        "/portal/api/agents/athena/model", json={"model": "totally-made-up-model"})
    assert r.status_code == 400, r.text
    assert world.dash.model_sets == [], (
        "an unvalidated model must never be written to the agent — it is checked against "
        "allowed_models() before the console is touched"
    )


def test_a_non_owner_cannot_change_another_users_model(world):
    world.add_agent("alice", "athena")
    r = app_client("bob").post(
        "/portal/api/agents/athena/model", json={"model": agents.DEFAULT_MODEL})
    assert r.status_code == 404, r.text
    assert world.dash.model_sets == [] and world.dash.logins == 0


# ---- key reprovision (-b00, Phase 3 of the LiteLLM -> freerouter cutover) --------------
#
# The claim under test: a running hermes agent's key changes VALUE (a real, distinct
# string minted through the stubbed `issuance.issue` -> `provisioning.backend()` seam) and
# reaches the LIVE resident process over its own console API (`PUT /api/env` then
# `POST /api/gateway/restart`, exercised against the real FakeDashboard HTTP server, not a
# mocked client) WITHOUT the pod being touched — no Secret re-apply that bumps the
# checksum annotation, no Deployment patch, no rollout. Ground truth for "did the new key
# actually reach the agent" is the FakeDashboard's own `env` store, populated only by a real
# HTTP PUT it received and parsed — not an assertion that `agent_gateway_console.call` was
# invoked with certain arguments.


def _run(coro):
    return asyncio.run(coro)


def test_reprovisioning_pushes_a_new_key_into_the_live_agent_without_a_pod_restart(world):
    world.add_agent("alice", "athena")
    obj = "agent-alice-athena"
    before = world.cluster.get("deployments", obj)
    before_dep = json.loads(json.dumps(before))  # deep copy for a real before/after diff

    result = _run(agents.reprovision_key("alice", "athena", actor="admin"))

    assert result["live_applied"] is True, result
    assert result["type"] == "hermes"
    # The FakeDashboard's own state, not a call-args assertion: the resident process was
    # actually told the new key, and actually restarted to pick it up.
    assert world.dash.env_puts == [{"OPENAI_API_KEY": "sk-fake-alice-agents/athena"}]
    assert world.dash.env["OPENAI_API_KEY"] == "sk-fake-alice-agents/athena"
    assert world.dash.restarts == 1
    # The mint went through `provisioning.backend()` — never straight at a backend module —
    # which is exactly what the stubbed `issuance.issue` records.
    assert ISSUED == [("alice", "agents/athena", "admin")]

    # The Secret holds the new key (staged for a future natural restart too)...
    secret = world.cluster.get("secrets", f"{obj}-key")
    data = secret["data"]
    assert base64.b64decode(data["OPENAI_API_KEY"]).decode() == "sk-fake-alice-agents/athena"
    # ...and every OTHER field the original Secret carried survived the merge untouched.
    assert base64.b64decode(data["DASHBOARD_USERNAME"]).decode() == world.dash.username
    assert base64.b64decode(data["DASHBOARD_PASSWORD"]).decode() == world.dash.password

    # THE POD ITSELF WAS NEVER TOUCHED. This is the control: the Deployment object — the
    # thing whose template annotation a rollout patches — is byte-identical before and
    # after, and no PATCH ever reached "deployments" at the fake cluster's HTTP layer. A
    # defect that routed the key change through `configure_connector`'s checksum-bump path
    # instead of the live console push would fail this assertion, because that path's
    # entire mechanism is a Deployment template patch.
    after_dep = world.cluster.get("deployments", obj)
    assert after_dep == before_dep, "reprovisioning a key must never touch the Deployment"
    assert ("patch", "deployments") not in world.cluster.calls

    assert ("admin", "agent.key.reprovision", "alice/athena") in AUDIT


def test_reprovisioning_is_owner_scoped(world):
    world.add_agent("alice", "athena")
    with pytest.raises(HTTPException) as exc:
        _run(agents.reprovision_key("bob", "athena", actor="bob"))
    assert exc.value.status_code == 404
    assert ISSUED == [], "no key may be minted before ownership is confirmed"
    assert world.dash.env_puts == []


def test_a_console_that_refuses_the_new_key_is_reported_not_raised(world):
    world.add_agent("alice", "athena")
    world.dash.fail_env.set()

    result = _run(agents.reprovision_key("alice", "athena", actor="admin"))

    assert result["live_applied"] is False
    assert "console refused" in result["live_error"]
    # The mint and the Secret write both still happened — a console hiccup must not lose
    # the new key, only delay when the live agent starts using it.
    assert ISSUED == [("alice", "agents/athena", "admin")]
    secret = world.cluster.get("secrets", "agent-alice-athena-key")
    assert (base64.b64decode(secret["data"]["OPENAI_API_KEY"]).decode()
            == "sk-fake-alice-agents/athena")
    assert world.dash.restarts == 0, "a refused key write must not trigger the restart"


def test_an_unreachable_agent_is_reported_not_raised(world):
    world.add_agent("alice", "athena", reachable=False)

    result = _run(agents.reprovision_key("alice", "athena", actor="admin"))

    assert result["live_applied"] is False
    assert result["live_error"], "an unreachable console must explain itself"
    # Still minted and staged, so the agent picks the key up on its next real start.
    assert ISSUED == [("alice", "agents/athena", "admin")]


def test_rolling_reprovision_covers_every_agent_and_tolerates_one_failure(world):
    world.add_agent("alice", "athena")
    world.add_agent("bob", "rudi")
    # bob's console is unreachable — the rolling pass must not stop at alice's neighbor.
    world.hosts["agent-bob-rudi"] = "127.0.0.3"

    results = _run(agents.rolling_reprovision(actor="admin"))

    by_name = {r["name"]: r for r in results}
    assert set(by_name) == {"athena", "rudi"}
    assert by_name["athena"]["live_applied"] is True
    assert by_name["rudi"]["live_applied"] is False
    assert by_name["rudi"]["live_error"]
    # BOTH keys were minted — one agent's unreachable console must not skip the mint (and
    # therefore the eventual pickup) for the OTHER agent, and must not abort the whole run.
    assert ("alice", "agents/athena", "admin") in ISSUED
    assert ("bob", "agents/rudi", "admin") in ISSUED


def test_a_non_hermes_agent_stages_the_secret_without_a_live_push(world):
    # openclaw (interim, opencode-rendered) has no console settings API yet (Contract D
    # covers hermes only) — `set_model` gates on the identical condition for the identical
    # reason.
    world.cluster.add_agent("alice", "coder", agent_type="openclaw")
    obj = "agent-alice-coder"
    world.cluster.put("secrets", {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": f"{obj}-key"},
        "data": {
            "OPENCODE_SERVER_PASSWORD": base64.b64encode(b"pw").decode(),
            "OPENAI_API_KEY": base64.b64encode(b"sk-old").decode(),
        },
    })

    result = _run(agents.reprovision_key("alice", "coder", actor="admin"))

    assert result["live_applied"] is False
    assert "hermes only" in result["live_error"]
    secret = world.cluster.get("secrets", f"{obj}-key")
    assert (base64.b64decode(secret["data"]["OPENAI_API_KEY"]).decode()
            == "sk-fake-alice-agents/coder")
    assert base64.b64decode(secret["data"]["OPENCODE_SERVER_PASSWORD"]).decode() == "pw"
