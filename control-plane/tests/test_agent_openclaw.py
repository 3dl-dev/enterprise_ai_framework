"""The openclaw Agents-pillar type (enterpriseaiframework-ff7): console proxy + model change.

The sibling of test_agent_gateway_console.py (hermes) and it reuses that suite's fake cluster
and ledger stubs. The upstream here is a FAKE OPENCLAW GATEWAY, and what makes it more than a
hand-written mock is where its rules come from:

  * Its auth policy (which users, which required headers) is READ FROM THE REAL SEED the
    provisioner produces (`agents.openclaw_seed_config`), so the fake and the shipped config
    cannot drift apart.
  * Its behaviour was recorded from a live ghcr.io/openclaw/openclaw 2026.9.6 gateway running
    exactly that seed: with `auth.mode: trusted-proxy`, a request with no client address is
    `403 proxy_attribution_required`; with `x-forwarded-user` it serves the Control UI under
    `controlUi.basePath`; `POST /api/v1/admin/rpc` `config.patch` without `baseHash` is
    `INVALID_REQUEST "config base hash required"`; and `config.get` returns `payload.hash`.
    The live run is the ground truth (see the item's REVIEW note); this fake replays it
    hermetically so the proxy's contract with it is checked on every `make test`.

The claims under test:

  * the owner reaches the Control UI and the proxy vouched for them with the trusted-proxy
    identity headers — no login round-trip, no cookie;
  * a browser cannot forge that identity: client-supplied `x-forwarded-user`/`-for`/
    `x-openclaw-scopes` are discarded and replaced;
  * a non-owner gets the same 404 as a non-existent agent, and the gateway never sees them;
  * the model is set through `config.get` + `config.patch` with the fresh base hash, the
    patch names the picked model as primary AND lists it (a custom provider only resolves ids
    it lists), and a refusal by the gateway is surfaced;
  * the WebSocket upgrade carries the identity and the browser's Origin (checked against
    `controlUi.allowedOrigins`).
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


def _seed_policy() -> dict:
    """The auth policy the shipped seed asks the gateway to enforce."""
    seed = json.loads(agents.openclaw_seed_config(OWNER, "claw", agents.DEFAULT_MODEL))
    return seed["gateway"]["auth"]["trustedProxy"]


class FakeOpenclaw:
    """A trusted-proxy openclaw gateway on the real Control UI port."""

    def __init__(self, address: str = "127.0.0.1"):
        policy = _seed_policy()
        self.allow_users = policy["allowUsers"]
        self.required = [h.lower() for h in policy["requiredHeaders"]]
        self.user_header = policy["userHeader"]
        self.address = address
        self.port = agents.OPENCLAW_PORT
        self.requests: list[dict] = []   # every request as the gateway saw it
        self.hash = "hash-1"
        self.config = {
            "agents": {"defaults": {"model": {"primary": f"gateway/{agents.DEFAULT_MODEL}"}}},
            "models": {"providers": {"gateway": {"models": []}}},
        }
        self.patches: list[dict] = []
        self.refuse_patch = False
        try:
            self.srv = ThreadingHTTPServer((address, self.port), self._handler())
        except OSError as exc:  # pragma: no cover - environment, not behaviour
            raise pytest.skip.Exception(
                f"cannot bind {address}:{self.port} for the fake openclaw gateway ({exc}). "
                "The console proxy dials the agent Service's real port."
            ) from exc
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _handler(gw):  # noqa: N805 - the closure is the handler's access to the gateway
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, body: dict):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self) -> bool:
                h = {k.lower(): v for k, v in self.headers.items()}
                gw.requests.append({"path": urlparse(self.path).path, "headers": h,
                                    "method": self.command})
                xff = h.get("x-forwarded-for", "")
                if not xff or xff.startswith("127.") or xff == "::1":
                    self._send(403, {"error": {"type": "proxy_attribution_required"}})
                    return False
                if any(r not in h for r in gw.required):
                    self._send(401, {"error": {"type": "unauthorized"}})
                    return False
                if h.get(gw.user_header) not in gw.allow_users:
                    self._send(401, {"error": {"type": "unauthorized"}})
                    return False
                return True

            def do_GET(self):
                if not self._authorised():
                    return
                h = gw.requests[-1]["headers"]
                self._send(200, {"path": gw.requests[-1]["path"],
                                 "user": h.get(gw.user_header),
                                 "prefix": h.get("x-forwarded-prefix", "")})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if not self._authorised():
                    return
                if urlparse(self.path).path != "/api/v1/admin/rpc":
                    self._send(404, {})
                    return
                call = json.loads(raw or b"{}")
                method, params = call.get("method"), call.get("params") or {}
                if method == "config.get":
                    self._send(200, {"ok": True, "payload": {
                        "hash": gw.hash, "parsed": gw.config}})
                elif method == "config.patch":
                    if params.get("baseHash") != gw.hash:
                        self._send(400, {"ok": False, "error": {
                            "code": "INVALID_REQUEST",
                            "message": "config base hash required; re-run config.get"}})
                    elif gw.refuse_patch:
                        self._send(400, {"ok": False, "error": {
                            "code": "INVALID_REQUEST", "message": "schema says no"}})
                    else:
                        gw.patches.append(json.loads(params["raw"]))
                        gw.hash = f"hash-{len(gw.patches) + 1}"
                        self._send(200, {"ok": True, "payload": {"ok": True, "hash": gw.hash}})
                else:
                    self._send(400, {"ok": False, "error": {"code": "INVALID_REQUEST",
                                                            "message": "method not allowed"}})

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
    monkeypatch.setattr(agents, "allowed_models",
                        lambda: (agents.DEFAULT_MODEL, "other-model"))

    gw = FakeOpenclaw()
    hosts: dict[str, str] = {}
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        key = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if key in hosts:
            return real_getaddrinfo(hosts[key], port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def add_agent(user: str, name: str):
        cluster.add_agent(user, name, agent_type="openclaw")
        obj = f"agent-{user}-{name}"
        cluster.put("secrets", {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": f"{obj}-key"},
            "data": {"OPENAI_API_KEY": base64.b64encode(b"sk-x").decode()},
        })
        hosts[obj] = gw.address

    world = type("World", (), {})()
    world.cluster, world.gw, world.add_agent, world.hosts = cluster, gw, add_agent, hosts
    try:
        yield world
    finally:
        gw.stop()
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


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------- console (Contract C)


def test_the_owner_reaches_the_control_ui_vouched_for_by_the_proxy(world):
    world.add_agent(OWNER, "claw")
    r = app_client(OWNER).get("/agents/claw/chat/main")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "/agents/claw/chat/main", (
        "openclaw mounts natively under controlUi.basePath, so the FULL path — prefix "
        "included — goes upstream; nothing is rewritten"
    )
    assert body["user"] == OWNER
    seen = world.gw.requests[-1]["headers"]
    assert seen["x-forwarded-prefix"] == "/agents/claw"
    assert seen["x-forwarded-proto"] and seen["x-forwarded-host"]
    assert "cookie" not in seen, "no login round-trip and no session cookie for openclaw"
    assert [r["method"] for r in world.gw.requests] == ["GET"], "exactly one upstream call"


def test_a_browser_cannot_forge_the_identity_the_gateway_trusts(world):
    """The proxy is the sole author of identity headers. A browser that sends its own is
    discarded: alice cannot become somebody else, cannot pick her own client address (a
    loopback one would be refused; a spoofed one would defeat the gateway's rate limit) and
    cannot widen her own scopes."""
    world.add_agent(OWNER, "claw")
    r = app_client(OWNER).get("/agents/claw/", headers={
        "x-forwarded-user": "mallory",
        "X-Forwarded-For": "127.0.0.1",
        "x-real-ip": "203.0.113.9",
        "Forwarded": "for=203.0.113.9",
        "x-openclaw-scopes": "operator.admin",
    })
    assert r.status_code == 200, r.text
    assert r.json()["user"] == OWNER
    seen = world.gw.requests[-1]["headers"]
    assert seen["x-forwarded-user"] == OWNER
    assert seen["x-forwarded-for"] == agent_gateway_console._PROXY_CLIENT_ADDR
    for forged in ("x-real-ip", "forwarded", "x-openclaw-scopes"):
        assert forged not in seen, f"{forged} from the browser reached the gateway"


def test_a_non_owner_gets_404_and_the_gateway_never_sees_them(world):
    world.add_agent(OWNER, "claw")
    r = app_client("mallory").get("/agents/claw/")
    assert r.status_code == 404, (
        "a 403 would confirm somebody owns an agent by that name — same as every console")
    assert world.gw.requests == [], "the request must be refused before any upstream call"


def test_the_gateway_refuses_a_request_the_proxy_did_not_vouch_for(world):
    """Guards the fake against being a rubber stamp: without the identity headers the
    recorded real behaviour is a refusal, so the passing tests above mean something."""
    import httpx

    r = httpx.get(f"http://127.0.0.1:{world.gw.port}/agents/claw/")
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "proxy_attribution_required"


def test_the_websocket_upgrade_carries_the_identity_and_the_browsers_origin(world):
    websockets = pytest.importorskip("websockets")
    seen: dict = {}

    async def handler(conn):
        req = getattr(conn, "request", None)
        headers = req.headers if req is not None else conn.request_headers
        seen["headers"] = {k.lower(): v for k, v in headers.items()}
        seen["path"] = req.path if req is not None else conn.path
        async for message in conn:
            await conn.send(f"echo:{message}")

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    ws_address = "127.0.0.2"     # same real port as the HTTP fake, on a second loopback IP

    async def serve():
        await websockets.serve(handler, ws_address, agents.OPENCLAW_PORT)
        ready.set()
        await asyncio.Future()

    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(serve())
        except (RuntimeError, OSError):
            pass

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(10), "the fake gateway's websocket server never started"

    world.add_agent(OWNER, "claw")
    world.hosts["agent-alice-claw"] = ws_address
    with app_client(OWNER).websocket_connect(
        "/agents/claw/", headers={"Origin": "https://ai.example.org",
                                  "x-forwarded-user": "mallory"},
    ) as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo:hello"
    assert seen["headers"]["x-forwarded-user"] == OWNER
    assert seen["headers"]["origin"] == "https://ai.example.org", (
        "the gateway checks the browser's Origin against controlUi.allowedOrigins")
    assert seen["headers"]["x-forwarded-for"] == agent_gateway_console._PROXY_CLIENT_ADDR
    assert seen["path"] == "/agents/claw/"

    with pytest.raises(Exception):
        with app_client("mallory").websocket_connect("/agents/claw/"):
            pass
    loop.call_soon_threadsafe(loop.stop)


# ---------------------------------------------------------------- model (Contract D)


def test_setting_the_model_patches_the_gateways_own_config_with_the_fresh_hash(world):
    world.add_agent(OWNER, "claw")
    r = app_client(OWNER).post("/portal/api/agents/claw/model", json={"model": "other-model"})
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "claw", "model": "other-model", "restarted": False}

    assert [q["path"] for q in world.gw.requests] == ["/api/v1/admin/rpc"] * 2
    patch = world.gw.patches[0]
    assert patch["agents"]["defaults"]["model"]["primary"] == "gateway/other-model"
    ids = [m["id"] for m in patch["models"]["providers"]["gateway"]["models"]]
    assert ids[0] == "other-model" and set(ids) == {"other-model", agents.DEFAULT_MODEL}, (
        "a custom provider only resolves model ids it lists, so the picked model must be "
        "registered in the same patch that makes it primary")
    assert set(patch) == {"agents", "models"}, "a targeted patch — never a whole-config write"
    assert world.gw.requests[-1]["headers"]["x-forwarded-user"] == OWNER
    assert ("alice", "agent.model.set", "alice/claw") in AUDIT


def test_an_unknown_model_never_reaches_the_gateway(world):
    world.add_agent(OWNER, "claw")
    r = app_client(OWNER).post("/portal/api/agents/claw/model", json={"model": "gpt-evil"})
    assert r.status_code == 400
    assert world.gw.requests == []


def test_a_gateway_that_refuses_the_patch_is_reported_not_swallowed(world):
    world.add_agent(OWNER, "claw")
    world.gw.refuse_patch = True
    r = app_client(OWNER).post("/portal/api/agents/claw/model", json={"model": "other-model"})
    assert r.status_code == 409
    assert "schema says no" in r.text
    assert world.gw.patches == []
    assert not any(a[1] == "agent.model.set" for a in AUDIT)


def test_a_non_owner_cannot_change_another_users_openclaw_model(world):
    world.add_agent(OWNER, "claw")
    r = app_client("mallory").post("/portal/api/agents/claw/model", json={"model": "other-model"})
    assert r.status_code == 404
    assert world.gw.requests == []


# ---------------------------------------------------------------- the seed itself


def test_the_seed_only_trusts_the_owner_and_never_embeds_the_key():
    cfg = json.loads(agents.openclaw_seed_config("alice", "claw", agents.DEFAULT_MODEL))
    dumped = json.dumps(cfg)
    assert "sk-" not in dumped and cfg["models"]["providers"]["gateway"]["apiKey"] == "${OPENAI_API_KEY}"
    tp = cfg["gateway"]["auth"]["trustedProxy"]
    assert tp["allowUsers"] == ["alice"]
    assert cfg["gateway"]["auth"]["identityScopes"] == {"alice": ["operator.admin"]}
    assert "token" not in cfg["gateway"]["auth"], "token and trusted-proxy are mutually exclusive"
    assert "dangerouslyAllowHostHeaderOriginFallback" not in cfg["gateway"]["controlUi"]


def test_the_seed_allows_only_the_portals_own_origin(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://ai.example.org/")
    cfg = json.loads(agents.openclaw_seed_config("alice", "claw", agents.DEFAULT_MODEL))
    assert cfg["gateway"]["controlUi"]["allowedOrigins"] == ["https://ai.example.org"]


# ---------------------------------------------------------------- the pod's init script


def _init_script() -> str:
    docs = agents.render_openclaw(
        "alice", "claw", image="img", model_source="integrated",
        key_secret="agent-alice-claw-key", cfgsum="c", keysum="k")
    dep = next(d for d in docs if d["kind"] == "Deployment")
    init = dep["spec"]["template"]["spec"]["initContainers"][0]
    assert init["name"] == "config-seed"
    assert init["command"][:2] == ["sh", "-c"]
    return init["command"][2]


def test_the_init_script_seeds_once_and_leaves_a_config_the_gateway_can_rewrite(tmp_path):
    """Runs the RENDERED init script for real, against a read-only-mode seed.

    Two defects were found by running openclaw on k3s, and both are about this script:
    the ConfigMap volume is 0444 and `cp` keeps that mode (the gateway then cannot write its
    own config back), and a root-owned state directory makes the gateway's `fchmod` fail
    EPERM. The seed's mode is the fault injected here (a real 0444 file, exactly what a
    ConfigMap volume presents), and the ownership step is asserted through a PATH shim for
    `chown` - this test runs unprivileged, so the real chown is proven live instead (item
    evidence: config.patch failed EPERM before, applied and persisted across a pod restart
    after).
    """
    import os
    import stat
    import subprocess

    state, cfgdir, seed = tmp_path / "state", tmp_path / "cfg", tmp_path / "seed"
    for d in (state, cfgdir, seed):
        d.mkdir()
    (seed / "openclaw.json").write_text('{"seeded": true}')
    os.chmod(seed / "openclaw.json", 0o444)
    shim = tmp_path / "bin"
    shim.mkdir()
    chown_log = tmp_path / "chown.log"
    (shim / "chown").write_text(f'#!/bin/sh\necho "$@" >> {chown_log}\n')
    os.chmod(shim / "chown", 0o755)

    script = (_init_script()
              .replace("/home/node/.config/openclaw", str(cfgdir))
              .replace("/home/node/.openclaw", str(state))
              .replace("/seed/", f"{seed}/"))
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"}

    def run():
        return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)

    first = run()
    assert first.returncode == 0, first.stderr
    cfg = state / "openclaw.json"
    assert cfg.read_text() == '{"seeded": true}'
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600, (
        "a 0444 seed copied as-is leaves the gateway unable to write its own config")
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(cfgdir.stat().st_mode) == 0o700
    chowned = chown_log.read_text().split()
    assert chowned[0] == "1000:1000" and {str(state), str(cfgdir), str(cfg)} <= set(chowned), (
        "the state dirs and config must be handed to uid 1000 (the gateway's user)")

    # The agent (or its Control UI) changes a setting; a restart must not put the seed back.
    cfg.write_text('{"seeded": false, "changed_by_agent": true}')
    second = run()
    assert second.returncode == 0, second.stderr
    assert cfg.read_text() == '{"seeded": false, "changed_by_agent": true}', (
        "the seed is FIRST-BOOT-ONLY (Contract B); it clobbered the agent's own config")
