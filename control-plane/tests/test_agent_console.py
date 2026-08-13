"""Attaching an agent console: whose agent you reach, and that you did not start it.

WHY THIS FILE IS A SECURITY TEST FIRST AND A FEATURE TEST SECOND

`/agents/<name>/` proxies a browser onto a headless coding agent that holds a spendable
key, runs unattended, and has no other door — `deploy/k8s/63-agent-common.yaml` gives its
port no NodePort and admits it from the control-plane pod alone. So this route is the
entire perimeter, and the only thing standing between one camper and another camper's live
session is that the upstream host is derived from `require_user()` and then checked against
the object's owner label. Every rejection case below is somebody trying to cross that.

The second claim is Contract 2's, and it is a NEGATIVE: attaching must not start anything.
A console that spawned its own agent would pass every "the page loads" test ever written
and would still be the Code surface with a different tab (finding 43). It is measured here
as identity-and-state-across-a-disconnect, and against a real pod in
tests-live/test_agent_console.py, where the resident PID is read out of the pod.

WHAT IS REAL HERE AND WHAT IS NOT

Real, exercised as shipped:
  * `app.agent_console` in full — the proxy, the entry-document rewrite, the shim, the
    streaming path, the websocket bridge;
  * `app.agents.console_target` — name derivation, the owner-label guard, the credential
    read — and `app.portal.require_user`, reached over loopback with the header
    oauth2-proxy sets. No test here hands an endpoint an identity it did not authenticate;
  * the Kubernetes API server, as a real HTTP server holding a real object store: the
    `FakeCluster` from test_portal_agents.py, reused rather than copied.

Two stand-ins for the CLUSTER, never for the code under test:
  * `FakeDaemon` — a real HTTP/websocket server answering the way the measured
    `opencode serve` answers (401 without Basic, an entry document whose asset references
    are root-absolute, an SSE stream, a session store). Every expected value in this file
    comes from IT or from the repository, never from `app/agent_console.py`;
  * cluster DNS. `agent-<user>-<name>` resolves inside the namespace and not on a laptop,
    so `socket.getaddrinfo` is redirected for the names the fake cluster actually holds.
    A name the shipped code derives WRONGLY does not resolve and the request fails, which
    is what makes the derivation itself part of what is under test.
"""

import contextlib
import json
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# The fake API server, the ledger stubs and the loopback client are the same objects
# test_portal_agents.py builds for the same purpose. Imported rather than copied: two
# fake clusters that drift apart would let a change pass one file's idea of Kubernetes
# and fail the other's.
from test_portal_agents import (  # noqa: E402  - path set up by that module's import
    _SA_DIR,
    FakeCluster,
    _stub_ledger,
)

from app import agent_console, agent_usage, agents, portal  # noqa: E402

# What the measured opencode 1.18.7 console actually serves, copied out of a running agent
# pod (tests-live/test_agent_console.py re-reads it from the real one). The two properties
# that matter are that there is NO <base> element and that every asset reference is
# root-absolute — which is precisely why a prefix-mounted copy needs rewriting at all.
ENTRY_DOCUMENT = b"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>OpenCode</title>
    <link rel="icon" type="image/svg+xml" href="/favicon-v3.svg" />
    <link rel="stylesheet" href="/assets/index-S3QimprQ.css" />
    <link rel="manifest" href="/site.webmanifest" />
    <script type="module" src="/assets/index-CgMYRCpN.js"></script>
  </head>
  <body><div id="root"></div></body>
