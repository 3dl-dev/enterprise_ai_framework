"""The Raven Agents-pillar type (enterpriseaiframework-f16): console proxy + owner scoping.

The sibling of test_agent_openclaw.py, and like it the upstream is a FAKE whose rules were
RECORDED from the real thing, not invented: the hosted image `raven-hosted:v0.2.3-eaf2`
(deploy/raven/) was started on k3s with `RAVEN_SERVE_TOKEN` set and probed by the -f16 run:

  * `GET /health` -> 200 `{"ok": true, "service": "raven-serve", ...}` with or without a token;
  * `GET /file?path=...` -> 401 with no token and with a wrong `X-Raven-Token`, 200 with the
    right one; `GET /` (the SPA) -> 200 either way;
  * the WebUI's RPC is JSON-RPC 2.0 over ONE WebSocket at `/rpc` (NOT HTTP POST); an upgrade
    with no token is `HTTP 401`, with `X-Raven-Token` it is accepted;
  * the SPA is served at the root of its origin and hard-codes `fetch(`/health`)` and
    `ws(s)://${location.host}/rpc`, so the entry document needs the URL shim.

The claims under test are about the PROXY (agent_gateway_console + agent_console) against
those recorded rules:

  * the owner reaches the WebUI and the proxy presents the console token, which the browser
    never holds and cannot override;
  * a non-owner gets the same 404 as a non-existent agent and the upstream never sees them;
  * the entry document is rewritten so its root-absolute URLs stay under /agents/<name>/;
  * the /rpc socket carries the token, forwards JSON-RPC frames both ways, and every browser
    frame passes through the `raven_frame_gate` seam (item -250 attaches its deny-list there).
"""
import asyncio
import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_portal_agents import (  # noqa: E402 - path set up by that module's import
    AUDIT,
    ISSUED,
    _SA_DIR,
    FakeCluster,
    _stub_ledger,
)

from app import agent_console, agent_gateway_console, agent_usage, agents, portal  # noqa: E402

OWNER = "alice"
TOKEN = "raven-console-token-alice-rv"

