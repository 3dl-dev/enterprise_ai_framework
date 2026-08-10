"""The Agents tab's isolation boundary, and the three tabs beside it.

WHY THIS FILE IS A SECURITY TEST AND NOT A FEATURE TEST

`/portal/api/agents` is the first portal endpoint that CREATES INFRASTRUCTURE. Behind it
the control plane holds create/get/list/patch/delete on Deployments, Services, PVCs,
Secrets and ConfigMaps in `enterprise-ai` (deploy/k8s/39-control-plane-rbac.yaml). RBAC
cannot narrow that per user — Kubernetes has no notion of "only objects whose owner label
matches the caller" — so within the namespace the account really can delete anybody's
agent and read anybody's Secret.

That means the owner scoping in `app/agents.py` is the ENTIRE boundary between one user
and another, and every case below is somebody trying to cross it. If these tests are ever
"simplified" into checking that a user can list their own agents, the control is gone and
nothing will say so.

WHAT IS REAL HERE AND WHAT IS NOT

Real, exercised as shipped:
  * `app.agents` in full — name derivation, the owner-label check, the template render,
    the object set it applies, the delete order.
  * `app.portal`'s four agent endpoints and `require_user`, reached over loopback with the
    identity header oauth2-proxy sets. Identity is never injected past the predicate.
  * `deploy/k8s/64-agent.template.yaml` — the real template, read from this checkout, so a
    placeholder this module forgets to substitute fails here rather than on a cluster.

A test double, and named as one: the Kubernetes API server is a real HTTP server in this
process holding a real object store, at a real address the shipped client is pointed at.
It is a stand-in for the CLUSTER, not for the code under test — nothing in `app/agents.py`
is patched, and the requests it receives are the requests k3s receives. The ledger
(Postgres, the gateway's key API) is stubbed for the same reason
`tests/test_portal_auth.py` stubs it: those are separate systems with their own tests, and
a test that needs Postgres to prove an authorisation check is a test nobody runs.

The claims a fake apiserver CANNOT support — that a created agent really reaches Running,
that stop really freezes the -914 meter, that delete really removes every object — are
proven against live k3s in tests-live/test_portal_agents.py, including the cross-user
refusal, with a second real identity.
"""

import base64
import json
import os
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

# The in-cluster credential: a REAL file at a real path, because the shipped code opens it
# on every single call (projected tokens rotate, so nothing caches one). Only its LOCATION
# is redirected, in the fixture — never `_token()` itself, which is the code that has to
# keep working when the kubelet rewrites the file underneath it.
_SA_DIR = Path(tempfile.mkdtemp(prefix="fake-sa-"))
(_SA_DIR / "token").write_text("fake-service-account-token")
(_SA_DIR / "namespace").write_text("enterprise-ai")
os.environ.setdefault("GATEWAY_MASTER_KEY", "sk-fake-master")

# The DRIVER is the shell, not the module. Same pattern as
# control-plane/tests/test_agents_alias.py: the test venv carries no database driver
# because it exists to prove behaviour rather than host a database, and nothing here opens
# a connection.
#
# Stubbing `app.db` / `app.issuance` themselves — the shape tests/test_portal_auth.py uses,
# which is right for a file that only exercises a header check — would mutate state shared
# with every other test in this directory. It did: replacing `app.issuance.issue` at import
# turned control-plane/tests/test_agents_alias.py red. So EVERY app module below is the real
# one, and only the two calls that would reach Postgres are replaced per test, in the
# fixture, and restored afterwards.
if "asyncpg" not in sys.modules:
    _pg = types.ModuleType("asyncpg")
    _pg.Pool = object

    async def _create_pool(*a, **kw):  # pragma: no cover - never reached
        raise RuntimeError("no database in this suite")

    _pg.create_pool = _create_pool
    sys.modules["asyncpg"] = _pg

from app import agent_usage, agents, db, issuance, metering, portal  # noqa: E402

NS = "enterprise-ai"

AUDIT: list[tuple] = []
ISSUED: list[tuple] = []


class _Conn:
    async def execute(self, *a, **k):
        return "UPDATE 0"

    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None


class _Acquire:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *a):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


