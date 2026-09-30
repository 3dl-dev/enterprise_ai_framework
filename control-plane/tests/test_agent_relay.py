"""Contract G: the agent relay (agents-raven.md), as a security control on the cross-agent path.

A Raven holds an owner-scoped agent-manager token. Through
`POST /agent-manager/v1/agents/<n>/relay/v1/chat/completions` it sends a turn to its owner's
hermes agent, whose API server (:8642) the network admits from the control plane alone. The
relay is therefore the ONLY door between agents, and this file attacks each rule the EAF
owner's review put on it: owner scoping, idle and total timeouts, cancel on disconnect, the
per-token concurrent-stream cap (a DoS guard, not a quota), the server-derived
X-Hermes-Session-Key, stripped inbound Authorization and X-Hermes-*, and one audit row per
turn that never holds the conversation.

WHAT IS REAL

  * the control-plane app, served by a real uvicorn on a real socket (the same harness as
    test_agent_manager_token.py, whose fixtures are reused): the token verifier, the router,
    `agents.relay_target` with the `_owned_deployment` guard, and `agent_relay` itself are
    all unpatched;
  * a real Postgres: the token table and the hash-chained audit trail;
  * the agents the relay targets are created through the REAL routes (a Raven's token
    creating its own hermes child, the portal creating another user's), so the API_SERVER_KEY
    the relay must present is the one the real provisioner wrote into the real Secret shape.

TEST DOUBLES, NAMED AS SUCH (separate systems, reached over real HTTP):

  * the Kubernetes API server: `FakeCluster` (a real HTTP object store). It has no kubelet,
    so a "running" agent is a Pod object the test writes, labelled as the kubelet's would be;
  * the hermes API server: `FakeHermes` below. Its contract is the one measured against the
    real `nousresearch/hermes-agent:v2026.8.3` by enterpriseaiframework-147
    (tests-live/test_hermes_api_server_isolation.py): POST /v1/chat/completions, Bearer
    API_SERVER_KEY or 401, OpenAI-shaped JSON or `data:` SSE frames ending `[DONE]`. It
    RECORDS what reached it (headers, body) and SEES a cancel as the socket closing, which is
    exactly how a real server learns its client is gone.

The live proof (a real Raven, a real hermes, real NetworkPolicies) is
tests-live/test_agent_relay_live.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import select
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from test_agent_manager_token import (  # noqa: E402 - sets up sys.path and the app import
    ClusterAndGateway, FakeKeycloak, World, _free_port, _real_asyncpg, postgres, sql,  # noqa: F401
)
from test_portal_agents import _SA_DIR  # noqa: E402

import uvicorn  # noqa: E402

from app import agent_usage, agents, db, freerouter, main  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "deploy" / "raven"))
import eaf_agents_mcp  # noqa: E402 - the Raven-side tool, driven against the real relay

MODELS = (agents.DEFAULT_MODEL, "other-model")


# ---------------------------------------------------------------- the hermes API server double


class FakeHermes:
    """hermes's API server on :8642, as far as the relay can tell.

    Behaviour is chosen by the LAST user message, i.e. by the data the relay forwarded:
      reply:<text>  SSE stream: the text in two deltas, then [DONE] (or JSON if stream=false)
      stall         SSE headers and one delta, then silence until the client goes
      nohead        silence BEFORE the response headers until the client goes
      dribble       SSE headers, then one keep-alive delta every 0.2 s until the client goes
    """

    def __init__(self, keys: dict[str, str]):
        self.keys = keys  # expected Bearer per agent host, read from the real Secrets
        # openclaw agents (enterpriseaiframework-b12): host -> the owner whose identity the
        # gateway will accept. They have no key; the policy below is the one the SHIPPED seed
        # config asks a real gateway to enforce (agents.openclaw_seed_config), not restated.
        self.claws: dict[str, str] = {}
        seed = json.loads(agents.openclaw_seed_config("owner", "claw", agents.DEFAULT_MODEL))
        self.claw_policy = seed["gateway"]["auth"]["trustedProxy"]
        self.claw_chat_enabled = seed["gateway"]["http"]["endpoints"]["chatCompletions"]["enabled"]
        self.seen: list[dict] = []
        self.cancelled: list[str] = []
        self.active = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # close-delimited bodies: no framing to get wrong

            def log_message(self, *a):
                pass

            def _client_gone(self, wait: float) -> bool:
                r, _, _ = select.select([self.connection], [], [], wait)
                if not r:
                    return False
                try:
                    return self.connection.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    return True

            def _hold_until_gone(self, mode: str, limit: float = 30.0):
                end = time.time() + limit
                while time.time() < end:
                    if self._client_gone(0.05):
                        fake.cancelled.append(mode)
                        return
                raise AssertionError(f"{mode}: the relay never closed the upstream request")

            def _sse(self, text: str):
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode())
                self.wfile.flush()

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw or b"{}")
                fake.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "body": body})
                host = self.headers.get("Host", "").split(":")[0]
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                if host in fake.claws:
                    h = {k.lower(): v for k, v in self.headers.items()}
                    pol = fake.claw_policy
                    # allowUsers in the real seed names the owner the agent was rendered for.
                    ok = (fake.claw_chat_enabled
                          and h.get(pol["userHeader"]) == fake.claws[host]
                          and all(r in h for r in pol["requiredHeaders"])
                          and h.get("x-forwarded-for", "127.0.0.1") != "127.0.0.1")
                    if not ok:
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":{"message":"unauthorized"}}')
                        return
                    # openclaw treats `model` as an AGENT TARGET (docs/gateway/openai-http-api.md
                    # in the 2026.9.6 image): anything but these is an unknown agent.
                    if body.get("model") not in ("openclaw", "openclaw/default"):
                        self.send_response(404)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":{"message":"unknown agent target"}}')
                        return
                elif self.headers.get("Authorization") != f"Bearer {fake.keys.get(host, '')}":
                    out = json.dumps({"error": {"message": "Invalid API key"}}).encode()
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(out)
                    return
                prompt = body["messages"][-1]["content"]
                fake.active += 1
                try:
                    if prompt == "nohead":
                        self._hold_until_gone("nohead")
                        return
                    if prompt.startswith("reply:") and not body.get("stream"):
                        out = json.dumps({"choices": [{"message": {
                            "role": "assistant", "content": prompt[6:]}}]}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("X-Hermes-Session-Id", "hermes-internal-session")
                        self.end_headers()
                        self.wfile.write(out)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("X-Hermes-Session-Id", "hermes-internal-session")
                    self.end_headers()
                    if prompt.startswith("reply:") or prompt.startswith("linger:"):
                        text = prompt.split(":", 1)[1]
                        self._sse(text[: len(text) // 2])
                        self._sse(text[len(text) // 2:])
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        if prompt.startswith("linger:"):
                            # What the live hermes did (-692 live run): the terminator is
                            # sent, the connection stays open a while longer.
                            self._hold_until_gone("linger")
                        return
                    if prompt == "stall":
                        self._sse("partial")
                        self._hold_until_gone("stall")
                        return
                    if prompt == "dribble":
                        end = time.time() + 30
                        while time.time() < end:
                            try:
                                self._sse(".")
                            except OSError:
                                fake.cancelled.append("dribble")
                                return
                            if self._client_gone(0.2):
                                fake.cancelled.append("dribble")
                                return
                        raise AssertionError("dribble: the relay never closed the upstream")
                    raise AssertionError(f"unknown mode {prompt!r}")
                finally:
                    fake.active -= 1

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def wait_cancelled(self, mode: str, timeout: float = 5.0) -> float:
        start = time.time()
        while time.time() - start < timeout:
            if mode in self.cancelled:
                return time.time() - start
            time.sleep(0.02)
        raise AssertionError(f"the upstream never saw the {mode!r} request cancelled "
                             f"(cancelled so far: {self.cancelled})")

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------- the world


class RelayWorld(World):
    def __init__(self, *a, hermes: FakeHermes, **k):
        super().__init__(*a, **k)
        self.hermes = hermes

    def run_pod(self, user: str, name: str):
        """What the kubelet would report once the agent's pod is up."""
        labels = self.cluster.get("deployments", f"agent-{user}-{name}")["metadata"]["labels"]
        self.cluster.put("pods", {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": f"agent-{user}-{name}-0", "labels": dict(labels)},
            "status": {"phase": "Running"}})

    def hermes_child(self, token: str, user: str, name: str) -> str:
        """The Raven creates a hermes child through its token; it comes up; return its key."""
        made = self.pod(token).post("/agent-manager/v1/agents",
                                    json={"name": name, "type": "hermes"})
        assert made.status_code == 201, made.text
        return self._wire(user, name)

    def openclaw_child(self, token: str, user: str, name: str) -> None:
        made = self.pod(token).post("/agent-manager/v1/agents",
                                    json={"name": name, "type": "openclaw"})
        assert made.status_code == 201, made.text
        self._wire_claw(user, name)

    def portal_openclaw(self, user: str, name: str) -> None:
        made = self.portal(user).post("/portal/api/agents", json={"name": name, "type": "openclaw"})
        assert made.status_code == 201, made.text
        self._wire_claw(user, name)

    def _wire_claw(self, user: str, name: str) -> None:
        obj = f"agent-{user}-{name}"
        self.hermes.claws[obj] = user
        self.hosts[obj] = "127.0.0.1"
        self.run_pod(user, name)

    def portal_hermes(self, user: str, name: str) -> str:
        made = self.portal(user).post("/portal/api/agents", json={"name": name, "type": "hermes"})
        assert made.status_code == 201, made.text
        return self._wire(user, name)

    def _wire(self, user: str, name: str) -> str:
        obj = f"agent-{user}-{name}"
        key = self.secret_value(obj, "API_SERVER_KEY")
        self.hermes.keys[obj] = key
        self.hosts[obj] = "127.0.0.1"
        self.run_pod(user, name)
        return key

    def relay_path(self, name: str) -> str:
        return f"/agent-manager/v1/agents/{name}/relay/v1/chat/completions"

    def turn(self, token: str, name: str, prompt: str, *, stream: bool = True, **headers):
        return self.pod(token, **headers).post(
            self.relay_path(name),
            json={"model": name, "stream": stream,
                  "messages": [{"role": "user", "content": prompt}]})