</html>
"""

BUNDLE = b'console.log("the opencode console bundle");'


class FakeDaemon:
    """A stand-in for the RESIDENT `opencode serve`, with the properties that matter.

    It is resident in the only sense a test can be: it is started once, by the fixture,
    before any request is made, and it holds an identity (`instance`) and a session store
    for its whole life. Nothing the proxy can do creates one — there is no endpoint here
    that starts a daemon — so "the same instance answered after a reconnect" means the
    console attached to something that was already there.
    """

    def __init__(self, password: str, address: str = "127.0.0.1"):
        self.password = password
        # The resident process's identity. Read back through /pid; a console that SPAWNED
        # its agent would produce a different one on the second connection.
        self.instance = str(uuid.uuid4())
        self.sessions: list[str] = []
        self.requests: list[tuple[str, str, bool]] = []   # (method, path, authenticated)
        self.release = threading.Event()                  # gates the SSE stream
        # THE REAL PORT, not an ephemeral one. anyio — which is what httpx connects
        # through — takes only the ADDRESS out of getaddrinfo and dials the port from the
        # URL, so a substitution that remapped the port would be silently ignored and the
        # test would be proving something else. Binding 4096 keeps the port under test:
        # `agents.SERVE_PORT` is what the shipped code dials and what the Service
        # publishes (deploy/k8s/64-agent.template.yaml).
        self.address = address
        self.port = agents.SERVE_PORT
        try:
            self.srv = ThreadingHTTPServer((address, self.port), self._handler())
        except OSError as exc:  # pragma: no cover - environment, not behaviour
            raise pytest.skip.Exception(
                f"cannot bind {address}:{self.port} for the fake agent daemon ({exc}). "
                "The console proxy dials the agent Service's real port, so this suite "
                "needs it free on loopback."
            ) from exc
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.release.set()
        self.srv.shutdown()
        self.srv.server_close()

    def paths(self) -> list[str]:
        return [p for _m, p, _a in self.requests]

    def _handler(daemon):  # noqa: N805 - the closure IS the handler's access to the daemon
        import base64 as _b64

        expected = "Basic " + _b64.b64encode(
            f"opencode:{daemon.password}".encode()).decode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, ctype, body: bytes, close=False):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if close:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _dispatch(self, method):
                path = urlparse(self.path).path
                ok = self.headers.get("Authorization") == expected
                daemon.requests.append((method, path, ok))
                if not ok:
                    # Exactly what the measured daemon does without the credential
                    # (tests-live/test_agent_resident.py: 401 anon, 200 with -u).
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="opencode"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if path in ("/", "/app"):
                    self._send(200, "text/html; charset=utf-8", ENTRY_DOCUMENT)
                elif path == "/assets/index-CgMYRCpN.js":
                    self._send(200, "text/javascript", BUNDLE)
                elif path == "/pid":
                    self._send(200, "application/json",
                               json.dumps({"instance": daemon.instance}).encode())
                elif path == "/session" and method == "POST":
                    session = f"ses_{len(daemon.sessions)}"
                    daemon.sessions.append(session)
                    self._send(201, "application/json",
                               json.dumps({"id": session}).encode())
                elif path == "/session":
                    self._send(200, "application/json",
                               json.dumps(daemon.sessions).encode())
                elif path.startswith("/api/fs/read/"):
                    # Echoes the RAW request line, percent-encoding and all. The daemon's
                    # own wildcard route carries an absolute file path in this position.
                    self._send(200, "application/json",
                               json.dumps({"raw": self.path}).encode())
                elif path == "/event":
                    # An open-ended stream, framed by connection close rather than a
                    # length — the shape SSE has. It writes one event, then BLOCKS until
                    # the test releases it, so a proxy that buffered the whole response
                    # before answering would never deliver that first event.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b'data: {"type":"server.connected"}\n\n')
                    self.wfile.flush()
                    daemon.release.wait(timeout=20)
                    self.wfile.write(b'data: {"type":"server.done"}\n\n')
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self._send(404, "application/json", b'{"error":"not found"}')

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self._dispatch("POST")

            def log_message(self, *a):
                pass

        return Handler


@pytest.fixture()
def world(monkeypatch):
    """A fake cluster holding one agent, and a daemon reachable at that agent's name.

    The DNS map is the cluster's, not the code's: only names the fake cluster really holds
    resolve. `agents.console_target` deriving the wrong object name therefore produces a
    connection failure rather than a quietly successful proxy to the right pod.
    """
    _stub_ledger(monkeypatch)
    cluster = FakeCluster()
    monkeypatch.setattr(agents, "KUBE_API", cluster.url)
    # The in-cluster credential's LOCATION, never the code that reads it: `_token()` opens
    # the file on every call because projected tokens rotate, and that behaviour has to
    # keep being exercised. Same substitution the environment performs in a pod.
    monkeypatch.setattr(agent_usage, "TOKEN_FILE", _SA_DIR / "token")
    monkeypatch.setattr(agent_usage, "CA_FILE", _SA_DIR / "ca.crt")
    monkeypatch.setattr(agent_usage, "NAMESPACE_FILE", _SA_DIR / "namespace")
    monkeypatch.setenv("GATEWAY_URL", cluster.url)

    password = "console-password-" + uuid.uuid4().hex[:8]
    daemon = FakeDaemon(password)

    # CLUSTER DNS, and nothing else. A Service name resolves inside the namespace and not
    # on a laptop; the port, the protocol and the connection are the shipped code's. A
    # name the code derives wrongly is simply not in this map and does not resolve, which
    # is what keeps the derivation itself inside what is under test.
    hosts: dict[str, str] = {}
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        # anyio ASCII-encodes the host before it reaches here, so the name arrives as
        # bytes on the httpx path and as str on the websockets one. Same Service name.
        key = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if key in hosts:
            return real_getaddrinfo(hosts[key], port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def add_agent(user: str, name: str, *, reachable: bool = True):
        cluster.add_agent(user, name)
        obj = f"agent-{user}-{name}"
        cluster.put("secrets", {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": f"{obj}-key"},
            "data": {
                # base64, exactly as a Secret holds it.
                "OPENCODE_SERVER_PASSWORD":
                    __import__("base64").b64encode(password.encode()).decode(),
            },
        })
        # An address that refuses, rather than a name that does not resolve, for an agent
        # that is stopped: `replicas: 0` leaves the Service and its ClusterIP in place
        # with no endpoints behind them, so the failure a user hits is a refused
        # connection and not NXDOMAIN.
        hosts[obj] = daemon.address if reachable else "127.0.0.3"

    world = type("World", (), {})()
    world.cluster = cluster
    world.daemon = daemon
    world.password = password
    world.add_agent = add_agent
    world.hosts = hosts
    try:
        yield world
    finally:
        daemon.stop()
        cluster.stop()


def _api() -> FastAPI:
    api = FastAPI()
    api.include_router(portal.router)
    api.include_router(agent_console.router)
    return api


def app_client(user: str, *, peer=("127.0.0.1", 41000)) -> TestClient:
    """The console, reached the way the oauth2-proxy sidecar reaches it."""
    client = TestClient(_api(), client=peer, raise_server_exceptions=False)
    client.headers.update({"X-Auth-Request-Preferred-Username": user})
    return client


@contextlib.contextmanager
def serving():
    """The app behind a REAL server on loopback, for the claims TestClient cannot carry.

    `TestClient` speaks ASGI in-process and its transport collects a whole response body
    before handing it back. That is fine for a status code, and it is fatal for the two
    claims below: a buffered transport makes an un-streamed proxy look streamed, and an
    in-process call makes a "disconnect" a function return rather than a closed socket.
    So those tests get uvicorn, a socket, and a client that can be thrown away — and the
    identity path is unchanged, because a connection from 127.0.0.1 is exactly what
    `require_user` requires and what the sidecar produces.
    """
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    # loop="asyncio", NOT uvloop. This server's whole reason to exist is the fake-DNS map
    # in the `world` fixture, which stubs `socket.getaddrinfo` so `agent-<user>-<name>`
    # resolves to loopback. uvloop (pulled in by uvicorn[standard]) resolves names through
    # its own getaddrinfo and never calls socket.getaddrinfo, so under it the proxy's
    # upstream dial hits the real resolver, gets NXDOMAIN, and the route 502s — which is a
    # property of the loop implementation, not of the code under test. Forcing asyncio keeps
    # the stub authoritative on any host, whether or not uvloop is installed.
    config = uvicorn.Config(_api(), host="127.0.0.1", port=port, log_level="warning",
                            loop="asyncio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the portal never came up on loopback"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def as_user(base: str, user: str) -> "httpx.Client":
    import httpx

    return httpx.Client(
        base_url=base,
        headers={"X-Auth-Request-Preferred-Username": user},
        timeout=10.0,
    )


# ---------------------------------------------------------------- attach


def test_the_owner_attaches_and_the_console_is_served_under_its_own_prefix(world):
    """The console loads, and everything it will later ask for stays inside its path.

    The asset references are the load-bearing part. opencode's console is built to sit at
    the root of an origin; the root of THIS origin is the chat surface, so an unrewritten
    `/assets/index.js` would be answered by LibreChat and the console would render as a
    blank page with a 404 in the network tab.
    """
    world.add_agent("alice", "scraper")
    alice = app_client("alice")

    page = alice.get("/agents/scraper/app")
    assert page.status_code == 200, page.text
    body = page.text

    assert "/agents/scraper/assets/index-CgMYRCpN.js" in body, body
    assert "/agents/scraper/assets/index-S3QimprQ.css" in body
    assert "/agents/scraper/site.webmanifest" in body
    assert not re.search(r'(src|href)="/(?!agents/scraper/)', body), (
        "a root-absolute reference escaped the console's prefix and would be answered by "
        f"whatever serves the origin root:\n{body}"
    )
    # The shim, and its prefix. Without it the compiled bundle resolves its server as
    # location.origin with no path and every API call leaves the prefix at runtime.
    assert '"/agents/scraper"' in body and "window.fetch=function" in body, body

    # And the rewritten URL is not a guess: it resolves, through this same proxy.
    asset = alice.get("/agents/scraper/assets/index-CgMYRCpN.js")
    assert asset.status_code == 200
    assert asset.content == BUNDLE

    # The browser never carries the daemon's credential; this hop adds it.
    assert all(authed for _m, _p, authed in world.daemon.requests), world.daemon.requests
    assert "authorization" not in {k.lower() for k in alice.headers}


def test_a_percent_encoded_path_reaches_the_daemon_unchanged(world):
    """`/api/fs/read/*` carries an encoded absolute file path in its wildcard.

    The ASGI server decodes the path before FastAPI sees it, so forwarding the decoded
    form would turn `%2Fworkspace%2Fwork%2Fnotes.md` into three extra path segments and
    ask the daemon for a different resource than the console asked for — which reads as
    "the file viewer is broken", not as a proxy bug.
    """
    world.add_agent("alice", "scraper")
    encoded = "%2Fworkspace%2Fwork%2Fnotes.md"
    resp = app_client("alice").get(f"/agents/scraper/api/fs/read/{encoded}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["raw"] == f"/api/fs/read/{encoded}", resp.json()


def test_repeated_query_parameters_survive_the_hop(world):
    world.add_agent("alice", "scraper")
    resp = app_client("alice").get("/agents/scraper/api/fs/read/x?k=1&k=2")
    assert resp.status_code == 200, resp.text
    assert resp.json()["raw"].endswith("?k=1&k=2"), resp.json()


def test_the_bare_path_redirects_so_the_consoles_own_urls_resolve(world):
    world.add_agent("alice", "scraper")
    resp = app_client("alice").get("/agents/scraper", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/agents/scraper/"


def test_the_event_stream_is_relayed_as_it_arrives_and_not_buffered(world):
    """SSE, which is how the console learns anything after the page loads.

    The daemon holds the stream open after its first event until this test releases it. A
    proxy that read the response to completion before answering would block here until the
    read timeout, so this asserts a property of the transfer rather than of the payload.
    """
    world.add_agent("alice", "scraper")

    with serving() as base, as_user(base, "alice") as alice:
        started = time.time()
        with alice.stream("GET", "/agents/scraper/event") as stream:
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            first = next(line for line in stream.iter_lines() if line)
            elapsed = time.time() - started
            assert "server.connected" in first, first
            assert elapsed < 5, (
                f"the first event took {elapsed:.1f}s to arrive while the daemon held the "
                "stream open. The response was buffered, so the console would receive "
                "nothing until the agent finished — which for an event stream is never."
            )
            world.daemon.release.set()


# ---------------------------------------------------------------- ATTACH, not spawn


def test_reconnecting_reaches_the_same_daemon_with_its_session_intact(world):
    """Contract 2's whole claim, stated as what must NOT change across a disconnect.

    The console is opened, a session is created through it, the client is torn down
    completely — every connection closed, as closing the browser does — and a second,
    independent client attaches. The daemon's identity and its session store must be the
    ones from before. A console that spawned its agent would answer both halves happily
    with a fresh instance and an empty list, which is exactly the Code surface's behaviour
    that this surface exists not to have (finding 43).
    """
    world.add_agent("alice", "scraper")

    with serving() as base:
        first = as_user(base, "alice")
        before = first.get("/agents/scraper/pid").json()["instance"]
        created = first.post("/agents/scraper/session")
        assert created.status_code == 201, created.text
        session = created.json()["id"]
        assert first.get("/agents/scraper/session").json() == [session]
        # THE DISCONNECT. A real client with real sockets, closed — which is what closing
        # the browser does, and what an in-process ASGI call cannot express.
        first.close()
        time.sleep(0.2)

        second = as_user(base, "alice")
        after = second.get("/agents/scraper/pid").json()["instance"]
        assert after == before, (
            f"the console reached a different daemon after reconnecting "
            f"({before} -> {after}). That is a spawn, not an attach, and it means the "
            "agent does not survive the browser closing — the entire difference between "
            "an Agent and the Code surface."
        )
        assert second.get("/agents/scraper/session").json() == [session], (
            "the session did not survive the disconnect"
        )
        assert world.daemon.sessions == [session], (
            "attaching created a second session; the console must join what is there"
        )
        second.close()


def test_attaching_asks_the_daemon_for_nothing_but_what_the_client_asked_for(world):
    """No lifecycle call is made on the way in.

    Every path the daemon saw is one the client requested. If attaching ever grew a
    "make sure it is up" step — a start, a scale, a spawn — it would show here as a path
    nobody asked for, which is the shape the regression would take.
    """
    world.add_agent("alice", "scraper")
    alice = app_client("alice")
    alice.get("/agents/scraper/app")
    alice.get("/agents/scraper/pid")
    assert world.daemon.paths() == ["/app", "/pid"], world.daemon.paths()


# ---------------------------------------------------------------- reject


def test_a_second_user_cannot_attach_to_another_users_console(world):
    """The attack: know the name, ask for the console, see what happens.

    404, and — the assertion that actually matters — the daemon received NOTHING. A
    status code alone would still pass if the request had been forwarded and the answer
    discarded, and by then the console's credential would already have been presented on
    somebody else's behalf.
    """
    world.add_agent("alice", "scraper")
    before = list(world.daemon.requests)

    mallory = app_client("mallory")
    resp = mallory.get("/agents/scraper/app")
    assert resp.status_code == 404, (
        f"mallory reached alice's console with {resp.status_code}"
    )
    assert world.daemon.requests == before, (
        f"alice's daemon was contacted on mallory's behalf: {world.daemon.requests}"
    )


def test_a_hyphen_collision_cannot_attach_to_another_users_console(world):
    """Why deriving the object name is necessary and not sufficient.

    `alice` + `bot-two` and `alice-bot` + `two` both derive `agent-alice-bot-two`. The
    derivation alone would hand alice a live console on alice-bot's agent — its session,
    its files, its spendable key. The owner LABEL is what refuses, and it is checked on
    the console path exactly as it is on stop/start/delete.
    """
    world.add_agent("alice-bot", "two")
    before = list(world.daemon.requests)

    resp = app_client("alice").get("/agents/bot-two/app")
    assert resp.status_code == 404, (
        f"alice attached to alice-bot's agent through the shared object name "
        f"agent-alice-bot-two ({resp.status_code})"
    )
    assert world.daemon.requests == before

    # And the rightful owner still gets in, or the check above would pass by refusing
    # everybody.
    owner = app_client("alice-bot").get("/agents/two/app")
    assert owner.status_code == 200, owner.text


def test_an_agent_that_does_not_exist_is_a_404_and_not_a_proxy_error(world):
    resp = app_client("alice").get("/agents/nothing-here/app")
    assert resp.status_code == 404


def test_identity_headers_are_ignored_from_anywhere_but_the_sidecar(world):
    """The console is not exempt from portal.py's loopback rule.

    A pod in the namespace that can reach the control-plane Service can set any identity
    header it likes. If this route honoured them, every agent console in the deployment
    would be reachable from any workspace by writing one header.
    """
    world.add_agent("alice", "scraper")
    from_another_pod = app_client("alice", peer=("10.42.1.7", 55000))
    resp = from_another_pod.get("/agents/scraper/app")
    assert resp.status_code == 403, resp.text
    assert world.daemon.requests == []


def test_a_stopped_agent_reads_as_stopped_rather_than_as_a_broken_page(world):
    """`replicas: 0` means the Service has no endpoints. The user gets a sentence.

    502 with an instruction, not a hang and not a 500: stop is a supported state in
    Contract 2, so attaching to a stopped agent is a normal thing to do by accident.
    """
    world.add_agent("alice", "sleeping", reachable=False)
    resp = app_client("alice").get("/agents/sleeping/app")
    assert resp.status_code == 502, resp.text
    assert "start it from the Agents tab" in resp.text


def test_the_console_credential_is_never_echoed_to_the_browser(world):
    """The daemon's password is the second lock. It must not leave this hop."""
    world.add_agent("alice", "scraper")
    alice = app_client("alice")
    page = alice.get("/agents/scraper/app")
    assert world.password not in page.text
    assert not any(world.password in v for v in page.headers.values())