def _stub_ledger(monkeypatch):
    """Postgres and the gateway's key API, which are not what this file is testing.

    Both have their own tests and both are exercised for real in
    tests-live/test_portal_agents.py. What is recorded here is WHAT WAS ASKED OF THEM —
    the audit actor and the issuance principal are assertions in their own right, because
    an agent minted for the wrong principal is a spendable key on somebody else's bill.
    """
    async def audit(actor, action, target=None, **detail):
        AUDIT.append((actor, action, target))
        return "hash"

    async def pool():
        return _Pool()

    async def issue(username, surface, *, actor):
        ISSUED.append((username, surface, actor))
        return {
            "username": username, "surface": surface,
            "key_alias": f"{username}::{surface}",
            "key": f"sk-fake-{username}-{surface}", "max_budget": None,
            "rotated": False,
        }

    async def spend_by_user_and_surface(since=None):
        return []

    monkeypatch.setattr(db, "audit", audit)
    monkeypatch.setattr(db, "pool", pool)
    monkeypatch.setattr(issuance, "issue", issue)
    monkeypatch.setattr(metering, "spend_by_user_and_surface", spend_by_user_and_surface)


# ---------------------------------------------------------------- the fake cluster


class FakeCluster:
    """A Kubernetes API server with an object store, over real HTTP.

    It implements only what `app/agents.py` actually calls — five collections, five verbs,
    label selectors, server-side apply — and it answers exactly as the API server does for
    the cases that matter: 404 on a missing object, `items` on a list, the stored object on
    a get. Anything the shipped code asks for that is not implemented raises here rather
    than returning an empty success, so a call that silently did nothing cannot pass.
    """

    def __init__(self):
        self.store: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.deny: set[str] = set()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # ---- the store, addressed the way a person reading kubectl output would

    def put(self, kind: str, obj: dict):
        self.store[(kind, obj["metadata"]["name"])] = obj

    def get(self, kind: str, name: str) -> dict | None:
        return self.store.get((kind, name))

    def names(self, kind: str) -> list[str]:
        return sorted(n for (k, n) in self.store if k == kind)

    def agent_deployment(self, user: str, name: str, *, replicas: int = 1) -> dict:
        """One agent's Deployment as the cluster would hold it, labels and all."""
        obj_name = f"agent-{user}-{name}"
        labels = {
            "app.kubernetes.io/component": "agent",
            "agent.enterprise-ai/user": user,
            "agent.enterprise-ai/name": name,
            "agent.enterprise-ai/model-source": "integrated",
        }
        return {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": obj_name, "namespace": NS, "labels": labels,
                         "creationTimestamp": "2026-08-10T00:00:00Z"},
            "spec": {"replicas": replicas},
        }

    def add_agent(self, user: str, name: str, *, replicas: int = 1, running: bool = True):
        dep = self.agent_deployment(user, name, replicas=replicas)
        self.put("deployments", dep)
        self.put("services", {"apiVersion": "v1", "kind": "Service",
                              "metadata": {"name": f"agent-{user}-{name}",
                                           "labels": dep["metadata"]["labels"]}})
        self.put("persistentvolumeclaims",
                 {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                  "metadata": {"name": f"agent-{user}-{name}",
                               "labels": dep["metadata"]["labels"]}})
        self.put("secrets", {"apiVersion": "v1", "kind": "Secret",
                             "metadata": {"name": f"agent-{user}-{name}-key",
                                          "labels": dep["metadata"]["labels"]}})
        if replicas and running:
            self.put("pods", {
                "apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": f"agent-{user}-{name}-abc",
                             "labels": dep["metadata"]["labels"], "uid": "uid-1"},
                "status": {"phase": "Running", "startTime": "2026-08-10T00:00:00Z",
                           "hostIP": "10.0.0.1"},
            })

    def add_workspace_pod(self, image="192.168.2.43:30500/enterprise-ai-workspace:test"):
        self.put("pods", {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": "ws-someone-1",
                         "labels": {"app.kubernetes.io/component": "workspace"}},
            "spec": {"containers": [{"name": "ttyd", "image": image}]},
            "status": {"phase": "Running"},
        })

    # ---- the HTTP surface

    def _handler(cluster):  # noqa: N805 - the closure IS the handler's access to the store
        COLLECTIONS = {
            "pods": "pods", "services": "services", "secrets": "secrets",
            "configmaps": "configmaps",
            "persistentvolumeclaims": "persistentvolumeclaims",
            "deployments": "deployments",
        }

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _split(self):
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                # /api/v1/namespaces/<ns>/<collection>[/<name>]
                # /apis/apps/v1/namespaces/<ns>/<collection>[/<name>]
                idx = parts.index("namespaces")
                collection = parts[idx + 2]
                name = parts[idx + 3] if len(parts) > idx + 3 else None
                query = parse_qs(parsed.query)
                return COLLECTIONS[collection], name, query

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def _json(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self, verb, collection) -> bool:
                # The API server checks the bearer token before anything else, and a
                # missing RBAC verb is a 403 with a message. Modelled so the shipped
                # code's 403 handling is exercised rather than assumed.
                if not self.headers.get("Authorization", "").startswith("Bearer "):
                    self._json(401, {"message": "Unauthorized"})
                    return False
                if f"{verb}:{collection}" in cluster.deny:
                    self._json(403, {"message": f"cannot {verb} {collection}"})
                    return False
                return True

            def do_GET(self):
                collection, name, query = self._split()
                cluster.calls.append(("get", collection))
                if not self._authorised("get", collection):
                    return
                if name:
                    obj = cluster.get(collection, name)
                    if obj is None:
                        self._json(404, {"message": f"{collection} {name} not found"})
                        return
                    self._json(200, obj)
                    return
                selector = (query.get("labelSelector") or [""])[0]
                wanted = dict(
                    pair.split("=", 1) for pair in selector.split(",") if "=" in pair
                )
                items = [
                    o for (k, _n), o in sorted(cluster.store.items())
                    if k == collection and all(
                        (o["metadata"].get("labels") or {}).get(lk) == lv
                        for lk, lv in wanted.items()
                    )
                ]
                self._json(200, {"items": items})

            def do_PATCH(self):
                collection, name, _ = self._split()
                cluster.calls.append(("patch", collection))
                if not self._authorised("patch", collection):
                    return
                body = self._body()
                ctype = self.headers.get("Content-Type", "")
                current = cluster.get(collection, name)
                if "apply-patch" in ctype:
                    # Server-side apply: the sent object IS the object.
                    cluster.put(collection, body)
                    self._json(200, body)
                    return
                if current is None:
                    self._json(404, {"message": f"{collection} {name} not found"})
                    return
                # Strategic merge, one level deep, which is all `{"spec": {...}}` needs.
                for key, value in body.items():
                    if isinstance(value, dict):
                        current.setdefault(key, {}).update(value)
                    else:
                        current[key] = value
                cluster.put(collection, current)
                self._json(200, current)

            def do_DELETE(self):
                collection, name, _ = self._split()
                cluster.calls.append(("delete", collection))
                if not self._authorised("delete", collection):
                    return
                if cluster.store.pop((collection, name), None) is None:
                    self._json(404, {"message": f"{collection} {name} not found"})
                    return
                # A deleted Deployment takes its pods with it, the way a controller would.
                if collection == "deployments":
                    for key in [k for k in cluster.store
                                if k[0] == "pods" and k[1].startswith(name + "-")]:
                        cluster.store.pop(key, None)
                self._json(200, {"status": "Success"})

            def do_POST(self):
                # The gateway's key API, hosted here so `gateway.delete_by_aliases` in the
                # delete path talks to something real rather than being patched out.
                if urlparse(self.path).path == "/key/delete":
                    self._json(200, {"deleted_keys": self._body().get("key_aliases", [])})
                    return
                self._json(405, {"message": "not implemented by the fake cluster"})

            def log_message(self, *a):
                pass

        return Handler