@pytest.fixture()
def world(postgres, monkeypatch):
    sql(postgres, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    cluster = ClusterAndGateway()
    idp = FakeKeycloak()
    hermes = FakeHermes({})
    hosts: dict[str, str] = {}
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *a, **k):
        key = host.decode() if isinstance(host, (bytes, bytearray)) else host
        return real_getaddrinfo(hosts.get(key, host), port, *a, **k)

    # Addresses and file locations only: what a pod gets from DNS and its environment.
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(agents, "KUBE_API", cluster.url)
    monkeypatch.setattr(agents, "HERMES_API_PORT", hermes.port)
    monkeypatch.setattr(agents, "OPENCLAW_PORT", hermes.port)
    monkeypatch.setattr(agent_usage, "TOKEN_FILE", _SA_DIR / "token")
    monkeypatch.setattr(agent_usage, "CA_FILE", _SA_DIR / "ca.crt")
    monkeypatch.setattr(agent_usage, "NAMESPACE_FILE", _SA_DIR / "namespace")
    monkeypatch.setattr(db, "asyncpg", _real_asyncpg())
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(freerouter, "alias_resolver", freerouter.alias_resolver)
    for k, v in {
        "CONTROL_PLANE_DATABASE_URL": postgres,
        "CONTROL_PLANE_ADMIN_TOKEN": "admin-692",
        "GATEWAY_URL": cluster.url, "GATEWAY_MASTER_KEY": "sk-master-692",
        "GATEWAY_PROVIDER": "litellm",
        "AGENT_MODELS": ",".join(MODELS), "CATALOG_URL": "http://127.0.0.1:9",
        "AGENT_USAGE_ENABLED": "0", "ANALYTICS_COLLECT_ENABLED": "0",
        "IDP_URL": idp.url, "IDP_CLIENT_SECRET": "kc-secret",
    }.items():
        monkeypatch.setenv(k, v)
    for k in ("CHAT_MONGO_URL", "FREEROUTER_URL", "AGENT_RELAY_IDLE_TIMEOUT",
              "AGENT_RELAY_TOTAL_TIMEOUT", "AGENT_RELAY_MAX_STREAMS", "AGENT_RELAY_MAX_BODY"):
        monkeypatch.delenv(k, raising=False)
    for u in ("alice", "bob"):
        idp.users.append({"id": f"kc-{u}", "username": u, "email": f"{u}@x", "enabled": True})

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        main.app, host="0.0.0.0", port=port, proxy_headers=False, lifespan="on",
        loop="asyncio", log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline and thread.is_alive():
        time.sleep(0.05)
    if not server.started:
        pytest.fail("the control-plane app did not start")
    sql(postgres,
        "INSERT INTO principal (idp_user_id, username, email, enabled) VALUES "
        "('kc-alice','alice','alice@x',TRUE), ('kc-bob','bob','bob@x',TRUE)")
    try:
        yield RelayWorld(postgres, cluster, None, idp, port, hosts, hermes=hermes)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        hermes.stop()
        idp.stop()
        cluster.stop()


