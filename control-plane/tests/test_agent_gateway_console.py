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
import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_portal_agents import (  # noqa: E402 - path set up by that module's import
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
        self.requests: list[tuple[str, str, bool]] = []  # (method, path, authenticated)
        self.fail_next_authed = threading.Event()  # forces one 401 to test re-login
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