@pytest.fixture()
def cluster(monkeypatch):
    _stub_ledger(monkeypatch)
    c = FakeCluster()
    # Address and credential location, both of which a pod gets from its environment. This
    # is the same substitution KUBERNETES_SERVICE_HOST and the projected-token mount
    # perform in the cluster; no behaviour of app.agents or app.agent_usage is replaced.
    monkeypatch.setattr(agents, "KUBE_API", c.url)
    monkeypatch.setattr(agent_usage, "TOKEN_FILE", _SA_DIR / "token")
    monkeypatch.setattr(agent_usage, "CA_FILE", _SA_DIR / "ca.crt")
    monkeypatch.setattr(agent_usage, "NAMESPACE_FILE", _SA_DIR / "namespace")
    monkeypatch.setenv("GATEWAY_URL", c.url)
    AUDIT.clear()
    ISSUED.clear()
    try:
        yield c
    finally:
        c.stop()


def client_as(user: str) -> TestClient:
    """The portal, reached the way the oauth2-proxy sidecar reaches it.

    Loopback peer address and the sidecar's identity header — the shipped `require_user`
    derives the name, so no test here can hand the endpoints an identity they did not
    authenticate.
    """
    api = FastAPI()
    api.include_router(portal.router)
    c = TestClient(api, client=("127.0.0.1", 41000), raise_server_exceptions=False)
    c.headers.update({"X-Auth-Request-Preferred-Username": user})
    return c