# The fragments of the real SPA (3 MB, one inlined file) the shim exists for, verbatim.
_SPA = b"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>Raven</title>
<link rel="icon" type="image/png" href="assets/raven.png" />
</head>
<body><script>
var Jie=()=>`${location.protocol===`https:`?`wss`:`ws`}://${location.host}/rpc`,
Xie=async()=>{try{let e=await fetch(`/health`,{cache:`no-store`});return e.status<500}catch{return!1}};
</script></body></html>"""


class FakeRaven:
    """nginx + engine on the WebUI port, enforcing the recorded token rules."""

    def __init__(self):
        self.token = TOKEN
        self.address = "127.0.0.1"
        self.requests: list[dict] = []
        self.srv = ThreadingHTTPServer((self.address, 0), self._handler())
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _handler(rv):  # noqa: N805 - the closure is the handler's access to the fake
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, body: bytes, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                h = {k.lower(): v for k, v in self.headers.items()}
                path = urlparse(self.path).path
                rv.requests.append({"path": path, "headers": h, "method": "GET"})
                if path == "/health":
                    self._send(200, json.dumps(
                        {"ok": True, "service": "raven-serve"}).encode())
                elif path in ("/file",) or path.startswith("/files/"):
                    if h.get("x-raven-token") != rv.token:
                        self._send(401, b'{"error":"unauthorized"}')
                    else:
                        self._send(200, b"file-bytes", "text/plain")
                else:
                    # nginx `try_files $uri /index.html`: every other path is the SPA.
                    self._send(200, _SPA, "text/html")

            def log_message(self, *a):
                pass

        return Handler


@pytest.fixture()
def world(monkeypatch):
    _stub_ledger(monkeypatch)
    AUDIT.clear()
    ISSUED.clear()
    cluster = FakeCluster()
    monkeypatch.setattr(agents, "KUBE_API", cluster.url)
    monkeypatch.setattr(agent_usage, "TOKEN_FILE", _SA_DIR / "token")
    monkeypatch.setattr(agent_usage, "CA_FILE", _SA_DIR / "ca.crt")
    monkeypatch.setattr(agent_usage, "NAMESPACE_FILE", _SA_DIR / "namespace")
    monkeypatch.setenv("GATEWAY_URL", cluster.url)

    rv = FakeRaven()
    # console_target reads the port at call time; the fake listens on an ephemeral one.
    monkeypatch.setattr(agents, "RAVEN_PORT", rv.port)
    hosts: dict[str, str] = {}
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        key = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if key in hosts:
            return real_getaddrinfo(hosts[key], port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def add_agent(user: str, name: str, token: str = TOKEN):
        cluster.add_agent(user, name, agent_type="raven")
        obj = f"agent-{user}-{name}"
        cluster.put("secrets", {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": f"{obj}-key"},
            "data": {"OPENAI_API_KEY": base64.b64encode(b"sk-x").decode(),
                     "RAVEN_SERVE_TOKEN": base64.b64encode(token.encode()).decode()},
        })
        hosts[obj] = rv.address

    w = type("World", (), {})()
    w.cluster, w.rv, w.add_agent, w.hosts = cluster, rv, add_agent, hosts
    try:
        yield w
    finally:
        rv.stop()
        cluster.stop()


def _api() -> FastAPI:
    api = FastAPI()
    api.include_router(portal.router)
    api.include_router(agent_console.router)
    return api


def app_client(user: str) -> TestClient:
    client = TestClient(_api(), client=("127.0.0.1", 41000), raise_server_exceptions=False)
    client.headers.update({"X-Auth-Request-Preferred-Username": user})
    return client


# ---------------------------------------------------------------- console (Contract C)


def test_the_owner_reaches_the_webui_and_the_proxy_presents_the_console_token(world):
    world.add_agent(OWNER, "rv")
    r = app_client(OWNER).get("/agents/rv/file", params={"path": "/data/x"})
    assert r.status_code == 200 and r.content == b"file-bytes", (
        "the engine's authenticated route answered 401: the proxy did not present the token")
    seen = world.rv.requests[-1]
    assert seen["path"] == "/file", "Raven serves at its root: the /agents/<name> prefix is stripped"
    assert seen["headers"]["x-raven-token"] == TOKEN
    assert "cookie" not in seen["headers"], "no cookie crosses the hop"


def test_a_browser_cannot_supply_or_override_the_console_token(world):
    world.add_agent(OWNER, "rv")
    r = app_client(OWNER).get("/agents/rv/file", headers={
        "X-Raven-Token": "browser-guess", "x-raven-other": "1",
        "X-Forwarded-User": "mallory", "Cookie": "raven_session_18793=stolen"})
    assert r.status_code == 200
    seen = world.rv.requests[-1]["headers"]
    assert seen["x-raven-token"] == TOKEN, "the browser's token reached the engine"
    for forged in ("x-raven-other", "x-forwarded-user", "cookie"):
        assert forged not in seen, f"{forged} from the browser reached the engine"


def test_the_fake_refuses_a_request_without_the_token(world):
    """Guards the fake against being a rubber stamp: the recorded behaviour without the token
    is a 401, so the passing tests above mean the proxy really authenticated."""
    import httpx

    for headers in ({}, {"X-Raven-Token": "wrong"}):
        r = httpx.get(f"http://127.0.0.1:{world.rv.port}/file", headers=headers)
        assert r.status_code == 401


def test_a_non_owner_gets_404_and_the_engine_never_sees_them(world):
    world.add_agent(OWNER, "rv")
    for path in ("/agents/rv/", "/agents/rv/file", "/agents/rv/health"):
        r = app_client("mallory").get(path)
        assert r.status_code == 404, f"{path} as mallory answered {r.status_code}"
    assert world.rv.requests == [], "the request must be refused before any upstream call"


def test_a_missing_console_token_is_a_503_not_an_unauthenticated_attach(world):
    world.add_agent(OWNER, "rv")
    secret = world.cluster.get("secrets", "agent-alice-rv-key")
    del secret["data"]["RAVEN_SERVE_TOKEN"]
    r = app_client(OWNER).get("/agents/rv/")
    assert r.status_code == 503
    assert world.rv.requests == []


def test_the_entry_document_is_rewritten_so_its_urls_stay_under_the_prefix(world):
    world.add_agent(OWNER, "rv")
    r = app_client(OWNER).get("/agents/rv/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert 'href="assets/raven.png"' in body, "a relative URL resolves under the prefix already"
    assert 'var P="/agents/rv"' in body, "the fetch/WebSocket shim is injected"
    assert "window[n]=new Proxy" in body and "window.fetch=" in body
    assert "location.host}/rpc" in body, "the SPA's own code is not edited, only the APIs it calls"


def test_the_health_route_streams_through_unmodified(world):
    world.add_agent(OWNER, "rv")
    r = app_client(OWNER).get("/agents/rv/health")
    assert r.status_code == 200 and r.json()["service"] == "raven-serve"


# ---------------------------------------------------------------- the /rpc socket


def _serve_ws(monkeypatch_target, handler, address="127.0.0.2"):
    """A websocket server on the fake's port (second loopback IP), enforcing the token."""
    websockets = pytest.importorskip("websockets")
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def process_request(conn, request):
        if request.headers.get("X-Raven-Token") != TOKEN:
            return conn.respond(401, "unauthorized\n")
        return None

    async def serve():
        from websockets.asyncio.server import serve as aserve
        server = await aserve(handler, address, monkeypatch_target.rv.port,
                              process_request=process_request)
        ready.set()
        await server.serve_forever()

    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(serve())
        except (RuntimeError, OSError, asyncio.CancelledError):
            pass

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(10), "the fake raven's websocket server never started"
    return loop


