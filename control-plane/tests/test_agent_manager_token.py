"""Contract F: the owner-scoped agent-manager token (agents-raven.md), as a security control.

WHY THIS IS A SECURITY TEST

The token is a bearer credential with agent LIFECYCLE authority: behind `/agent-manager/v1/*`
the control plane holds create/patch/delete on every Deployment, Secret and PVC in the
namespace, and Kubernetes cannot narrow that per user. What stands between a Raven and every
other user's agents is (a) the token record naming exactly one owner and one Raven, (b) the
same `agents._owned_deployment` owner guard the portal relies on, and (c) the Raven-specific
refusals in `agents.py`. Every case below is somebody trying to cross one of those.

WHAT IS REAL HERE

  * THE REAL CONTROL-PLANE APP — `app.main.app`, lifespan and all, served by a real uvicorn
    on a real socket with `proxy_headers=False` (the Dockerfile's `--no-proxy-headers`).
    The loopback fence reads the TCP peer, so it is exercised with real TCP peers: the
    portal door is reached over 127.0.0.1 exactly as the oauth2-proxy sidecar reaches it,
    and the agent-manager door over this host's non-loopback address, as a pod IP would.
    Nothing sets `request.client` by hand.
  * A REAL POSTGRES (disposable container). The token table, its partial unique index, the
    hash-only storage, revocation, the principal-enabled re-check and the hash-chained audit
    trail are all SQL; a stubbed pool would be asserting the thing under test.
  * `app.agents` in full, unpatched: the owner guard, create/scale/set_model/delete, the
    Raven refusals, the created-by stamp, the provisioners and the real templates.

TEST DOUBLES, NAMED AS SUCH (all are separate systems reached over real HTTP; none is code
under test, and nothing in app/ is monkeypatched except the addresses and file locations a
pod would get from its environment):

  * the Kubernetes API server — `FakeCluster` from test_portal_agents.py (a real HTTP
    object store the shipped client talks to), extended here with LiteLLM's two key
    endpoints (/key/generate, /key/delete) so `issuance.issue` runs for real;
  * the openclaw gateway — `FakeOpenclaw` from test_agent_openclaw.py, whose behaviour was
    recorded from a live openclaw 2026.9.6 (see that file), so set-model runs end to end;
  * Keycloak's admin API — two endpoints, for the `/admin/sync` disable path.

The claims a fake cluster cannot support (a real Raven pod receiving the token as env and
using it from its pod IP; a real oauth2-proxy forwarding it over loopback) are proven live on
k3s against a throwaway control-plane pod: see the item's live verification record.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from test_portal_agents import _SA_DIR, FakeCluster  # noqa: E402 - sets up sys.path
from test_agent_openclaw import FakeOpenclaw  # noqa: E402

import uvicorn  # noqa: E402

from app import agent_usage, agents, db, freerouter, main  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MODELS = (agents.DEFAULT_MODEL, "other-model")


# ---------------------------------------------------------------- the real database


def _real_asyncpg():
    """The installed asyncpg, even if an earlier test file put a stub in sys.modules."""
    prev = sys.modules.get("asyncpg")
    if prev is not None and hasattr(prev, "connect"):
        return prev
    sys.modules.pop("asyncpg", None)
    try:
        return importlib.import_module("asyncpg")
    finally:
        if prev is not None:
            sys.modules["asyncpg"] = prev
        else:
            sys.modules.pop("asyncpg", None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def postgres():
    """A disposable Postgres. Absent docker is a FAILURE, not a skip: this suite is the
    proof of a security control, and a skipped proof is a missing one."""
    if not shutil.which("docker"):
        pytest.fail("docker is required: the token store is proven against real Postgres")
    port = _free_port()
    name = f"eaf72b-pg-{port}"
    r = subprocess.run([
        "docker", "run", "--rm", "-d", "--name", name,
        "-e", "POSTGRES_PASSWORD=eaitest", "-e", "POSTGRES_USER=eai", "-e", "POSTGRES_DB=eai",
        "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine",
    ], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        pytest.fail(f"could not start disposable postgres: {r.stderr}")
    try:
        deadline = time.time() + 90
        # TCP probe, not the unix socket: the image's init server listens on the socket only
        # (see test_metering_continuous_history.py for the flake that taught this).
        while time.time() < deadline:
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "eai",
                               "-h", "127.0.0.1"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("disposable postgres never became ready")
        yield f"postgresql://eai:eaitest@127.0.0.1:{port}/eai"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def sql(dsn: str, query: str, *args):
    apg = _real_asyncpg()

    async def go():
        conn = await apg.connect(dsn)
        try:
            if query.lstrip().upper().startswith("SELECT"):
                return await conn.fetch(query, *args)
            await conn.execute(query, *args)
            return []
        finally:
            await conn.close()

    return asyncio.run(go())


def sha256(text: str) -> str:
    # The independent statement of the storage contract: SHA-256 hex of the exact bearer.
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------- the other systems


class ClusterAndGateway(FakeCluster):
    """The fake API server, plus LiteLLM's key-minting endpoint on the same listener."""

    def __init__(self):
        self.minted: list[dict] = []
        super().__init__()

    def _handler(cluster):  # noqa: N805
        base = FakeCluster._handler(cluster)

        class Handler(base):
            def do_POST(self):
                if urlparse(self.path).path == "/key/generate":
                    body = self._body()
                    cluster.minted.append(body)
                    key = "sk-" + secrets.token_hex(12)
                    self._json(200, {"key": key, "token": sha256(key),
                                     "key_alias": body.get("key_alias")})
                    return
                super().do_POST()

        return Handler