# ---------------------------------------------------------------- allow


def test_a_user_lists_stops_starts_and_deletes_their_own_agent(cluster):
    """The whole lifecycle for the owner, so the refusals below mean something.

    A test that only proves the refusals passes just as well against an endpoint that
    refuses everybody, which is the failure mode that would take the surface down without
    anything going red.
    """
    cluster.add_agent("alice", "scraper")
    alice = client_as("alice")

    listed = alice.get("/portal/api/agents").json()
    assert [a["name"] for a in listed["agents"]] == ["scraper"]
    row = listed["agents"][0]
    assert row["status"] == "running", row
    assert row["surface"] == "agents/scraper", "the join key to spend and usage"
    assert row["console_url"] == "/agents/scraper/", (
        "the console entry must not carry the owner in the path — Contract 1 resolves the "
        "owner from the session, and a path that named it would be guessable"
    )

    assert alice.post("/portal/api/agents/scraper/stop").status_code == 200
    assert cluster.get("deployments", "agent-alice-scraper")["spec"]["replicas"] == 0
    assert alice.get("/portal/api/agents").json()["agents"][0]["status"] == "stopped"
    assert cluster.get("persistentvolumeclaims", "agent-alice-scraper") is not None, (
        "stopping an agent must keep its volume — Contract 2 promises stop keeps state "
        "and only delete destroys it"
    )

    assert alice.post("/portal/api/agents/scraper/start").status_code == 200
    assert cluster.get("deployments", "agent-alice-scraper")["spec"]["replicas"] == 1

    body = alice.request("DELETE", "/portal/api/agents/scraper").json()
    assert body["deleted"] is True
    for kind in ("deployments", "services", "secrets", "persistentvolumeclaims"):
        assert cluster.names(kind) == [], f"{kind} survived the delete: {cluster.names(kind)}"
    assert body["key_revoked"] == "alice::agents/scraper", (
        "deleting the pod without revoking the key leaves a spendable credential at the "
        "gateway with nothing using it"
    )


def test_creating_an_agent_applies_the_real_template_with_every_placeholder_filled(cluster):
    cluster.add_workspace_pod(image="registry.invalid/enterprise-ai-workspace:xyz")
    created = client_as("alice").post("/portal/api/agents", json={"name": "helper"})
    assert created.status_code == 201, created.text

    dep = cluster.get("deployments", "agent-alice-helper")
    assert dep is not None, f"no Deployment was applied; store holds {list(cluster.store)}"
    rendered = json.dumps(dep) + json.dumps(cluster.get("services", "agent-alice-helper"))
    assert "__" not in rendered, (
        f"an unsubstituted placeholder reached the cluster: {rendered[:400]}"
    )
    labels = dep["metadata"]["labels"]
    assert labels["agent.enterprise-ai/user"] == "alice", (
        "the owner label is what every later authorisation check reads; if it is wrong "
        "here the agent belongs to nobody and the guard cannot work"
    )
    assert labels["agent.enterprise-ai/name"] == "helper"
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "registry.invalid/enterprise-ai-workspace:xyz", (
        "an agent runs the image the Code surface is actually running, read off a live "
        "workspace pod rather than computed from a tag"
    )
    secret = cluster.get("secrets", "agent-alice-helper-key")
    key = base64.b64decode(secret["data"]["OPENAI_API_KEY"]).decode()
    assert key == "sk-fake-alice-agents/helper", "the minted key must reach the pod's Secret"
    assert key != agents.KEY_SENTINEL, (
        "an agent that starts holding -055's sentinel 401s on its first request with "
        "nothing on screen to say why"
    )
    assert ISSUED == [("alice", "agents/helper", "alice")], (
        "the key must be minted through issuance.issue for the caller, as themselves — "
        "the actor and the principal are both the signed-in user and neither is a "
        "parameter"
    )
    assert ("alice", "agent.create", "alice/helper") in AUDIT