def _wait_audit(world, action: str, n: int, timeout: float = 5.0) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        rows = world.audit(action)
        if len(rows) >= n:
            return rows
        time.sleep(0.05)
    return world.audit(action)


def _read_until(s: socket.socket, marker: bytes, timeout: float = 5.0) -> bytes:
    s.settimeout(timeout)
    got = b""
    while marker not in got:
        chunk = s.recv(65536)
        if not chunk:
            break
        got += chunk
    return got


def _sse_frames(text: str) -> list:
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


# ================================================================ the path works


def test_a_ravens_turn_reaches_its_own_hermes_child_and_the_reply_streams_back(world):
    token = world.create_raven("alice", "rv")
    key = world.hermes_child(token, "alice", "helper")

    r = world.turn(token, "helper", "reply:hello from helper")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(r.text)
    assert "".join(f["choices"][0]["delta"]["content"] for f in frames if f != "[DONE]") \
        == "hello from helper"
    assert frames[-1] == "[DONE]"

    # The target was reached with ITS OWN key, read from its Secret, never the Raven's token.
    (seen,) = world.hermes.seen
    assert seen["headers"]["authorization"] == f"Bearer {key}"
    assert token not in json.dumps(seen)
    assert seen["body"]["messages"] == [{"role": "user", "content": "reply:hello from helper"}]
    # The agent's own session id stays on the relay<->agent leg.
    assert "x-hermes-session-id" not in {k.lower() for k in r.headers}

    # A non-streamed turn works the same way (the relay does not care which the Raven asks).
    j = world.turn(token, "helper", "reply:plain json", stream=False)
    assert j.status_code == 200 and j.json()["choices"][0]["message"]["content"] == "plain json"