class FakeKeycloak:
    """Keycloak's token endpoint and admin users list, for `/admin/sync`."""

    def __init__(self):
        self.users: list[dict] = []
        idp = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _json(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._json(200, {"access_token": "kc-admin"})

            def do_GET(self):
                first = int((urlparse(self.path).query.split("first=")[-1].split("&")[0])
                            if "first=" in self.path else 0)
                self._json(200, idp.users if first == 0 else [])

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def _pod_address() -> str:
    """This host's non-loopback address: what a pod IP is to the control plane."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        addr = s.getsockname()[0]
    if addr.startswith("127."):
        pytest.fail(f"no non-loopback address on this host (got {addr}); the pod-IP door "
                    "cannot be exercised without one")
    return addr


# ---------------------------------------------------------------- the world


class World:
    def __init__(self, dsn, cluster, gw, idp, port, hosts):
        self.dsn, self.cluster, self.gw, self.idp, self.port = dsn, cluster, gw, idp, port
        self.hosts = hosts
        self.pod_ip = _pod_address()

    # --- the two doors
    def portal(self, user: str) -> httpx.Client:
        """The portal as the oauth2-proxy sidecar reaches it: loopback + identity header."""
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30,
                            headers={"X-Auth-Request-Preferred-Username": user})

    def pod(self, token: str | None, **headers) -> httpx.Client:
        """The agent-manager API as a Raven reaches it: from a pod IP, with its bearer."""
        h = dict(headers)
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        return httpx.Client(base_url=f"http://{self.pod_ip}:{self.port}", timeout=30,
                            headers=h)

    def admin(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30,
                            headers={"Authorization": "Bearer admin-72b"})

    # --- state, read the way the pod and the operator would
    def secret_value(self, obj: str, key: str) -> str:
        data = self.cluster.get("secrets", f"{obj}-key")["data"]
        return base64.b64decode(data[key]).decode()

    def create_raven(self, user: str, name: str) -> str:
        """Create a Raven through the REAL portal route; return the token its pod receives."""
        r = self.portal(user).post("/portal/api/agents", json={"name": name, "type": "raven"})
        assert r.status_code == 201, r.text
        return self.secret_value(f"agent-{user}-{name}", "EAF_AGENT_MANAGER_TOKEN")

    def create_openclaw(self, user: str, name: str):
        r = self.portal(user).post("/portal/api/agents", json={"name": name, "type": "openclaw"})
        assert r.status_code == 201, r.text
        self.hosts[f"agent-{user}-{name}"] = self.gw.address

    def audit(self, action: str | None = None) -> list[dict]:
        rows = sql(self.dsn, "SELECT actor, action, target, detail FROM audit_event "
                             "ORDER BY seq")
        out = [{**dict(r), "detail": json.loads(r["detail"]) if isinstance(r["detail"], str)
                else r["detail"]} for r in rows]
        return [r for r in out if action is None or r["action"] == action]

    def tokens(self) -> list[dict]:
        return [dict(r) for r in sql(self.dsn, "SELECT * FROM agent_manager_token "
                                               "ORDER BY created_at")]

    def replicas(self, obj: str) -> int:
        return self.cluster.get("deployments", obj)["spec"]["replicas"]


@pytest.fixture()
def world(postgres, monkeypatch):
    # A clean schema per test; the app's own lifespan (db.init) creates it.
    sql(postgres, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    cluster = ClusterAndGateway()
    # Its own loopback address, so the fixed :18789 (the port the console proxy dials) never
    # collides with a sibling suite's fake on 127.0.0.1. A bind failure FAILS this suite —
    # FakeOpenclaw would otherwise skip it, and a skipped security proof is a missing one.
    try:
        gw = FakeOpenclaw(address="127.0.0.72")
    except pytest.skip.Exception as exc:
        pytest.fail(str(exc))
    idp = FakeKeycloak()
    hosts: dict[str, str] = {}
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *a, **k):
        key = host.decode() if isinstance(host, (bytes, bytearray)) else host
        return real_getaddrinfo(hosts.get(key, host), port, *a, **k)

    # Addresses and file locations only — what a pod gets from its environment.
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(agents, "KUBE_API", cluster.url)
    monkeypatch.setattr(agent_usage, "TOKEN_FILE", _SA_DIR / "token")
    monkeypatch.setattr(agent_usage, "CA_FILE", _SA_DIR / "ca.crt")
    monkeypatch.setattr(agent_usage, "NAMESPACE_FILE", _SA_DIR / "namespace")
    monkeypatch.setattr(db, "asyncpg", _real_asyncpg())
    monkeypatch.setattr(db, "_pool", None)
    # main.lifespan rewires this module global; restore it for the rest of the suite.
    monkeypatch.setattr(freerouter, "alias_resolver", freerouter.alias_resolver)
    for k, v in {
        "CONTROL_PLANE_DATABASE_URL": postgres,
        "CONTROL_PLANE_ADMIN_TOKEN": "admin-72b",
        "GATEWAY_URL": cluster.url, "GATEWAY_MASTER_KEY": "sk-master-72b",
        "GATEWAY_PROVIDER": "litellm",
        "AGENT_MODELS": ",".join(MODELS), "CATALOG_URL": "http://127.0.0.1:9",
        "AGENT_USAGE_ENABLED": "0", "ANALYTICS_COLLECT_ENABLED": "0",
        "IDP_URL": idp.url, "IDP_CLIENT_SECRET": "kc-secret",
    }.items():
        monkeypatch.setenv(k, v)
    for k in ("CHAT_MONGO_URL", "FREEROUTER_URL"):
        monkeypatch.delenv(k, raising=False)

    # Two real principals, as /admin/sync or the lazy IdP lookup would have written them.
    for u in ("alice", "bob"):
        idp.users.append({"id": f"kc-{u}", "username": u, "email": f"{u}@x", "enabled": True})

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        main.app, host="0.0.0.0", port=port, proxy_headers=False, lifespan="on", loop="asyncio",
        log_level="warning", access_log=False))
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
        yield World(postgres, cluster, gw, idp, port, hosts)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        gw.stop()
        idp.stop()
        cluster.stop()


# ================================================================ issuance and storage


def test_raven_create_mints_one_hash_only_token_delivered_only_through_its_key_secret(world):
    alice = world.portal("alice")
    created = alice.post("/portal/api/agents", json={"name": "rv", "type": "raven"})
    assert created.status_code == 201, created.text
    token = world.secret_value("agent-alice-rv", "EAF_AGENT_MANAGER_TOKEN")

    # Form: eafam_ + 32 random bytes, base64url (43 chars unpadded).
    assert token.startswith("eafam_") and len(token) == len("eafam_") + 43, token
    assert token not in created.text, "the plaintext must never be returned to any person"
    assert token not in alice.get("/portal/api/agents").text

    rows = world.tokens()
    assert len(rows) == 1
    row = rows[0]
    assert (row["owner"], row["raven_name"], row["revoked_at"]) == ("alice", "rv", None)
    assert row["token_hash"] == sha256(token), "stored as SHA-256 of the bearer"
    dumped = json.dumps([dict(r) for r in sql(
        world.dsn, "SELECT row_to_json(t)::text AS j FROM agent_manager_token t")])
    assert token not in dumped and token[6:] not in dumped, "plaintext at rest in Postgres"
    audit_dump = json.dumps(world.audit(), default=str)
    assert token not in audit_dump, "plaintext in the audit trail"

    # The Raven container receives it as a secretKeyRef env, and nothing else carries it.
    dep = world.cluster.get("deployments", "agent-alice-rv")
    env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["EAF_AGENT_MANAGER_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "agent-alice-rv-key", "key": "EAF_AGENT_MANAGER_TOKEN"}
    cm = world.cluster.get("configmaps", "agent-alice-rv-config")
    assert token not in json.dumps(cm) and token not in json.dumps(dep)

    issued = world.audit("agent-manager.issue")
    assert [(a["actor"], a["target"]) for a in issued] == [("alice", "alice/rv")]

    # Only a raven mints one.
    world.create_openclaw("alice", "claw")
    assert len(world.tokens()) == 1
    assert "EAF_AGENT_MANAGER_TOKEN" not in world.cluster.get(
        "secrets", "agent-alice-claw-key")["data"]


# ================================================================ the owner boundary


def test_user_a_token_is_404_or_403_on_every_user_b_agent_operation(world):
    token_a = world.create_raven("alice", "rv")
    world.create_raven("bob", "brv")
    world.create_openclaw("bob", "bclaw")
    before = {n: world.replicas(n) for n in ("agent-bob-brv", "agent-bob-bclaw")}

    a = world.pod(token_a)
    listed = a.get("/agent-manager/v1/agents")
    assert listed.status_code == 200, listed.text
    assert [x["name"] for x in listed.json()["agents"]] == ["rv"], "only alice's agents"

    probes = []
    for target in ("brv", "bclaw", "nosuch"):
        probes += [("POST", f"/agent-manager/v1/agents/{target}/stop", None),
                   ("POST", f"/agent-manager/v1/agents/{target}/start", None),
                   ("POST", f"/agent-manager/v1/agents/{target}/model", {"model": MODELS[1]}),
                   ("DELETE", f"/agent-manager/v1/agents/{target}", None)]
    for method, path, body in probes:
        r = a.request(method, path, json=body)
        assert r.status_code in (403, 404), f"{method} {path} -> {r.status_code} {r.text}"
        # 404 specifically: a distinct answer for "exists but not yours" would let a Raven
        # enumerate other users' agent names.
        assert r.status_code == 404, f"{method} {path} leaked existence: {r.status_code}"

    # Nothing of bob's moved, and the openclaw gateway never heard from alice's Raven.
    assert {n: world.replicas(n) for n in before} == before
    for obj in ("agent-bob-brv", "agent-bob-bclaw"):
        assert world.cluster.get("persistentvolumeclaims", obj) is not None
    assert world.gw.patches == []

    denied = world.audit("agent-manager.denied")
    assert len(denied) == len(probes)
    assert {d["actor"] for d in denied} == {"agent-manager:alice/rv"}
    assert all(d["detail"]["status"] == 404 for d in denied)


def test_the_owner_comes_only_from_the_token_never_from_headers_or_body(world):
    """SECOND MUTATION of the owner boundary: alice's token presented with every identity
    field a caller could set naming bob — the oauth2-proxy headers, a body `owner`/`user`."""
    token_a = world.create_raven("alice", "rv")
    world.create_openclaw("bob", "bclaw")
    forged = world.pod(token_a, **{
        "X-Auth-Request-Preferred-Username": "bob", "X-Forwarded-User": "bob",
        "X-Forwarded-Preferred-Username": "bob", "X-Forwarded-For": "127.0.0.1"})

    assert [x["name"] for x in forged.get("/agent-manager/v1/agents").json()["agents"]] == ["rv"]
    r = forged.post("/agent-manager/v1/agents/bclaw/stop", json={"owner": "bob", "user": "bob"})
    assert r.status_code == 404
    assert world.replicas("agent-bob-bclaw") == 1

    # A create carrying owner=bob lands as ALICE's agent, attributed to alice's Raven.
    made = forged.post("/agent-manager/v1/agents",
                       json={"name": "kid", "type": "openclaw", "owner": "bob", "user": "bob"})
    assert made.status_code == 201, made.text
    assert world.cluster.get("deployments", "agent-bob-kid") is None
    labels = world.cluster.get("deployments", "agent-alice-kid")["metadata"]["labels"]
    assert labels["agent.enterprise-ai/user"] == "alice"
    assert labels["agent.enterprise-ai/created-by"] == "rv"
    assert world.cluster.minted[-1]["key_alias"] == "alice::agents/kid", (
        "the child's key must be minted on the owner's own line")


# ================================================================ what the token may do


def test_lifecycle_through_the_token_is_real_and_every_call_is_audited_as_the_raven(world):
    token = world.create_raven("alice", "rv")
    world.create_openclaw("alice", "mine")          # the owner's own, not the Raven's
    m = world.pod(token)

    made = m.post("/agent-manager/v1/agents", json={"name": "kid", "type": "openclaw"})
    assert made.status_code == 201, made.text
    world.hosts["agent-alice-kid"] = world.gw.address
    dep = world.cluster.get("deployments", "agent-alice-kid")
    assert dep["metadata"]["labels"]["agent.enterprise-ai/created-by"] == "rv"
    assert dep["spec"]["template"]["metadata"]["labels"]["agent.enterprise-ai/created-by"] == "rv"
    assert "agent.enterprise-ai/created-by" not in dep["spec"]["selector"]["matchLabels"]

    for target in ("kid", "mine"):   # stop/start/set-model apply to ALL owned agents
        assert m.post(f"/agent-manager/v1/agents/{target}/stop").status_code == 200
        assert world.replicas(f"agent-alice-{target}") == 0
        assert m.post(f"/agent-manager/v1/agents/{target}/start").status_code == 200
        assert world.replicas(f"agent-alice-{target}") == 1
        r = m.post(f"/agent-manager/v1/agents/{target}/model", json={"model": MODELS[1]})
        assert r.status_code == 200, r.text
    assert len(world.gw.patches) == 2
    assert world.gw.patches[-1]["agents"]["defaults"]["model"]["primary"] == f"gateway/{MODELS[1]}"

    # The model allowlist is agents.py's, reached through this route too (no parallel path).
    bad = m.post("/agent-manager/v1/agents/kid/model", json={"model": "gpt-evil"})
    assert bad.status_code == 400
    badc = m.post("/agent-manager/v1/agents", json={"name": "k2", "model": "gpt-evil"})
    assert badc.status_code == 400 and world.cluster.get("deployments", "agent-alice-k2") is None

    gone = m.delete("/agent-manager/v1/agents/kid")
    assert gone.status_code == 200 and gone.json()["deleted"] is True
    assert world.cluster.get("deployments", "agent-alice-kid") is None

    actor = "agent-manager:alice/rv"
    by_action = {}
    for a in world.audit():
        if a["actor"] == actor:
            by_action.setdefault(a["action"], []).append(a)
    for action, n in (("agent.create", 1), ("agent.stop", 2), ("agent.start", 2),
                      ("agent.model.set", 2), ("agent.delete", 1), ("agent-manager.denied", 2)):
        assert len(by_action.get(action, [])) == n, (action, by_action.get(action))
    for a in by_action["agent.create"] + by_action["agent.delete"]:
        assert a["detail"]["via"] == "agent-manager"
        assert (a["detail"]["owner"], a["detail"]["raven"]) == ("alice", "rv")
    assert by_action["agent.create"][0]["target"] == "alice/kid"
    # The owner's own portal actions stay attributed to the owner.
    assert [a["actor"] for a in world.audit("agent.create")
            if a["target"] in ("alice/rv", "alice/mine")] == ["alice", "alice"]

    row = world.tokens()[0]
    assert row["last_used_at"] is not None


def test_a_raven_cannot_create_a_raven(world):
    token = world.create_raven("alice", "rv")
    m = world.pod(token)
    for t in ("raven", " raven "):       # second mutation: the whitespace a strip() eats
        r = m.post("/agent-manager/v1/agents", json={"name": "rv2", "type": t})
        assert r.status_code == 403, (t, r.status_code, r.text)
    assert world.cluster.get("deployments", "agent-alice-rv2") is None
    assert [t["raven_name"] for t in world.tokens()] == ["rv"], "no token minted a token"
    assert not any(m_["key_alias"] == "alice::agents/rv2" for m_ in world.cluster.minted)
    denied = world.audit("agent-manager.denied")
    assert [(d["actor"], d["detail"]["attempted"], d["detail"]["status"]) for d in denied] == [
        ("agent-manager:alice/rv", "agent.create", 403)] * 2


def test_a_raven_cannot_stop_start_remodel_or_delete_itself(world):
    token = world.create_raven("alice", "rv")
    m = world.pod(token)
    for method, path, body in (("POST", "/agent-manager/v1/agents/rv/stop", None),
                               ("POST", "/agent-manager/v1/agents/rv/start", None),
                               ("POST", "/agent-manager/v1/agents/rv/model",
                                {"model": MODELS[1]}),
                               ("DELETE", "/agent-manager/v1/agents/rv", None)):
        r = m.request(method, path, json=body)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
    assert world.replicas("agent-alice-rv") == 1
    assert world.cluster.get("persistentvolumeclaims", "agent-alice-rv") is not None
    assert world.tokens()[0]["revoked_at"] is None
    assert m.get("/agent-manager/v1/agents").status_code == 200, "still alive and working"
    assert len(world.audit("agent-manager.denied")) == 4


def test_delete_is_limited_to_the_ravens_own_created_by_children(world):
    token_rv = world.create_raven("alice", "rv")
    token_rv2 = world.create_raven("alice", "rv2")
    world.create_openclaw("alice", "mine")                      # no created-by at all
    assert world.pod(token_rv2).post(
        "/agent-manager/v1/agents", json={"name": "kid2", "type": "openclaw"}).status_code == 201
    m = world.pod(token_rv)

    # (1) an owned agent with no created-by; (2) a child created-by ANOTHER Raven of the
    # same owner (the relabel mutation); (3) another Raven of the same owner.
    for target in ("mine", "kid2", "rv2"):
        r = m.delete(f"/agent-manager/v1/agents/{target}")
        assert r.status_code == 403, (target, r.status_code, r.text)
        assert world.cluster.get("deployments", f"agent-alice-{target}") is not None
        assert world.cluster.get("persistentvolumeclaims", f"agent-alice-{target}") is not None
    denied = [d for d in world.audit("agent-manager.denied")
              if d["detail"].get("attempted") == "agent.delete"]
    assert [(d["actor"], d["target"], d["detail"]["status"]) for d in denied] == [
        ("agent-manager:alice/rv", f"alice/{t}", 403) for t in ("mine", "kid2", "rv2")]
    assert not world.audit("agent.delete"), "nothing was deleted"

    # A hand-edited created-by (a label the owner could set with kubectl) is honoured only
    # when it names THIS Raven — proving the check reads the label, not a list.
    world.cluster.get("deployments", "agent-alice-kid2")["metadata"]["labels"][
        "agent.enterprise-ai/created-by"] = "rv"
    assert m.delete("/agent-manager/v1/agents/kid2").status_code == 200
    # ...and rv2 can no longer delete what it created once the label is not its own.
    assert world.pod(token_rv2).delete("/agent-manager/v1/agents/mine").status_code == 403


# ================================================================ revocation


def test_the_token_dies_with_its_raven_and_a_replayed_hash_or_old_token_never_works(world):
    token = world.create_raven("alice", "rv")
    m = world.pod(token)
    assert m.get("/agent-manager/v1/agents").status_code == 200

    assert world.portal("alice").delete("/portal/api/agents/rv").status_code == 200
    row = world.tokens()[0]
    assert row["revoked_at"] is not None, "revoked in the same call as the virtual key"
    rev = world.audit("agent-manager.revoke")
    assert [(r["actor"], r["target"], r["detail"]["reason"]) for r in rev] == [
        ("alice", "alice/rv", "raven_deleted")]

    assert m.get("/agent-manager/v1/agents").status_code == 401
    # SECOND MUTATION: the stored hash itself, replayed as a bearer.
    assert world.pod(row["token_hash"]).get("/agent-manager/v1/agents").status_code == 401
    # THIRD: the Raven re-created under the same name gets a NEW token; the old stays dead.
    token2 = world.create_raven("alice", "rv")
    assert token2 != token
    assert world.pod(token2).get("/agent-manager/v1/agents").status_code == 200
    assert m.get("/agent-manager/v1/agents").status_code == 401
    live = [t for t in world.tokens() if t["revoked_at"] is None]
    assert [t["token_hash"] for t in live] == [sha256(token2)], "exactly one live token"

    unknown = [d for d in world.audit("agent-manager.denied")
               if d["actor"] == "agent-manager:unknown"]
    assert len(unknown) == 3 and {d["detail"]["status"] for d in unknown} == {401}
    assert unknown[0]["detail"]["hash_prefix"] == sha256(token)[:12]


def test_a_token_whose_raven_vanished_or_changed_type_is_dead_even_unrevoked(world):
    """The verifier's re-check, for when revocation 'somehow failed'. Driven through the
    cluster's own objects — the input the verifier actually reads — not its code."""
    token = world.create_raven("alice", "rv")
    m = world.pod(token)
    dep = world.cluster.get("deployments", "agent-alice-rv")
    dep["metadata"]["labels"]["agent.enterprise-ai/type"] = "hermes"      # relabelled
    assert m.get("/agent-manager/v1/agents").status_code == 401
    dep["metadata"]["labels"]["agent.enterprise-ai/type"] = "raven"
    assert m.get("/agent-manager/v1/agents").status_code == 200
    dep["metadata"]["labels"]["agent.enterprise-ai/user"] = "bob"         # re-owned
    assert m.get("/agent-manager/v1/agents").status_code == 401
    dep["metadata"]["labels"]["agent.enterprise-ai/user"] = "alice"
    world.cluster.store.pop(("deployments", "agent-alice-rv"))            # vanished
    assert m.get("/agent-manager/v1/agents").status_code == 401
    assert world.tokens()[0]["revoked_at"] is None, "dead by re-check, not by revocation"
    gone = [d for d in world.audit("agent-manager.denied")
            if d["detail"].get("reason") == "raven_gone"]
    assert len(gone) == 3 and {d["actor"] for d in gone} == {"agent-manager:alice/rv"}


def test_idp_disable_revokes_the_owners_tokens_and_re_enable_does_not_resurrect(world):
    token_a = world.create_raven("alice", "rv")
    token_b = world.create_raven("bob", "brv")

    world.idp.users[0]["enabled"] = False                 # alice disabled in Keycloak
    r = world.admin().post("/admin/sync")
    assert r.status_code == 200, r.text
    assert world.pod(token_a).get("/agent-manager/v1/agents").status_code == 401
    assert world.pod(token_b).get("/agent-manager/v1/agents").status_code == 200
    rev = [x for x in world.audit("agent-manager.revoke")
           if x["detail"].get("reason") == "disabled_in_idp"]
    assert [(x["actor"], x["target"], x["detail"]["count"]) for x in rev] == [
        ("system", "alice", 1)]

    world.idp.users[0]["enabled"] = True
    assert world.admin().post("/admin/sync").status_code == 200
    assert world.pod(token_a).get("/agent-manager/v1/agents").status_code == 401, (
        "revocation is stored state; re-enabling must not resurrect the token")


def test_a_disable_between_syncs_is_refused_by_the_verifier(world):
    token = world.create_raven("alice", "rv")
    sql(world.dsn, "UPDATE principal SET enabled = FALSE WHERE username = 'alice'")
    assert world.pod(token).get("/agent-manager/v1/agents").status_code == 401
    # SECOND MUTATION: a duplicate principal row for the same name that IS enabled must not
    # mask the disabled one (username is not unique; idp_user_id is).
    sql(world.dsn, "INSERT INTO principal (idp_user_id, username, enabled) "
                   "VALUES ('kc-alice-2', 'alice', TRUE)")
    assert world.pod(token).get("/agent-manager/v1/agents").status_code == 401
    sql(world.dsn, "UPDATE principal SET enabled = TRUE WHERE username = 'alice'")
    assert world.pod(token).get("/agent-manager/v1/agents").status_code == 200
    reasons = [d["detail"]["reason"] for d in world.audit("agent-manager.denied")]
    assert reasons == ["owner_disabled", "owner_disabled"]


# ================================================================ the loopback fence


def test_a_valid_token_arriving_over_loopback_is_refused_before_the_bearer_is_read(world):
    token = world.create_raven("alice", "rv")
    world.create_openclaw("alice", "mine")
    assert world.pod(token).get("/agent-manager/v1/agents").status_code == 200  # control

    # Exactly the request oauth2-proxy makes: loopback peer, its identity headers, and the
    # bearer a person replayed through their browser session.
    via_proxy = httpx.Client(base_url=f"http://127.0.0.1:{world.port}", timeout=30, headers={
        "Authorization": f"Bearer {token}",
        "X-Auth-Request-Preferred-Username": "alice", "X-Forwarded-User": "alice",
        # a proxy-supplied client address must not un-loopback the TCP peer
        "X-Forwarded-For": world.pod_ip, "X-Real-IP": world.pod_ip})
    for method, path in (("GET", "/agent-manager/v1/agents"),
                         ("POST", "/agent-manager/v1/agents/mine/stop"),
                         ("DELETE", "/agent-manager/v1/agents/mine")):
        r = via_proxy.request(method, path)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
    assert world.replicas("agent-alice-mine") == 1

    # SECOND MUTATION: a different loopback address (127.0.0.2, which the portal would not
    # even accept) is refused too — the fence is "any loopback", not one string.
    t = httpx.HTTPTransport(local_address="127.0.0.2")
    other_lo = httpx.Client(transport=t, base_url=f"http://127.0.0.1:{world.port}",
                            timeout=30, headers={"Authorization": f"Bearer {token}"})
    assert other_lo.get("/agent-manager/v1/agents").status_code == 403

    denied = [d for d in world.audit("agent-manager.denied")
              if d["detail"].get("reason") == "loopback_origin"]
    assert len(denied) == 4
    assert {d["detail"]["peer"] for d in denied} == {"127.0.0.1", "127.0.0.2"}
    assert {d["detail"]["hash_prefix"] for d in denied} == {sha256(token)[:12]}, (
        "the refusal must be of THIS token, for its origin — not a missing-bearer 401")
    assert world.tokens()[0]["last_used_at"] is not None  # from the control call only
    # And the converse door: the portal refuses the pod IP, so no request satisfies both.
    assert world.pod(None, **{"X-Auth-Request-Preferred-Username": "alice"}).get(
        "/portal/api/agents").status_code == 403


def test_missing_or_unknown_bearers_are_401_and_audited_without_an_owner(world):
    world.create_raven("alice", "rv")
    assert world.pod(None).get("/agent-manager/v1/agents").status_code == 401
    assert world.pod("eafam_" + "A" * 43).get("/agent-manager/v1/agents").status_code == 401
    unknown = world.audit("agent-manager.denied")
    assert [(d["actor"], d["detail"]["reason"]) for d in unknown] == [
        ("agent-manager:unknown", "no_token"), ("agent-manager:unknown", "unknown_or_revoked")]


def test_only_the_lifecycle_routes_exist_behind_the_token(world):
    """Scope is the router: connectors, keys, BYO, admin and portal are not reachable."""
    token = world.create_raven("alice", "rv")
    world.create_openclaw("alice", "mine")
    m = world.pod(token)
    for method, path in (("POST", "/agent-manager/v1/agents/mine/connectors"),
                         ("POST", "/agent-manager/v1/keys/rotate"),
                         ("GET", "/agent-manager/v1/agents/mine"),
                         ("POST", "/admin/sync"), ("GET", "/portal/api/agents")):
        r = m.request(method, path, json={})
        assert r.status_code in (401, 403, 404, 405), (method, path, r.status_code)
        assert r.status_code != 200


def test_uvicorn_runs_without_proxy_headers():
    """The loopback fence is sound only if request.client is the TCP peer."""
    cmd = (ROOT / "Dockerfile").read_text()
    assert '"--no-proxy-headers"' in cmd.split("CMD", 1)[1], (
        "without --no-proxy-headers uvicorn rewrites request.client from X-Forwarded-For "
        "sent by the loopback sidecar, and both doors read the wrong peer")