def test_the_rpc_socket_carries_the_token_and_forwards_json_rpc_frames(world):
    seen: dict = {}

    async def handler(conn):
        seen["headers"] = {k.lower(): v for k, v in conn.request.headers.items()}
        seen["path"] = conn.request.path
        async for message in conn:
            call = json.loads(message)
            await conn.send(json.dumps({"jsonrpc": "2.0", "id": call["id"],
                                        "result": {"echo": call["method"]}}))

    loop = _serve_ws(world, handler)
    try:
        world.add_agent(OWNER, "rv")
        world.hosts["agent-alice-rv"] = "127.0.0.2"
        with app_client(OWNER).websocket_connect(
            "/agents/rv/rpc", headers={"X-Raven-Token": "browser-guess",
                                       "Origin": "https://ai.example.org"},
        ) as ws:
            ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "system.ping",
                                     "params": {}}))
            assert json.loads(ws.receive_text()) == {
                "jsonrpc": "2.0", "id": 1, "result": {"echo": "system.ping"}}
        assert seen["headers"]["x-raven-token"] == TOKEN
        assert seen["path"] == "/rpc", "the prefix is stripped: the engine serves /rpc at its root"

        with pytest.raises(Exception):
            with app_client("mallory").websocket_connect("/agents/rv/rpc"):
                pass
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_every_browser_frame_passes_the_gate_seam_and_a_refusal_never_reaches_the_agent(
        world, monkeypatch):
    """The seam item -250 attaches its provider/channel write lock to. Today's gate is
    pass-through; this proves the wiring: a gate that refuses answers the BROWSER with the
    error frame it returned, and the frame is never forwarded upstream."""
    upstream_frames: list[str] = []

    async def handler(conn):
        async for message in conn:
            upstream_frames.append(message)
            await conn.send(json.dumps({"jsonrpc": "2.0", "id": 0, "result": "upstream"}))

    loop = _serve_ws(world, handler)
    try:
        world.add_agent(OWNER, "rv")
        world.hosts["agent-alice-rv"] = "127.0.0.2"
        # today's real gate: pass-through
        assert agent_gateway_console.raven_frame_gate('{"method":"anything"}') is None

        def gate(frame):
            if json.loads(frame)["method"] == "settings.set":
                return json.dumps({"jsonrpc": "2.0", "id": 7,
                                   "error": {"code": -32000, "message": "read-only"}})
            return None

        monkeypatch.setattr(agent_gateway_console, "raven_frame_gate", gate)
        with app_client(OWNER).websocket_connect("/agents/rv/rpc") as ws:
            ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "settings.set"}))
            assert json.loads(ws.receive_text())["error"]["message"] == "read-only"
            ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 8, "method": "turn.send"}))
            assert json.loads(ws.receive_text())["result"] == "upstream"
        assert [json.loads(f)["method"] for f in upstream_frames] == ["turn.send"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