def test_the_raven_side_tool_lists_creates_and_talks_to_a_child_by_name(world, monkeypatch):
    """deploy/raven/eaf_agents_mcp.py's three tools, against the real relay and API."""
    token = world.create_raven("alice", "rv")
    base = f"http://{world.pod_ip}:{world.port}/agent-manager/v1"
    monkeypatch.setenv("EAF_AGENT_MANAGER_TOKEN", token)

    made = json.loads(eaf_agents_mcp.create_agent(base, "scout", "hermes"))
    assert made["created"] == "scout"
    world._wire("alice", "scout")
    listed = json.loads(eaf_agents_mcp.list_agents(base))
    assert {(a["name"], a["type"]) for a in listed["agents"]} == {("rv", "raven"), ("scout", "hermes")}
    assert eaf_agents_mcp.send_to_agent(base, "scout", "reply:scouting done") == "scouting done"

    # A refusal is reported as one, and a timeout is never passed off as a complete reply.
    assert eaf_agents_mcp.send_to_agent(base, "nosuch", "reply:x").startswith("refused (404)")
    monkeypatch.setenv("AGENT_RELAY_IDLE_TIMEOUT", "0.5")
    out = eaf_agents_mcp.send_to_agent(base, "scout", "stall")
    assert out.startswith("the turn to 'scout' failed") and "partial reply: partial" in out, out


def test_the_tool_reads_the_token_from_the_container_env_not_its_own(tmp_path, monkeypatch):
    """Raven starts stdio MCP servers with only HOME/PATH/..., so the token comes from PID 1's
    environment. A file with the real /proc/<pid>/environ format stands in for it."""
    monkeypatch.delenv("EAF_AGENT_MANAGER_TOKEN", raising=False)
    environ = tmp_path / "environ"
    environ.write_bytes(b"PATH=/usr/bin\0EAF_AGENT_MANAGER_TOKEN=eafam_fromproc\0HOME=/data\0")
    assert eaf_agents_mcp._token(str(environ)) == "eafam_fromproc"
    environ.write_bytes(b"PATH=/usr/bin\0HOME=/data\0")
    with pytest.raises(RuntimeError, match="no agent-manager token"):
        eaf_agents_mcp._token(str(environ))


# ================================================================ owner scoping


def test_another_users_agent_is_refused_and_never_contacted(world):
    token_a = world.create_raven("alice", "rv")
    world.hermes_child(token_a, "alice", "mine")
    world.portal_hermes("bob", "bobs")
    token_b = world.create_raven("bob", "brv")

    for target in ("bobs", "brv", "nosuch"):
        r = world.turn(token_a, target, "reply:should never arrive")
        assert r.status_code == 404, (target, r.status_code, r.text)
    # Nothing reached any agent: the refusal is the owner guard, not the upstream's auth.
    assert world.hermes.seen == []

    # The identity fields a caller could forge change nothing.
    forged = world.turn(token_a, "bobs", "reply:x", **{
        "X-Auth-Request-Preferred-Username": "bob", "X-Forwarded-User": "bob"})
    assert forged.status_code == 404 and world.hermes.seen == []

    # bob's own Raven reaches bob's agent: the 404 above was about ownership, not the agent.
    ok = world.turn(token_b, "bobs", "reply:bob here")
    assert ok.status_code == 200 and "bob" in ok.text
    assert world.hermes.seen[-1]["headers"]["host"].startswith("agent-bob-bobs")

    denied = world.audit("agent-manager.denied")
    assert [(d["actor"], d["target"], d["detail"]["status"], d["detail"]["attempted"])
            for d in denied] == [
        ("agent-manager:alice/rv", "alice/bobs", 404, "agent.relay"),
        ("agent-manager:alice/rv", "alice/brv", 404, "agent.relay"),
        ("agent-manager:alice/rv", "alice/nosuch", 404, "agent.relay"),
        ("agent-manager:alice/rv", "alice/bobs", 404, "agent.relay")]