# ---------------------------------------------------------------- the websocket


def test_the_console_websocket_is_bridged_to_the_owners_daemon(world):
    """opencode's terminal panel is a websocket, so the bridge is part of the console.

    Proven end to end: the browser's frame reaches the daemon, the daemon's reply reaches
    the browser, and the daemon saw the Basic credential this hop adds. The refusal case
    below is the one that matters — a websocket that ignored ownership would be a live
    terminal on somebody else's agent.
    """
    websockets = pytest.importorskip("websockets")
    import asyncio

    seen: dict = {}

    async def handler(conn):
        # websockets>=14 moved the handshake request under `conn.request` (a Request with
        # `.headers`/`.path`); <=13 (the version control-plane/requirements.txt pins, and
        # what the image runs) exposes `conn.request_headers`/`conn.path` directly. Read
        # whichever exists so the fake daemon drives the bridge on either — the bridge
        # itself is what is under test, not the version of the server standing in for it.
        req = getattr(conn, "request", None)
        if req is not None:
            seen["auth"] = req.headers.get("Authorization")
            seen["path"] = req.path
        else:
            seen["auth"] = conn.request_headers.get("Authorization")
            seen["path"] = conn.path
        async for message in conn:
            await conn.send(f"echo:{message}")

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    # A SECOND loopback address, because this daemon must answer on the same real port
    # 4096 as the HTTP one — see FakeDaemon for why the port cannot be substituted.
    WS_ADDRESS = "127.0.0.2"

    async def serve():
        await websockets.serve(handler, WS_ADDRESS, agents.SERVE_PORT)
        ready.set()
        await asyncio.Future()

    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(serve())
        except (RuntimeError, OSError):
            pass          # the test stops this loop when it is done with it

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(10), "the fake daemon's websocket server never started"

    world.add_agent("alice", "scraper")
    world.hosts["agent-alice-scraper"] = WS_ADDRESS

    with app_client("alice").websocket_connect("/agents/scraper/api/pty/p1/connect") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo:hello"
    assert seen["path"].endswith("/api/pty/p1/connect"), seen
    assert (seen["auth"] or "").startswith("Basic "), (
        "the bridge reached the daemon without the credential this hop is supposed to add"
    )

    # The refusal, on the same socket path and against the same running daemon.
    with pytest.raises(Exception):
        with app_client("mallory").websocket_connect(
            "/agents/scraper/api/pty/p1/connect"
        ):
            pass

    loop.call_soon_threadsafe(loop.stop)