# ---------------------------------------------------------------- reject


def test_a_second_user_cannot_see_another_users_agent(cluster):
    cluster.add_agent("alice", "scraper")
    listed = client_as("mallory").get("/portal/api/agents").json()
    assert listed["agents"] == [], (
        "one user's list showed another user's agent — the label selector carries the "
        "owner precisely so a mistake here cannot leak a row"
    )


@pytest.mark.parametrize("method,path", [
    ("POST", "/portal/api/agents/scraper/stop"),
    ("POST", "/portal/api/agents/scraper/start"),
    ("DELETE", "/portal/api/agents/scraper"),
])
def test_a_second_user_cannot_touch_another_users_agent(cluster, method, path):
    """The attack: name somebody else's agent and see what happens.

    404, not 403, and the object must be untouched. A 403 would confirm that an agent by
    that name exists and belongs to somebody, which is the one fact these endpoints exist
    to keep private.
    """
    cluster.add_agent("alice", "scraper")
    resp = client_as("mallory").request(method, path)
    assert resp.status_code == 404, (
        f"{method} {path} as mallory returned {resp.status_code}; alice's agent is "
        "reachable by name from another session"
    )
    dep = cluster.get("deployments", "agent-alice-scraper")
    assert dep is not None and dep["spec"]["replicas"] == 1, (
        "alice's agent was modified or destroyed by a request from mallory"
    )
    assert cluster.get("persistentvolumeclaims", "agent-alice-scraper") is not None


def test_a_hyphen_collision_cannot_reach_another_users_agent(cluster):
    """The case that makes name derivation insufficient on its own.

        user "alice-bot" + agent "two"  -> agent-alice-bot-two
        user "alice"     + agent "bot-two" -> agent-alice-bot-two

    Two different people, ONE object name. Deriving the name from the authenticated
    identity — the discipline `/keys/rotate` relies on — is not enough here, because both
    identities derive the same string. Only re-reading the object and checking its owner
    LABEL closes it.
    """
    cluster.add_agent("alice-bot", "two")
    assert cluster.get("deployments", "agent-alice-bot-two") is not None

    alice = client_as("alice")
    assert alice.get("/portal/api/agents").json()["agents"] == [], (
        "the collided object appeared in the other user's list"
    )
    for method, path in (("POST", "/portal/api/agents/bot-two/stop"),
                         ("DELETE", "/portal/api/agents/bot-two")):
        resp = alice.request(method, path)
        assert resp.status_code == 404, (
            f"{method} {path} as alice returned {resp.status_code} — the hyphen collision "
            "let one user drive another user's agent without ever naming them"
        )
    dep = cluster.get("deployments", "agent-alice-bot-two")
    assert dep is not None and dep["spec"]["replicas"] == 1
    assert dep["metadata"]["labels"]["agent.enterprise-ai/user"] == "alice-bot"


def test_creating_over_a_collided_name_is_refused_rather_than_applied(cluster):
    """The same collision on the create path, where the damage would be worse.

    Server-side apply over an existing Deployment would hand the caller another person's
    running agent, its volume and its session, with no error anywhere.
    """
    cluster.add_agent("alice-bot", "two")
    cluster.add_workspace_pod()
    resp = client_as("alice").post("/portal/api/agents", json={"name": "bot-two"})
    assert resp.status_code == 403, resp.text
    dep = cluster.get("deployments", "agent-alice-bot-two")
    assert dep["metadata"]["labels"]["agent.enterprise-ai/user"] == "alice-bot", (
        "alice's create overwrote alice-bot's agent"
    )
    assert ISSUED == [], "a key was minted for an agent the caller does not own"


@pytest.mark.parametrize("name", [
    "../../etc/passwd",     # path traversal into another collection
    "Scraper",              # upper case: not a legal object name
    "agent name",           # a space
    "a" * 60,               # over the RFC 1123 budget once the user is prefixed
    "has/slash",            # would break Contract 1's alias grammar
    "has::colons",          # likewise
    "",
])
def test_a_name_that_could_bend_the_object_name_is_refused(cluster, name):
    """Constrained, never sanitised. A rejected name is explainable; a rewritten one is not.

    Each of these is a way to make `agent-<user>-<name>` mean something other than this
    user's agent — a different API path, a different alias, or a truncation that collides.
    """
    cluster.add_workspace_pod()
    resp = client_as("alice").post("/portal/api/agents", json={"name": name})
    assert resp.status_code == 400, (
        f"the name {name!r} was accepted ({resp.status_code}); it must be refused before "
        "it can reach an object name or an alias"
    )
    assert cluster.names("deployments") == []