def test_the_hyphen_collision_cannot_reach_a_neighbours_agent(world):
    """SECOND MUTATION of the owner boundary. `agent-<user>-<name>` is ambiguous: alice's
    agent `bot-two` and user `alice-bot`'s agent `two` are both `agent-alice-bot-two`. The
    name alone resolves to the neighbour's live agent; only the label re-check stops it."""
    world.idp.users.append({"id": "kc-ab", "username": "alice-bot", "email": "ab@x",
                            "enabled": True})
    sql(world.dsn, "INSERT INTO principal (idp_user_id, username, email, enabled) VALUES "
                   "('kc-ab','alice-bot','ab@x',TRUE)")
    token = world.create_raven("alice", "rv")
    world.portal_hermes("alice-bot", "two")
    assert world.cluster.get("deployments", "agent-alice-bot-two") is not None

    r = world.turn(token, "bot-two", "reply:crossed")
    assert r.status_code == 404, r.text
    assert world.hermes.seen == []
    # The SAME answer as for a name nobody has: anything more specific ("not running")
    # would tell alice's Raven that an agent by that name exists.
    assert r.json() == world.turn(token, "nobody-has-this", "reply:x").json() | {
        "detail": "you have no agent called 'bot-two'"}, r.text


def test_self_stopped_starting_non_hermes_and_keyless_targets_are_refused(world):
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "sleepy")

    assert world.turn(token, "rv", "reply:x").status_code == 403  # itself
    # Stopped (replicas 0) and starting (no Running pod): 404 with the reason, never a hang.
    assert world.pod(token).post("/agent-manager/v1/agents/sleepy/stop").status_code == 200
    world.cluster.store.pop(("pods", "agent-alice-sleepy-0"))
    r = world.turn(token, "sleepy", "reply:x")
    assert r.status_code == 404 and "not running" in r.text
    assert world.pod(token).post("/agent-manager/v1/agents/sleepy/start").status_code == 200
    r = world.turn(token, "sleepy", "reply:x")
    assert r.status_code == 404 and "not running" in r.text
    world.run_pod("alice", "sleepy")
    assert world.turn(token, "sleepy", "reply:awake").status_code == 200

    # An opencode-typed agent is not a relay target: the same 404 as "no such agent".
    made = world.pod(token).post("/agent-manager/v1/agents", json={"name": "claw", "type": "openclaw"})
    assert made.status_code == 201, made.text
    world.run_pod("alice", "claw")
    world.cluster.get("deployments", "agent-alice-claw")["metadata"]["labels"]["agent.enterprise-ai/type"] = "opencode"
    assert world.turn(token, "claw", "reply:x").status_code == 404

    # A hermes agent from before -147 has no API_SERVER_KEY: 503 naming the fix.
    secret = world.cluster.get("secrets", "agent-alice-sleepy-key")
    secret["data"].pop("API_SERVER_KEY")
    r = world.turn(token, "sleepy", "reply:x")
    assert r.status_code == 503 and "re-provision" in r.text
    assert len(world.hermes.seen) == 1  # only the one legitimate turn ever arrived


# ================================================================ headers and the session key


def expected_session_key(owner: str, raven: str, target: str) -> str:
    # The documented derivation, restated here (agents-raven.md: a stable hash of
    # (owner, raven, target)). The properties below are what make it a control.
    return "eaf-relay-" + hashlib.sha256(json.dumps([owner, raven, target]).encode()).hexdigest()[:48]


def test_inbound_authorization_and_x_hermes_headers_never_reach_the_agent(world):
    token = world.create_raven("alice", "rv")
    token2 = world.create_raven("alice", "rv2")
    world.hermes_child(token, "alice", "h1")
    world.hermes_child(token, "alice", "h2")

    smuggled = {
        "X-Hermes-Session-Key": "bob-memory-scope",
        "X-Hermes-Session-Id": "someone-elses-session",
        "X-Hermes-Anything": "1",
        "Cookie": "hermes_session=stolen",
        "X-Forwarded-For": "10.0.0.9",
        "X-Api-Key": "sk-other",
    }
    r = world.turn(token, "h1", "reply:ok", **smuggled)
    assert r.status_code == 200, r.text
    got = {k.lower(): v for k, v in world.hermes.seen[-1]["headers"].items()}

    assert got["authorization"] == f"Bearer {world.hermes.keys['agent-alice-h1']}"
    assert token not in json.dumps(got), "the Raven's token must never reach an agent"
    hermes_headers = {k: v for k, v in got.items() if k.startswith("x-hermes-")}
    assert hermes_headers == {"x-hermes-session-key": expected_session_key("alice", "rv", "h1")}
    for name in ("cookie", "x-forwarded-for", "x-api-key"):
        assert name not in got, name

    # One conversation per (Raven, agent) pair: stable across turns, distinct across pairs,
    # and whatever the Raven sends cannot move it.
    keys = {}
    for tok, raven in ((token, "rv"), (token2, "rv2")):
        for target in ("h1", "h2"):
            world.turn(tok, target, "reply:k", **{"X-Hermes-Session-Key": f"{raven}-{target}"})
            keys[(raven, target)] = world.hermes.seen[-1]["headers"]["x-hermes-session-key"]
    assert keys[("rv", "h1")] == hermes_headers["x-hermes-session-key"]
    assert len(set(keys.values())) == 4
    assert all(len(k) <= 256 and k.startswith("eaf-relay-") for k in keys.values())


# ================================================================ timeouts and cancellation


def test_an_upstream_silent_before_its_headers_ends_at_the_idle_timeout(world, monkeypatch):
    monkeypatch.setenv("AGENT_RELAY_IDLE_TIMEOUT", "0.6")
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "h")
    t0 = time.time()
    r = world.turn(token, "h", "nohead", stream=False)
    elapsed = time.time() - t0
    assert r.status_code == 504 and r.json()["error"]["code"] == "timeout_idle", r.text
    assert 0.5 < elapsed < 5, elapsed
    world.hermes.wait_cancelled("nohead")
    (row,) = _wait_audit(world, "agent.relay.turn", 1)
    assert row["detail"]["outcome"] == "timeout_idle"


def test_a_stalled_stream_ends_at_the_idle_timeout_with_an_error_event(world, monkeypatch):
    monkeypatch.setenv("AGENT_RELAY_IDLE_TIMEOUT", "0.6")
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "h")
    t0 = time.time()
    r = world.turn(token, "h", "stall")
    assert time.time() - t0 < 5
    frames = _sse_frames(r.text)
    assert frames[0]["choices"][0]["delta"]["content"] == "partial"
    assert frames[-1]["error"]["code"] == "timeout_idle", frames
    world.hermes.wait_cancelled("stall")
    (row,) = _wait_audit(world, "agent.relay.turn", 1)
    assert row["detail"]["outcome"] == "timeout_idle"


def test_a_slow_dribbling_stream_ends_at_the_total_timeout(world, monkeypatch):
    # Every 0.2 s a byte arrives, so the idle timeout (2 s) never fires; the total (1.2 s) does.
    monkeypatch.setenv("AGENT_RELAY_IDLE_TIMEOUT", "2")
    monkeypatch.setenv("AGENT_RELAY_TOTAL_TIMEOUT", "1.2")
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "h")
    t0 = time.time()
    r = world.turn(token, "h", "dribble")
    elapsed = time.time() - t0
    frames = _sse_frames(r.text)
    assert len(frames) > 3 and frames[-1]["error"]["code"] == "timeout_total", frames[-2:]
    assert 1.0 < elapsed < 4, elapsed
    world.hermes.wait_cancelled("dribble")
    (row,) = _wait_audit(world, "agent.relay.turn", 1)
    assert row["detail"]["outcome"] == "timeout_total"