def test_a_forged_identity_header_from_off_pod_reaches_no_agent_endpoint(cluster):
    """`require_user` guards these endpoints too, not only the ones that existed before.

    A pod in the namespace can reach this Service and set any header it likes. The four
    agent endpoints hold write authority over the whole namespace, so an endpoint that
    forgot the dependency would be the worst instance of this class of bug in the codebase.
    """
    import starlette.requests

    cluster.add_agent("alice", "scraper")
    api = FastAPI()
    api.include_router(portal.router)
    c = TestClient(api, client=("127.0.0.1", 41000), raise_server_exceptions=False)
    original = starlette.requests.Request.client.fget
    starlette.requests.Request.client = property(
        lambda self: types.SimpleNamespace(host="10.42.0.99", port=1))
    try:
        headers = {"X-Auth-Request-Preferred-Username": "alice"}
        for method, path in (("GET", "/portal/api/agents"),
                             ("POST", "/portal/api/agents"),
                             ("POST", "/portal/api/agents/scraper/stop"),
                             ("POST", "/portal/api/agents/scraper/start"),
                             ("DELETE", "/portal/api/agents/scraper")):
            resp = c.request(method, path, headers=headers, json={"name": "x"})
            assert resp.status_code == 403, (
                f"{method} {path} honoured a forged identity header from off-pod "
                f"({resp.status_code}) — any workload in the namespace can now create and "
                "destroy anybody's agent"
            )
    finally:
        starlette.requests.Request.client = property(original)
    assert cluster.get("deployments", "agent-alice-scraper")["spec"]["replicas"] == 1


def test_a_missing_rbac_verb_is_reported_as_a_deployment_fault(cluster):
    """A 403 from the API server must not reach a user as a blank 500.

    It means deploy/k8s/39-control-plane-rbac.yaml was not applied, which has a one-line
    fix — and an error that does not say so is a deployment left broken for a day.
    """
    cluster.add_agent("alice", "scraper")
    cluster.deny.add("patch:deployments")
    resp = client_as("alice").post("/portal/api/agents/scraper/stop")
    assert resp.status_code == 500
    assert "39-control-plane-rbac.yaml" in resp.json()["detail"], resp.text


# ---------------------------------------------------------------- the other two tabs


def test_the_chat_and_code_tabs_are_untouched_beside_the_new_one():
    """Additive, and asserted rather than promised.

    The camp runs on Chat and Code the day after this lands. A third tab that quietly
    changed where the second one points, or dropped the first, would be discovered by a
    person rather than by a test.
    """
    index = (ROOT / "app" / "portal_static" / "index.html").read_text()
    js = (ROOT / "app" / "portal_static" / "app.js").read_text()

    for marker in ('id="tab-chat"', 'id="tab-code"', 'id="view-chat"', 'id="view-code"',
                   'id="frame-chat"', 'id="frame-code"', 'id="code-fallback"'):
        assert marker in index, f"the Agents tab removed {marker} from the portal page"
    assert 'id="tab-agents"' in index and 'id="view-agents"' in index

    assert '"/workshop/"' in js, (
        "the Code tab must still point at the workshop proxy on this origin"
    )
    assert 'frame.src = which === "code" ? "/workshop/" : (LINKS.chat || "/");' in js, (
        "the frame-mounting line changed; Chat and Code must load exactly what they loaded "
        "before the Agents tab existed"
    )
    # The Agents view is a page, not a frame: an iframe here would need its own origin and
    # would put a third scrollbar inside a tab that has to render a list.
    assert 'id="frame-agents"' not in index


def test_the_frozen_code_surface_is_not_in_this_change():
    """Contract 6, from this file's side.

    tests/test_agents_code_untouched.py owns the mechanical check. This asserts the one
    thing a portal change could plausibly break: the workshop proxy path the Code tab
    depends on is still served by the shipped module.
    """
    from app import workshop

    assert hasattr(workshop, "workshop_proxy")