@pytest.mark.parametrize("mode", ["stall", "nohead"])
def test_a_client_disconnect_cancels_the_upstream_request(world, mode):
    """`stall`: Raven leaves mid-stream. `nohead`: Raven leaves while the agent is still
    thinking and has sent nothing (a non-streamed turn), the case a response-only disconnect
    check would miss. Default timeouts: only the disconnect can end these."""
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "h")
    with socket.create_connection((world.pod_ip, world.port)) as s:
        body = json.dumps({"model": "h", "stream": mode == "stall",
                           "messages": [{"role": "user", "content": mode}]}).encode()
        s.sendall((f"POST {world.relay_path('h')} HTTP/1.1\r\nHost: cp\r\n"
                   f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        end = time.time() + 5
        while world.hermes.active == 0 and time.time() < end:
            time.sleep(0.02)
        assert world.hermes.active == 1, "the turn never reached the agent"
        if mode == "stall":
            assert b"partial" in _read_until(s, b"partial")
    took = world.hermes.wait_cancelled(mode, timeout=5)
    assert took < 5
    (row,) = _wait_audit(world, "agent.relay.turn", 1)
    assert row["detail"]["outcome"] == "client_cancel"


# ================================================================ the per-token stream cap


def test_the_cap_limits_open_streams_per_token_and_is_not_a_quota(world, monkeypatch):
    monkeypatch.setenv("AGENT_RELAY_MAX_STREAMS", "2")
    token_a = world.create_raven("alice", "rv")
    world.hermes_child(token_a, "alice", "h")
    token_b = world.create_raven("bob", "brv")
    world.portal_hermes("bob", "bh")

    def open_stall(tok, target):
        s = socket.create_connection((world.pod_ip, world.port))
        body = json.dumps({"model": target, "stream": True,
                           "messages": [{"role": "user", "content": "stall"}]}).encode()
        s.sendall((f"POST {world.relay_path(target)} HTTP/1.1\r\nHost: cp\r\n"
                   f"Authorization: Bearer {tok}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        assert b"partial" in _read_until(s, b"partial")
        return s

    held = [open_stall(token_a, "h"), open_stall(token_a, "h")]
    capped = world.turn(token_a, "h", "reply:third")
    assert capped.status_code == 429, capped.text
    assert int(capped.headers["retry-after"]) > 0
    # Another token is unaffected while alice's two are open.
    assert world.turn(token_b, "bh", "reply:bob unaffected").status_code == 200

    # A gauge of open streams, not a count over time: close one and alice may go again, as
    # many times as she likes, sequentially.
    held.pop().close()
    world.hermes.wait_cancelled("stall")
    end = time.time() + 5
    while time.time() < end:
        r = world.turn(token_a, "h", "reply:again")
        if r.status_code == 200:
            break
        time.sleep(0.05)
    assert r.status_code == 200, r.text
    for _ in range(5):
        assert world.turn(token_a, "h", "reply:more").status_code == 200
    held.pop().close()

    rows = _wait_audit(world, "agent.relay.turn", 10)
    outcomes = [x["detail"]["outcome"] for x in rows]
    assert outcomes.count("capped") == 1 and outcomes.count("client_cancel") == 2, outcomes


def test_parallel_streams_up_to_the_cap_all_succeed(world, monkeypatch):
    monkeypatch.setenv("AGENT_RELAY_MAX_STREAMS", "3")
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "h")
    with ThreadPoolExecutor(3) as pool:
        codes = list(pool.map(lambda i: world.turn(token, "h", f"reply:n{i}").status_code, range(3)))
    assert codes == [200, 200, 200]


# ================================================================ the audit


def test_every_turn_writes_one_row_with_the_outcome_and_never_the_conversation(world):
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "h")
    secret_prompt = "reply:the-launch-code-is-4471"
    assert world.turn(token, "h", secret_prompt).status_code == 200
    # An upstream refusal (the agent's key rotated underneath) is an upstream_error turn.
    world.hermes.keys["agent-alice-h"] = "rotated"
    assert world.turn(token, "h", "reply:x").status_code == 401
    # An unreachable upstream (nothing listening where the Service points) is one too.
    world.hosts["agent-alice-h"] = "127.0.0.2"
    r = world.turn(token, "h", "reply:x")
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unreachable"

    rows = _wait_audit(world, "agent.relay.turn", 3)
    assert [(x["actor"], x["target"], x["detail"]["outcome"], x["detail"]["upstream_status"])
            for x in rows] == [
        ("agent-manager:alice/rv", "alice/h", "ok", 200),
        ("agent-manager:alice/rv", "alice/h", "upstream_error", 401),
        ("agent-manager:alice/rv", "alice/h", "upstream_error", None)]
    first = rows[0]["detail"]
    assert first["via"] == "agent-manager" and first["raven"] == "rv" and first["owner"] == "alice"
    assert first["bytes_in"] > 0 and first["bytes_out"] > 0 and first["duration_ms"] >= 0
    dump = json.dumps(world.audit(), default=str)
    assert "launch-code" not in dump and "4471" not in dump, "the audit holds conversation text"
    assert token not in dump


def test_a_client_that_hangs_up_after_done_completed_its_turn(world, monkeypatch):
    """Found live: an OpenAI client (the eaf-agents tool, like most) stops at `data: [DONE]`
    and closes while the agent still holds its end open. That turn delivered its whole
    reply, so it is `ok`, not `client_cancel`; a hang-up BEFORE the terminator still is."""
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "h")
    monkeypatch.setenv("EAF_AGENT_MANAGER_TOKEN", token)
    base = f"http://{world.pod_ip}:{world.port}/agent-manager/v1"
    assert eaf_agents_mcp.send_to_agent(base, "h", "linger:all of it") == "all of it"
    world.hermes.wait_cancelled("linger")
    (row,) = _wait_audit(world, "agent.relay.turn", 1)
    assert row["detail"]["outcome"] == "ok", row


def test_an_oversized_request_is_refused_and_audited(world, monkeypatch):
    monkeypatch.setenv("AGENT_RELAY_MAX_BODY", "2048")
    token = world.create_raven("alice", "rv")
    world.hermes_child(token, "alice", "h")
    r = world.turn(token, "h", "reply:" + "x" * 4096)
    assert r.status_code == 413
    assert world.hermes.seen == []
    (row,) = _wait_audit(world, "agent.relay.turn", 1)
    assert row["detail"]["outcome"] == "too_large"
    # And the slot was given back.
    assert world.turn(token, "h", "reply:small").status_code == 200


# ================================================================ openclaw targets (b12)


def test_a_ravens_turn_reaches_its_openclaw_child_as_the_owner_and_the_reply_streams_back(world):
    token = world.create_raven("alice", "rv")
    world.openclaw_child(token, "alice", "claw")

    # The Raven names the agent in `model` (as the tool does) and tries to forge the caller.
    r = world.turn(token, "claw", "reply:hello from claw", **{
        "X-Forwarded-User": "bob", "X-Openclaw-Scopes": "operator.admin",
        "X-Openclaw-Model": "gateway/expensive", "X-Hermes-Session-Key": "mine"})
    assert r.status_code == 200, r.text
    frames = _sse_frames(r.text)
    assert "".join(f["choices"][0]["delta"]["content"] for f in frames if f != "[DONE]") \
        == "hello from claw"

    (seen,) = world.hermes.seen
    h = seen["headers"]
    assert h["x-forwarded-user"] == "alice", "the identity is the OWNER's, not the request's"
    assert "authorization" not in h and token not in json.dumps(seen)
    assert h["x-openclaw-session-key"] == expected_session_key("alice", "rv", "claw")
    for smuggled in ("x-openclaw-scopes", "x-openclaw-model", "x-hermes-session-key"):
        assert smuggled not in h, smuggled
    assert seen["body"]["model"] == "openclaw"  # an agent target, not the Raven's `claw`
    assert seen["body"]["messages"] == [{"role": "user", "content": "reply:hello from claw"}]
    assert "x-hermes-session-id" not in {k.lower() for k in r.headers}

    j = world.turn(token, "claw", "reply:plain json", stream=False)
    assert j.status_code == 200 and j.json()["choices"][0]["message"]["content"] == "plain json"
    rows = _wait_audit(world, "agent.relay.turn", 2)
    assert {r["target"] for r in rows} == {"alice/claw"}
    assert {r["detail"]["outcome"] for r in rows} == {"ok"}
    assert "hello from claw" not in json.dumps(world.audit(), default=str)


def test_another_users_openclaw_agent_is_refused_and_never_contacted(world):
    """The owner tests for openclaw: alice's Raven cannot relay to bob's openclaw agent, by
    name, by hyphen collision or by a forged identity, and bob's own Raven can."""
    token_a = world.create_raven("alice", "rv")
    world.openclaw_child(token_a, "alice", "mine")
    world.portal_openclaw("bob", "bobs")
    token_b = world.create_raven("bob", "brv")

    r = world.turn(token_a, "bobs", "reply:should never arrive")
    assert r.status_code == 404, r.text
    forged = world.turn(token_a, "bobs", "reply:x", **{
        "X-Auth-Request-Preferred-Username": "bob", "X-Forwarded-User": "bob"})
    assert forged.status_code == 404
    assert world.hermes.seen == [], "the owner guard refused it before any upstream contact"

    world.idp.users.append({"id": "kc-ab", "username": "alice-bot", "email": "ab@x", "enabled": True})
    sql(world.dsn, "INSERT INTO principal (idp_user_id, username, email, enabled) VALUES "
                   "('kc-ab','alice-bot','ab@x',TRUE)")
    world.portal_openclaw("alice-bot", "two")
    assert world.turn(token_a, "bot-two", "reply:crossed").status_code == 404
    assert world.hermes.seen == []

    ok = world.turn(token_b, "bobs", "reply:bob here")
    assert ok.status_code == 200 and "bob" in ok.text
    assert world.hermes.seen[-1]["headers"]["x-forwarded-user"] == "bob"
    assert [d["detail"]["status"] for d in world.audit("agent-manager.denied")] == [404, 404, 404]


def test_the_shipped_openclaw_seed_enables_the_endpoint_the_relay_drives():
    seed = json.loads(agents.openclaw_seed_config("alice", "claw", agents.DEFAULT_MODEL))
    assert seed["gateway"]["http"]["endpoints"]["chatCompletions"] == {"enabled": True}
    # ...and still authenticates by trusted-proxy identity for the owner alone.
    assert seed["gateway"]["auth"]["mode"] == "trusted-proxy"
    assert seed["gateway"]["auth"]["trustedProxy"]["allowUsers"] == ["alice"]


def test_a_stopped_openclaw_agent_and_a_non_object_body_are_refused(world):
    token = world.create_raven("alice", "rv")
    world.openclaw_child(token, "alice", "claw")
    assert world.pod(token).post("/agent-manager/v1/agents/claw/stop").status_code == 200
    world.cluster.store.pop(("pods", "agent-alice-claw-0"))
    r = world.turn(token, "claw", "reply:x")
    assert r.status_code == 404 and "not running" in r.text
    assert world.pod(token).post("/agent-manager/v1/agents/claw/start").status_code == 200
    world.run_pod("alice", "claw")
    bad = world.pod(token).post(world.relay_path("claw"), content=b"[1,2]",
                                headers={"Content-Type": "application/json"})
    assert bad.status_code == 400
    assert world.hermes.seen == []
    assert world.turn(token, "claw", "reply:awake").status_code == 200


def test_the_raven_side_tool_talks_to_an_openclaw_child_by_name(world, monkeypatch):
    token = world.create_raven("alice", "rv")
    base = f"http://{world.pod_ip}:{world.port}/agent-manager/v1"
    monkeypatch.setenv("EAF_AGENT_MANAGER_TOKEN", token)
    made = json.loads(eaf_agents_mcp.create_agent(base, "scout", "openclaw"))
    assert made["created"] == "scout" and made["type"] == "openclaw"
    world._wire_claw("alice", "scout")
    assert eaf_agents_mcp.send_to_agent(base, "scout", "reply:claw scouting done") == "claw scouting done"
