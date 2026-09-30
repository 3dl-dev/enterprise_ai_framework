"""LiveKit room tokens: whose room you can join, and that LiveKit stays off the public edge.

SECURITY TEST FIRST. The self-hosted LiveKit server (deploy/k8s/72-livekit.yaml) trusts any
token signed with its API secret, so `POST /portal/api/voice/token` is the ENTIRE room
authorisation. Every rejection below is somebody trying to obtain a token for a room that
is not theirs, driven through the real endpoint with the real `require_user` (identity
arrives as the header oauth2-proxy sets, over loopback; nothing here hands the endpoint an
identity it did not authenticate).

WHAT IS REAL AND WHAT IS NOT
Real: `app.livekit_tokens`, `app.portal.require_user`, `app.gateway.AGENT_SLUG`, the FastAPI
routing, and the manifests/Caddyfile as files on disk.
The INDEPENDENT verifier for every token is PyJWT, given the API secret the way the
LiveKit server has it (`keys:` in its config): the expected claims come from LiveKit's
documented access-token shape (iss = key, sub = identity, `video.room`/`roomJoin`), not from
the minting code. Mocks: none. The only stand-in is the environment (API key/secret/URL are
set with monkeypatch.setenv, the same way the Deployment sets them).

The exposure half reads the manifests and the example Caddyfile and asserts the Baron ruling
of 2026-09-29 (gate cfa: media LAN/VPN only, no public NodePort/TURN) as amended 2026-09-30 (82e: the ONLY public piece is /rtc signalling, routed to the oauth2-proxy portal port). `_exposure_violations` is
pointed at a poisoned COPY of the manifest in the tests below, so the checker is proven to
fail on the fault it exists to catch. The live probe from outside the edge is in
tests-live/test_livekit_exposure.py.
"""

import sys
import types
from pathlib import Path

import jwt
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

if "asyncpg" not in sys.modules:  # the driver is the shell; nothing here opens a connection
    _pg = types.ModuleType("asyncpg")
    _pg.Pool = object
    sys.modules["asyncpg"] = _pg

from app import livekit_tokens  # noqa: E402

KEY = "APIvoicetestkey"
SECRET = "s3cret-that-is-at-least-32-characters-long"
URL = "ws://192.168.2.50:30780"


@pytest.fixture()
def voice_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", SECRET)
    monkeypatch.setenv("LIVEKIT_URL", URL)
    # The endpoint now checks the raven exists and is the caller's (82e), so these tests run
    # against a cluster that holds exactly the ravens they name.
    from test_portal_agents import FakeCluster, _SA_DIR
    from app import agent_usage, agents
    cluster = FakeCluster()
    monkeypatch.setattr(agents, "KUBE_API", cluster.url)
    monkeypatch.setattr(agent_usage, "TOKEN_FILE", _SA_DIR / "token")
    monkeypatch.setattr(agent_usage, "CA_FILE", _SA_DIR / "ca.crt")
    monkeypatch.setattr(agent_usage, "NAMESPACE_FILE", _SA_DIR / "namespace")
    # ("a","b-x") and ("a-b","x") name the SAME object (agent-a-b-x), so the platform can hold
    # only one of them at a time; the collision test swaps them.
    for user, raven in (("alice", "raven"), ("a", "b-x"), ("bob", "raven")):
        cluster.add_agent(user, raven, agent_type="raven")
    from test_voice import FakeLiveKit
    lk = FakeLiveKit(KEY, SECRET)
    monkeypatch.setenv("LIVEKIT_API_URL", lk.url)
    yield cluster
    cluster.stop()
    lk.stop()


def _client(peer="127.0.0.1"):
    api = FastAPI()
    api.include_router(livekit_tokens.router)
    return TestClient(api, client=(peer, 43210))


def _as(who, **body):
    return _client().post(
        "/portal/api/voice/token",
        json=body,
        headers={"X-Auth-Request-Preferred-Username": who},
    )


def _decode(token, secret=SECRET):
    return jwt.decode(token, secret, algorithms=["HS256"], options={"verify_aud": False})


def test_user_gets_a_token_that_livekit_would_accept_for_their_own_room(voice_env):
    r = _as("alice", raven="raven")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["url"] == URL and out["room"] == "voice-alice.raven"
    claims = _decode(out["token"])  # signature checked with the secret LiveKit holds
    assert claims["iss"] == KEY
    assert claims["sub"] == "alice"
    assert claims["video"]["room"] == "voice-alice.raven"
    assert claims["video"]["roomJoin"] is True
    assert 0 < claims["exp"] - claims["nbf"] <= 900, "a room token lives minutes, not days"


def test_user_can_name_their_own_room_explicitly(voice_env):
    r = _as("alice", raven="raven", room="voice-alice.raven")
    assert r.status_code == 200 and r.json()["room"] == "voice-alice.raven"


def test_user_cannot_obtain_a_token_for_another_users_room(voice_env):
    r = _as("alice", raven="raven", room="voice-bob.raven")
    assert r.status_code == 403
    assert "token" not in r.json(), "a refusal must not carry a token"


def test_a_room_that_shares_a_prefix_with_mine_is_still_not_mine(voice_env):
    # `voice-alice.` is my prefix; `voice-alice.raven` extended is a different raven's room.
    for room in ("voice-alice.raven2", "voice-alice", "voice-alice.", "voice-alic.raven",
                 "voice-alice.raven/../voice-bob.raven", "VOICE-ALICE.RAVEN", ""):
        r = _as("alice", raven="raven", room=room)
        assert r.status_code == 403, f"{room!r} was honoured for alice/raven"


def test_hyphenated_names_cannot_collide_into_one_room(voice_env):
    # The record's `voice-<user>-<raven>` gives BOTH of these `voice-a-b-x`.
    cluster = voice_env
    room_a = _as("a", raven="b-x").json()["room"]
    # the platform can hold only one of the two (same k8s object name): swap, then mint again
    cluster.store.pop(("deployments", "agent-a-b-x"))
    cluster.add_agent("a-b", "x", agent_type="raven")
    room_ab = _as("a-b", raven="x").json()["room"]
    assert room_a != room_ab
    # And each is refused the other's, through the real endpoint.
    assert _as("a-b", raven="x", room=room_a).status_code == 403
    assert _as("a", raven="b-x", room=room_ab).status_code == 403


def test_a_body_cannot_smuggle_an_identity(voice_env):
    r = _as("alice", raven="raven", user="bob", identity="bob", sub="bob")
    assert r.status_code == 200
    claims = _decode(r.json()["token"])
    assert claims["sub"] == "alice" and claims["video"]["room"] == "voice-alice.raven"


def test_alices_token_is_worthless_against_a_different_secret(voice_env):
    token = _as("alice", raven="raven").json()["token"]
    with pytest.raises(jwt.InvalidSignatureError):
        _decode(token, secret="another-deployments-secret-32-chars-xx")


def test_raven_name_must_be_a_slug(voice_env):
    for bad in ("Raven", "a.b", "../bob.raven", "", "x" * 60):
        assert _as("alice", raven=bad).status_code == 400, bad


def test_a_pod_that_is_not_the_proxy_gets_nothing_even_with_a_perfect_header(voice_env):
    r = _client(peer="10.42.0.99").post(
        "/portal/api/voice/token", json={"raven": "raven"},
        headers={"X-Auth-Request-Preferred-Username": "alice"})
    assert r.status_code == 403 and "token" not in r.json()


def test_no_identity_is_not_signed_in(voice_env):
    r = _client().post("/portal/api/voice/token", json={"raven": "raven"})
    assert r.status_code == 401


def test_unconfigured_deployment_answers_503_rather_than_signing_with_an_empty_secret(monkeypatch):
    for k in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"):
        monkeypatch.delenv(k, raising=False)
    assert _as("alice", raven="raven").status_code == 503


def test_the_route_is_mounted_on_the_real_control_plane_app():
    main = (ROOT / "app" / "main.py").read_text()
    assert "livekit_tokens.router" in main


# ------------------------------------------------------------------ exposure (manifests)

LIVEKIT_MANIFEST = REPO / "deploy" / "k8s" / "72-livekit.yaml"
CADDYFILE = REPO / "deploy" / "caddy" / "Caddyfile"
ALLOWED_NODEPORTS = {30780, 30781, 30782}


K8S_DIR = REPO / "deploy" / "k8s"
ROUTE_KINDS = {"Ingress", "IngressRoute", "IngressRouteTCP", "IngressRouteUDP", "HTTPRoute",
               "GRPCRoute", "TCPRoute", "UDPRoute", "TLSRoute", "Gateway"}
LIVEKIT_MARKERS = ("livekit", "7880", "7881", "7882", "30780", "30781", "30782", "/rtc")


def _routing_violations(k8s_dir: Path) -> list[str]:
    """Any object in ANY manifest that routes to LiveKit from beyond the node: an Ingress /
    Gateway-API route naming livekit, its ports or /rtc, or a LoadBalancer/externalIPs Service
    that selects the livekit pods. Files are not assumed to be the livekit file."""
    bad = []
    for f in sorted(k8s_dir.glob("*.y*ml")):
        for d in yaml.safe_load_all(f.read_text()):
            if not d:
                continue
            kind, spec = d.get("kind"), d.get("spec") or {}
            blob = yaml.safe_dump(d).lower()
            if kind in ROUTE_KINDS and any(m in blob for m in LIVEKIT_MARKERS):
                bad.append(f"{f.name}: {kind} routes to livekit")
            if kind == "Service" and "livekit" in blob and (
                spec.get("type") in ("LoadBalancer", "ExternalName") or spec.get("externalIPs")
            ):
                bad.append(f"{f.name}: Service {spec.get('type') or 'externalIPs'} exposes livekit")
    return bad


PORTAL_MANIFEST_NAME = "40-control-plane.yaml"


def _portal_nodeport(k8s_dir: Path) -> int:
    """The NodePort of the Service that fronts the portal's oauth2-proxy: the only upstream a
    public /rtc route may name. Read from the manifest, not hard-coded here."""
    for d in yaml.safe_load_all((k8s_dir / PORTAL_MANIFEST_NAME).read_text()):
        if d and d.get("kind") == "Service" and "portal" in d["metadata"]["name"]:
            for p in d["spec"]["ports"]:
                if p.get("nodePort"):
                    return p["nodePort"]
    raise AssertionError("no portal NodePort Service in 40-control-plane.yaml")


def _caddy_blocks(text: str):
    """(header, body-lines) for every `header { ... }` block of a Caddyfile, comments stripped,
    innermost first; body lines include nested blocks' lines flattened."""
    stack: list[list] = []
    out = []
    for raw in text.splitlines():
        t = raw.split("#", 1)[0].strip()
        if not t:
            continue
        if t.endswith("{"):
            stack.append([t[:-1].strip(), []])
        elif t == "}":
            head, body = stack.pop()
            out.append((head, body))
            if stack:
                stack[-1][1].extend(body)
        elif stack:
            stack[-1][1].append(t)
    return out


def _rtc_route_violations(caddyfile: Path, portal_port: int) -> list[str]:
    """/rtc may be routed on the public edge ONLY to the authenticated portal upstream. Every
    Caddy `handle`/`route` block that matches /rtc (directly, or through a named matcher) must
    proxy to <host>:<portal nodePort> and nowhere else; an inline `reverse_proxy /rtc* x` too."""
    lines = [ln.split("#", 1)[0].strip() for ln in caddyfile.read_text().splitlines()]
    named = {ln.split()[0] for ln in lines if ln.startswith("@") and "/rtc" in ln.lower()}
    bad = []

    def check(where: str, proxy_lines: list[str]) -> None:
        ups = [u for ln in proxy_lines if ln.startswith("reverse_proxy")
               for u in ln.split()[1:] if not u.startswith(("/", "@", "{"))]
        if not ups:
            bad.append(f"{where}: /rtc route with no reverse_proxy upstream (cannot prove it is authenticated)")
        for u in ups:
            if not u.endswith(f":{portal_port}"):
                bad.append(f"{where}: /rtc is routed to {u}, not the authenticated portal port {portal_port}")

    for head, body in _caddy_blocks(caddyfile.read_text()):
        h = head.lower().split()
        if h and h[0] in ("handle", "handle_path", "route") and ("/rtc" in head.lower() or named & set(head.split())):
            check(head, body)
    for ln in lines:
        low = ln.lower()
        if low.startswith("reverse_proxy") and ln.endswith("{") is False and (
                "/rtc" in low or named & set(ln.split()[1:2])):
            check(ln, [ln])
    return bad


def _edge_violations(caddyfile: Path, k8s_dir: Path) -> list[str]:
    bad = _rtc_route_violations(caddyfile, _portal_nodeport(k8s_dir))
    # oauth2-proxy is the authentication: no skip-auth carve-out may exist for /rtc, and it must
    # have no upstream other than the loopback control plane.
    for d in yaml.safe_load_all((k8s_dir / PORTAL_MANIFEST_NAME).read_text()):
        if d and d.get("kind") == "Deployment":
            for c in d["spec"]["template"]["spec"]["containers"]:
                if c["name"] != "oauth2-proxy":
                    continue
                for a in c.get("args", []):
                    if a.startswith("--skip-auth-regex") or (a.startswith("--skip-auth") and "rtc" in a.lower()):
                        bad.append(f"oauth2-proxy skips authentication for a route: {a}")
                    if a.startswith("--upstream") and a != "--upstream=http://127.0.0.1:8000":
                        bad.append(f"oauth2-proxy has an unexpected upstream: {a}")
    edge = "\n".join(ln.split("#", 1)[0] for ln in caddyfile.read_text().lower().splitlines())
    for token in ("livekit", "7880", "7881", "7882", "30780", "30781", "30782", "3478", "5349"):
        if token in edge:
            bad.append(f"the public edge routes {token}")
    return bad


def _exposure_violations(manifest: Path, caddyfile: Path, k8s_dir: Path = K8S_DIR) -> list[str]:
    """Every way the files put LiveKit on the public edge, or reach for a LiveKit-Cloud piece."""
    docs = [d for d in yaml.safe_load_all(manifest.read_text()) if d]
    bad = _routing_violations(k8s_dir)
    for d in docs:
        if d["kind"] in ROUTE_KINDS:
            bad.append(f"{d['kind']} in the livekit manifest")
    for d in docs:
        spec = d.get("spec", {})
        if d["kind"] == "Service":
            if spec.get("type") not in (None, "ClusterIP", "NodePort"):
                bad.append(f"Service type {spec['type']} publishes beyond the node")
            if spec.get("externalIPs"):
                bad.append("Service sets externalIPs")
            for p in spec.get("ports", []):
                np = p.get("nodePort")
                if np is not None and np not in ALLOWED_NODEPORTS:
                    bad.append(f"unexpected nodePort {np}")
        if d["kind"] == "Deployment":
            pod = spec["template"]["spec"]
            if pod.get("hostNetwork"):
                bad.append("hostNetwork")
            for c in pod["containers"]:
                if ":" not in c["image"] or c["image"].endswith(":latest"):
                    bad.append(f"image {c['image']} is not pinned")
                for port in c.get("ports", []):
                    if port.get("hostPort"):
                        bad.append("hostPort")
                for e in c.get("env", []):
                    if e["name"] == "LIVEKIT_CONFIG_BODY":
                        cfg = yaml.safe_load(e["value"].replace("$(", "X("))
                        if cfg.get("turn", {}).get("enabled") is not False:
                            bad.append("TURN is not explicitly disabled")
                        if cfg.get("rtc", {}).get("use_external_ip"):
                            bad.append("rtc.use_external_ip advertises a public address")
    text = manifest.read_text().lower()
    for cloud in ("livekit.cloud", "krisp", "turn-detector", "inference"):
        # comments name what is absent; only non-comment lines count
        if any(cloud in ln for ln in text.splitlines() if not ln.strip().startswith("#")):
            bad.append(f"cloud/non-OSI piece referenced: {cloud}")
    bad += _edge_violations(caddyfile, k8s_dir)
    all_docs = _all_docs(manifest, k8s_dir)
    bad += _alias_route_violations(all_docs, caddyfile)
    bad += _alias_route_object_violations(all_docs)
    return bad


# ---- Service-selector resolution (item f2b): a route to an ALIAS Service is a route to LiveKit ----
# The name/port markers above miss `reverse_proxy voice-relay.enterprise-ai.svc:80` where voice-relay
# is a ClusterIP Service selecting the livekit pods and forwarding 80 -> 7880. So the checker resolves
# what each Service (or Endpoints/EndpointSlice, or ExternalName) actually reaches.

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod",
                  "ReplicationController"}
LIVEKIT_PORTS = {7880, 7881, 7882, 30780, 30781, 30782}
_NO_EXCEPTION = "the only public route to LiveKit is wss://<portal origin>/rtc behind oauth2-proxy (Baron 2026-09-30)"


def _all_docs(manifest: Path, k8s_dir: Path) -> list[tuple[str, dict]]:
    docs = []
    for f in [manifest, *sorted(k8s_dir.glob("*.y*ml"))]:
        for d in yaml.safe_load_all(f.read_text()):
            if isinstance(d, dict) and d:
                docs.append((f.name, d))
    return docs


def _pod_template(d: dict) -> dict:
    """The pod metadata+spec of any workload kind, whatever its nesting (CronJob wraps a Job)."""
    spec = d.get("spec") or {}
    if d.get("kind") == "Pod":
        return {"metadata": d.get("metadata") or {}, "spec": spec}
    if d.get("kind") == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return spec.get("template") or {}


def _is_livekit_workload(d: dict) -> bool:
    t = _pod_template(d)
    containers = (t.get("spec") or {}).get("containers") or []
    labels = (t.get("metadata") or {}).get("labels") or {}
    return (any("livekit" in str(c.get("image", "")).lower() for c in containers)
            or "livekit" in str(labels.get("app", "")).lower()
            or "livekit" in str(labels.get("app.kubernetes.io/component", "")).lower())


def _livekit_pod_labels(docs) -> list[dict]:
    """Label sets of every pod that IS livekit: read from the workloads (a livekit/ image, or a
    livekit label), never assumed to be app=livekit."""
    return [(_pod_template(d).get("metadata") or {}).get("labels") or {}
            for _, d in docs if d.get("kind") in WORKLOAD_KINDS and _is_livekit_workload(d)]


def _selects(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _lk_endpoint_refs(d: dict, lk_pod_names: set) -> bool:
    """A hand-made Endpoints/EndpointSlice that points at livekit: a targetRef naming a livekit
    pod, or a LiveKit port. (Without a selector the IPs cannot be resolved statically, so the
    port and the ref are the only evidence there is.)"""
    blob = yaml.safe_dump(d).lower()
    if "livekit" in blob or any(n and n.lower() in blob for n in lk_pod_names):
        return True
    ports = set()
    for s in d.get("subsets") or []:
        ports |= {p.get("port") for p in s.get("ports") or []}
    ports |= {p.get("port") for p in d.get("ports") or []}
    return bool(ports & LIVEKIT_PORTS)


def _host_is_service(host: str, name: str, ns: str) -> bool:
    lab = host.lower().split(".")
    return lab[0] == str(name).lower() and (len(lab) == 1 or lab[1] == str(ns).lower())


def _livekit_services(docs) -> dict[str, dict]:
    """{service name: {"ns", "why", "clusterIPs", "nodePorts"}} for every Service that reaches the
    livekit pods by ANY route: a selector matching their labels (subset match, the way
    Kubernetes matches), a selectorless Service whose Endpoints/EndpointSlice point at livekit or
    whose ports are LiveKit's, or an ExternalName aimed at one already found (transitive)."""
    pods = _livekit_pod_labels(docs)
    pod_names = {(d.get("metadata") or {}).get("name") for _, d in docs
                 if d.get("kind") in WORKLOAD_KINDS and _is_livekit_workload(d)}
    svcs = [d for _, d in docs if d.get("kind") == "Service"]
    eps = [d for _, d in docs if d.get("kind") in ("Endpoints", "EndpointSlice")]
    found: dict[str, dict] = {}

    def add(d, why):
        m, spec = d.get("metadata") or {}, d.get("spec") or {}
        e = found.setdefault(m.get("name"), {"ns": m.get("namespace", "default"), "why": why,
                                             "clusterIPs": set(), "nodePorts": set()})
        for ip in [spec.get("clusterIP"), *(spec.get("clusterIPs") or [])]:
            if ip and ip != "None":
                e["clusterIPs"].add(ip)
        e["nodePorts"] |= {p["nodePort"] for p in spec.get("ports") or [] if p.get("nodePort")}

    for d in svcs:
        spec, name = d.get("spec") or {}, (d.get("metadata") or {}).get("name")
        sel = spec.get("selector") or {}
        if sel:
            if any(_selects(sel, lb) for lb in pods):
                add(d, f"selects the livekit pods (selector {sel})")
        elif spec.get("type") != "ExternalName":
            ports = {p.get("port") for p in spec.get("ports") or []} | \
                    {p.get("targetPort") for p in spec.get("ports") or []}
            mine = [e for e in eps if (e.get("metadata") or {}).get("name") == name or
                    ((e.get("metadata") or {}).get("labels") or {}).get("kubernetes.io/service-name") == name]
            if ports & LIVEKIT_PORTS or any(_lk_endpoint_refs(e, pod_names) for e in mine):
                add(d, "is a selectorless Service whose Endpoints or ports are livekit's")
    changed = True
    while changed:  # ExternalName -> alias -> ... chains
        changed = False
        for d in svcs:
            name = (d.get("metadata") or {}).get("name")
            spec = d.get("spec") or {}
            ext = str(spec.get("externalName", "")).lower().rstrip(".")
            if name in found or spec.get("type") != "ExternalName" or not ext:
                continue
            if "livekit" in ext or any(_host_is_service(ext, n, v["ns"]) for n, v in found.items()):
                add(d, f"is an ExternalName for {ext}")
                changed = True
    return found


def _upstream_parts(u: str) -> tuple[str, int | None]:
    """host, port of a Caddy upstream in any written form: bare host, host:port, scheme://,
    srv+http://, h2c://, [v6]:port, a trailing path."""
    u = u.strip().strip('"')
    if "://" in u:
        u = u.split("://", 1)[1]
    u = u.split("/", 1)[0]
    if u.startswith("["):
        host, _, rest = u[1:].partition("]")
        port = rest.lstrip(":")
    elif u.count(":") == 1:
        host, _, port = u.partition(":")
    else:
        host, port = u, ""
    return host.lower(), int(port) if port.isdigit() else None


def _caddy_upstreams(caddyfile: Path) -> list[tuple[str, str]]:
    """(upstream, directive) for every upstream the Caddyfile hands a proxy: `reverse_proxy
    [matcher] a b c`, the `to a b` sub-directive, and `dynamic` upstream sources; comments stripped."""
    out = []
    in_rp = False
    for raw in caddyfile.read_text().splitlines():
        t = raw.split("#", 1)[0].strip()
        if not t:
            continue
        toks = t.rstrip("{").split()
        if toks and toks[0] in ("reverse_proxy", "php_fastcgi"):
            out += [(u, t) for u in toks[1:] if not u.startswith(("/", "@", "*"))]
            in_rp = t.endswith("{")
        elif in_rp and toks and toks[0] == "to":
            out += [(u, t) for u in toks[1:]]
        elif in_rp and toks and toks[0] == "dynamic":
            out.append(("dynamic:" + " ".join(toks[1:]), t))
        elif in_rp and t == "}":
            in_rp = False
    return out


def _alias_route_violations(docs, caddyfile: Path) -> list[str]:
    lk = _livekit_services(docs)
    bad = []
    for name, v in lk.items():
        for np in sorted(v["nodePorts"]):
            if np not in ALLOWED_NODEPORTS:
                bad.append(f"Service {name} ({v['why']}) publishes unexpected nodePort {np}")
    for u, where in _caddy_upstreams(caddyfile):
        if u.startswith("dynamic:") or "{" in u:
            bad.append(f"public route `{where}` has an upstream that cannot be resolved statically ({u}); {_NO_EXCEPTION}")
            continue
        host, port = _upstream_parts(u)
        for name, v in lk.items():
            if _host_is_service(host, name, v["ns"]) or host in v["clusterIPs"] or port in v["nodePorts"]:
                bad.append(f"public route `{where}` reaches Service {name}, which {v['why']}; {_NO_EXCEPTION}")
    return bad


def _backend_service_names(node) -> set:
    """Every Service name a route object backs onto, in every field spelling: backend.service.name,
    serviceName, backendRefs[].name, services[].name, defaultBackend."""
    names = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "serviceName" and isinstance(v, str):
                names.add(v)
            elif k == "service" and isinstance(v, dict) and isinstance(v.get("name"), str):
                names.add(v["name"])
            elif k in ("backendRefs", "services", "backends", "forwardTo") and isinstance(v, list):
                names |= {i["name"] for i in v if isinstance(i, dict) and isinstance(i.get("name"), str)}
            names |= _backend_service_names(v)
    elif isinstance(node, list):
        for i in node:
            names |= _backend_service_names(i)
    return names


def _alias_route_object_violations(docs) -> list[str]:
    lk = _livekit_services(docs)
    bad = []
    for fname, d in docs:
        if d.get("kind") in ROUTE_KINDS:
            for n in _backend_service_names(d.get("spec") or {}):
                for name, v in lk.items():
                    if _host_is_service(n, name, v["ns"]):
                        bad.append(f"{fname}: {d['kind']} backend {n} is Service {name}, which {v['why']}; {_NO_EXCEPTION}")
        if d.get("kind") == "Service":
            spec, name = d.get("spec") or {}, (d.get("metadata") or {}).get("name")
            if name in lk and (spec.get("type") == "LoadBalancer" or spec.get("externalIPs")):
                bad.append(f"{fname}: Service {name} {spec.get('type') or 'externalIPs'} exposes livekit ({lk[name]['why']})")
    return bad


def test_shipped_livekit_manifest_and_edge_are_lan_only():
    assert _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE) == []


def _poisoned(tmp_path, old, new):
    text = LIVEKIT_MANIFEST.read_text()
    assert old in text, "poison target moved; update the test"
    p = tmp_path / "72-livekit.yaml"
    p.write_text(text.replace(old, new, 1))
    return p


@pytest.mark.parametrize("old,new,expect", [
    ("type: NodePort", "type: LoadBalancer", "LoadBalancer"),
    ("                  enabled: false", "                  enabled: true", "TURN"),
    ("use_external_ip: false", "use_external_ip: true", "use_external_ip"),
    ("nodePort: 30780", "nodePort: 30443", "nodePort 30443"),
    ("livekit/livekit-server:v1.9.0", "livekit/livekit-server:latest", "not pinned"),
    ("      automountServiceAccountToken: false", "      hostNetwork: true\n      automountServiceAccountToken: false", "hostNetwork"),
])
def test_exposure_checker_fails_on_a_poisoned_manifest(tmp_path, old, new, expect):
    bad = _exposure_violations(_poisoned(tmp_path, old, new), CADDYFILE)
    assert any(expect in b for b in bad), bad


def test_exposure_checker_fails_when_the_public_edge_routes_livekit(tmp_path):
    edge = tmp_path / "Caddyfile"
    edge.write_text(CADDYFILE.read_text() + "\nhandle /rtc* {\n    reverse_proxy NODE_IP:30780\n}\n")
    assert any("30780" in b for b in _exposure_violations(LIVEKIT_MANIFEST, edge))


def test_control_plane_manifest_hands_the_voice_env_to_the_token_endpoint():
    docs = [d for d in yaml.safe_load_all((REPO / "deploy/k8s/40-control-plane.yaml").read_text()) if d]
    dep = next(d for d in docs if d["kind"] == "Deployment")
    cp = next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "control-plane")
    names = {e["name"] for e in cp["env"]}
    assert {"LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"} <= names


def _k8s_copy(tmp_path):
    import shutil
    d = tmp_path / "k8s"
    shutil.copytree(K8S_DIR, d)
    return d


INGRESS_TO_LIVEKIT = """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: rtc, namespace: enterprise-ai}
spec:
  rules:
    - http:
        paths:
          - {path: /rtc, pathType: Prefix, backend: {service: {name: livekit, port: {number: 7880}}}}
"""


def test_checker_flags_an_ingress_routing_rtc_to_livekit_in_a_new_manifest(tmp_path):
    d = _k8s_copy(tmp_path)
    (d / "99-rtc-ingress.yaml").write_text(INGRESS_TO_LIVEKIT)
    assert any("Ingress routes to livekit" in b for b in _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE, d))


def test_checker_flags_a_nearby_ingress_by_port_only(tmp_path):
    # second mutation: no "livekit" word, backend named otherwise, only the LiveKit port
    d = _k8s_copy(tmp_path)
    (d / "99-x.yaml").write_text(INGRESS_TO_LIVEKIT.replace("/rtc", "/ws").replace("livekit", "voice"))
    assert any("Ingress" in b for b in _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE, d))


def test_checker_flags_a_loadbalancer_service_for_livekit_in_another_file(tmp_path):
    d = _k8s_copy(tmp_path)
    (d / "99-lb.yaml").write_text(
        "apiVersion: v1\nkind: Service\nmetadata: {name: lk-public}\n"
        "spec:\n  type: LoadBalancer\n  selector: {app: livekit}\n  ports: [{port: 7880}]\n")
    assert any("exposes livekit" in b for b in _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE, d))


def test_checker_flags_a_caddy_rtc_route_without_naming_livekit(tmp_path):
    edge = tmp_path / "Caddyfile"
    edge.write_text(CADDYFILE.read_text() + "\nhandle /rtc* {\n    reverse_proxy voice:9000\n}\n")
    assert any("/rtc" in b for b in _exposure_violations(LIVEKIT_MANIFEST, edge))


def _caddy_with_block(tmp_path, block: str):
    edge = tmp_path / "Caddyfile"
    edge.write_text(CADDYFILE.read_text() + "\n" + block + "\n")
    return edge


def test_checker_flags_a_public_site_block_proxying_to_the_livekit_service(tmp_path):
    edge = _caddy_with_block(tmp_path, "https://voice.example.org:443 {\n    reverse_proxy livekit.enterprise-ai.svc:80\n}")
    assert any("livekit" in b for b in _exposure_violations(LIVEKIT_MANIFEST, edge))


def test_checker_flags_a_public_site_block_proxying_to_a_livekit_nodeport(tmp_path):
    # second mutation: no livekit word, no /rtc path, only a node IP + NodePort
    edge = _caddy_with_block(tmp_path, "https://ai.example.org:443 {\n    handle /call* {\n        reverse_proxy 192.168.2.44:30781\n    }\n}")
    assert any("30781" in b for b in _exposure_violations(LIVEKIT_MANIFEST, edge))


def test_checker_is_clean_on_the_real_caddyfile():
    assert _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE) == []


# ---- BARON RULING 2026-09-30 (82e): signalling is reachable ONLY at /rtc behind oauth2-proxy ----
# The checker must ALLOW exactly that route and still FLAG every other way to reach LiveKit. Every
# fault below is injected through the Caddyfile / manifest DATA, into a copy, then the unmodified
# checker runs on it.

def test_the_shipped_caddyfile_routes_rtc_to_the_portal_port_on_both_public_blocks():
    """Non-vacuity: the allow-case is real. Both portal-bearing site blocks route /rtc, and to
    the portal's oauth2-proxy NodePort (30460, read from the manifest, not from the checker)."""
    text = CADDYFILE.read_text()
    assert text.count("@rtc path /rtc /rtc/*") == 2
    assert text.count("handle @rtc {\n        reverse_proxy NODE_IP:30460\n    }") == 2
    docs = [d for d in yaml.safe_load_all((K8S_DIR / "40-control-plane.yaml").read_text()) if d]
    ports = [p["nodePort"] for d in docs if d["kind"] == "Service" for p in d["spec"]["ports"] if p.get("nodePort")]
    assert 30460 in ports and _portal_nodeport(K8S_DIR) == 30460


_RTC = "@rtc path /rtc /rtc/*\n    handle @rtc {\n        reverse_proxy NODE_IP:30460"


@pytest.mark.parametrize("new,expect", [
    # relabel the upstream to LiveKit's signalling NodePort: an unauthenticated route to LiveKit
    (_RTC.replace("30460", "30780"), "30780"),
    # a nearby wrong value: another port that is not the authenticated portal (the inference spoke)
    (_RTC.replace("30460", "30480"), "not the authenticated portal port"),
    # a route with no upstream to prove
    (_RTC.replace("reverse_proxy NODE_IP:30460", "respond 200"), "no reverse_proxy upstream"),
])
def test_checker_flags_a_poisoned_rtc_route_in_the_real_caddyfile(tmp_path, new, expect):
    text = CADDYFILE.read_text()
    assert _RTC in text
    edge = tmp_path / "Caddyfile"
    edge.write_text(text.replace(_RTC, new))  # poisons BOTH site blocks
    bad = _exposure_violations(LIVEKIT_MANIFEST, edge)
    assert any(expect in b for b in bad), bad


def test_checker_flags_an_unauthenticated_inline_rtc_proxy_and_a_new_public_block(tmp_path):
    edge = _caddy_with_block(tmp_path, "https://ai.example.org:443 {\n    reverse_proxy /rtc* voice:9000\n}")
    assert any("voice:9000" in b for b in _exposure_violations(LIVEKIT_MANIFEST, edge))
    edge2 = _caddy_with_block(tmp_path, "https://x.example.org:443 {\n    @sig path /rtc\n    handle @sig {\n        reverse_proxy NODE_IP:30781\n    }\n}")
    assert any("30781" in b for b in _exposure_violations(LIVEKIT_MANIFEST, edge2))


def _k8s_with_oauth_arg(tmp_path, arg):
    d = _k8s_copy(tmp_path)
    f = d / "40-control-plane.yaml"
    t = f.read_text()
    anchor = "            - --proxy-websockets=true\n"
    assert anchor in t
    f.write_text(t.replace(anchor, anchor + f"            - {arg}\n", 1))
    return d


@pytest.mark.parametrize("arg", ["--skip-auth-route=GET=^/rtc", "--skip-auth-regex=^/rtc.*", "--upstream=http://livekit:7880"])
def test_checker_flags_an_oauth2_proxy_carve_out_that_unauthenticates_rtc(tmp_path, arg):
    d = _k8s_with_oauth_arg(tmp_path, arg)
    bad = _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE, d)
    assert any("oauth2-proxy" in b for b in bad), bad


def test_livekit_pod_is_not_handed_service_link_env_that_crashes_the_server():
    """Observed live: a Service named `livekit` injects LIVEKIT_PORT=tcp://..., which livekit-server
    parses as its --port and dies on. The shipped Deployment must opt out of service links (proven live:
    the pod crash-loops without it, runs with it)."""
    def dep(manifest: Path):
        return next(d for d in yaml.safe_load_all(manifest.read_text()) if d and d["kind"] == "Deployment")
    assert dep(LIVEKIT_MANIFEST)["spec"]["template"]["spec"].get("enableServiceLinks") is False


# ---- item f2b: a route to a Service that SELECTS the livekit pods is a route to LiveKit ----
# Repro from the 4d2 audit: `reverse_proxy voice-relay.enterprise-ai.svc:80`, voice-relay a ClusterIP
# Service forwarding 80 -> 7880, returned []. Faults are injected as DATA (a new manifest file in a
# copy of deploy/k8s, a block appended to a copy of the Caddyfile); the unmodified checker then runs.
# The expected verdict is independent of the checker: which pods LiveKit runs is fixed by the real
# 72-livekit.yaml, and the only allowed public route is the shipped /rtc -> portal NodePort.

def _svc(name, selector=None, type_=None, ports=None, extra=""):
    sel = f"  selector: {selector}\n" if selector is not None else ""
    ty = f"  type: {type_}\n" if type_ else ""
    ports = ports or "[{port: 80, targetPort: 7880}]"
    return (f"apiVersion: v1\nkind: Service\nmetadata: {{name: {name}, namespace: enterprise-ai}}\n"
            f"spec:\n{ty}{sel}  ports: {ports}\n{extra}")


def _with_manifest(tmp_path, yaml_text):
    tmp_path.mkdir(parents=True, exist_ok=True)
    d = _k8s_copy(tmp_path)
    (d / "99-alias.yaml").write_text(yaml_text)
    return d


def _with_upstream(tmp_path, upstream, block=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _caddy_with_block(tmp_path, block or f"https://voice.example.org:443 {{\n    reverse_proxy {upstream}\n}}")


ALIAS = _svc("voice-relay", "{app: livekit}")


@pytest.mark.parametrize("label,manifest,upstream", [
    # the audit repro: alias ClusterIP selecting app=livekit, upstream by in-cluster DNS name
    ("selector app=livekit, .svc name", ALIAS, "voice-relay.enterprise-ai.svc:80"),
    ("bare service name", ALIAS, "voice-relay:80"),
    ("fully qualified .svc.cluster.local", ALIAS, "voice-relay.enterprise-ai.svc.cluster.local:80"),
    ("scheme-prefixed upstream", ALIAS, "http://voice-relay.enterprise-ai.svc:80"),
    ("h2c scheme upstream", ALIAS, "h2c://voice-relay:80"),
    ("upstream with no port", ALIAS, "voice-relay"),
    # selector spelled with a different livekit label, or a superset of labels
    ("selector by component label", _svc("voice-relay", "{app.kubernetes.io/component: livekit}"), "voice-relay:80"),
    ("selector by two labels", _svc("voice-relay", "{app: livekit, app.kubernetes.io/component: livekit}"), "voice-relay:80"),
    # address forms other than the name
    ("by static clusterIP", _svc("voice-relay", "{app: livekit}", extra="  clusterIP: 10.43.99.99\n"), "10.43.99.99:80"),
    ("by the alias's own NodePort", _svc("voice-relay", "{app: livekit}", "NodePort",
                                        "[{port: 80, targetPort: 7880, nodePort: 30780}]"), "NODE_IP:30780"),
    # Services that reach livekit without a selector
    ("ExternalName to the livekit service", _svc("voice-relay", None, "ExternalName",
                                               "[{port: 80}]", "  externalName: livekit.enterprise-ai.svc.cluster.local\n"), "voice-relay:80"),
    ("ExternalName chained through an alias", ALIAS + "---\n" + _svc("voice-two", None, "ExternalName", "[{port: 80}]",
                                                                    "  externalName: voice-relay.enterprise-ai.svc\n"), "voice-two:80"),
    ("selectorless Service on a livekit targetPort", _svc("voice-relay", None, None, "[{port: 80, targetPort: 7880}]"), "voice-relay:80"),
    ("selectorless Service + Endpoints naming a livekit pod", _svc("voice-relay", None, None, "[{port: 80, targetPort: 8080}]") +
     "---\napiVersion: v1\nkind: Endpoints\nmetadata: {name: voice-relay, namespace: enterprise-ai}\nsubsets:\n"
     "  - addresses: [{ip: 10.42.0.9, targetRef: {kind: Pod, name: livekit-abc}}]\n    ports: [{port: 8080}]\n", "voice-relay:80"),
    ("selectorless Service + EndpointSlice on a livekit port", _svc("voice-relay", None, None, "[{port: 80, targetPort: 8080}]") +
     "---\napiVersion: discovery.k8s.io/v1\nkind: EndpointSlice\nmetadata: {name: voice-relay-x, namespace: enterprise-ai,\n"
     "  labels: {kubernetes.io/service-name: voice-relay}}\naddressType: IPv4\nendpoints: [{addresses: [10.42.0.9]}]\nports: [{port: 7880}]\n",
     "voice-relay:80"),
])
def test_checker_flags_a_public_caddy_route_to_a_service_that_reaches_livekit(tmp_path, label, manifest, upstream):
    d = _with_manifest(tmp_path / "m", manifest)
    edge = _with_upstream(tmp_path / "e", upstream)
    bad = _exposure_violations(LIVEKIT_MANIFEST, edge, d)
    assert any("reaches Service voice-relay" in b or "reaches Service voice-two" in b for b in bad), (label, bad)


@pytest.mark.parametrize("block", [
    "https://voice.example.org:443 {\n    reverse_proxy {\n        to voice-relay:80\n    }\n}",
    "https://voice.example.org:443 {\n    reverse_proxy 10.43.0.1:80 voice-relay:80\n}",
    "https://voice.example.org:443 {\n    handle /call* {\n        reverse_proxy /call* voice-relay:80\n    }\n}",
])
def test_checker_flags_caddy_upstream_forms_other_than_a_single_bare_reverse_proxy(tmp_path, block):
    d = _with_manifest(tmp_path / "m", ALIAS)
    bad = _exposure_violations(LIVEKIT_MANIFEST, _with_upstream(tmp_path / "e", None, block), d)
    assert any("Service voice-relay" in b for b in bad), bad


@pytest.mark.parametrize("upstream", ["{$VOICE_UPSTREAM}", "{env.VOICE}:80"])
def test_checker_refuses_an_upstream_it_cannot_resolve(tmp_path, upstream):
    bad = _exposure_violations(LIVEKIT_MANIFEST, _with_upstream(tmp_path, upstream))
    assert any("cannot be resolved statically" in b for b in bad), bad


def test_checker_refuses_a_dynamic_upstream_source(tmp_path):
    edge = _with_upstream(tmp_path, None, "https://voice.example.org:443 {\n    reverse_proxy {\n        dynamic srv _sig._tcp.voice.svc\n    }\n}")
    assert any("cannot be resolved statically" in b for b in _exposure_violations(LIVEKIT_MANIFEST, edge))


INGRESS_TO_ALIAS = """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: web, namespace: enterprise-ai}
spec:
  rules:
    - http:
        paths:
          - {path: /call, pathType: Prefix, backend: {service: {name: voice-relay, port: {number: 80}}}}
"""


@pytest.mark.parametrize("label,route", [
    ("Ingress networking/v1", INGRESS_TO_ALIAS),
    ("Ingress extensions serviceName", INGRESS_TO_ALIAS.replace("{service: {name: voice-relay, port: {number: 80}}}", "{serviceName: voice-relay, servicePort: 80}")),
    ("Ingress defaultBackend", "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: web}\nspec:\n  defaultBackend: {service: {name: voice-relay, port: {number: 80}}}\n"),
    ("Gateway-API HTTPRoute backendRefs", "apiVersion: gateway.networking.k8s.io/v1\nkind: HTTPRoute\nmetadata: {name: web}\nspec:\n  rules:\n    - backendRefs: [{name: voice-relay, port: 80}]\n"),
    ("Traefik IngressRoute services", "apiVersion: traefik.io/v1alpha1\nkind: IngressRoute\nmetadata: {name: web}\nspec:\n  routes:\n    - match: PathPrefix(`/call`)\n      services: [{name: voice-relay, port: 80}]\n"),
])
def test_checker_flags_a_route_object_whose_backend_is_a_service_that_reaches_livekit(tmp_path, label, route):
    d = _with_manifest(tmp_path, ALIAS + "---\n" + route)
    bad = _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE, d)
    assert any("backend voice-relay is Service voice-relay" in b for b in bad), (label, bad)


def test_checker_flags_a_loadbalancer_alias_that_never_says_livekit(tmp_path):
    d = _with_manifest(tmp_path, _svc("voice-relay", "{app: livekit}", "LoadBalancer"))
    assert any("Service voice-relay LoadBalancer exposes livekit" in b for b in _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE, d))


def test_checker_flags_an_alias_nodeport_outside_the_allowed_livekit_ports(tmp_path):
    d = _with_manifest(tmp_path, _svc("voice-relay", "{app: livekit}", "NodePort", "[{port: 80, targetPort: 7880, nodePort: 30999}]"))
    assert any("nodePort 30999" in b for b in _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE, d))


def test_the_approved_rtc_route_is_not_flagged_but_the_same_route_to_an_alias_is(tmp_path):
    """Both ways on the exception: the shipped /rtc -> portal port is clean even with an alias
    Service present; pointing that same /rtc route at the alias is flagged."""
    d = _with_manifest(tmp_path / "m", ALIAS)
    assert _exposure_violations(LIVEKIT_MANIFEST, CADDYFILE, d) == []
    text = CADDYFILE.read_text()
    assert _RTC in text
    edge = tmp_path / "Caddyfile"
    edge.write_text(text.replace(_RTC, _RTC.replace("NODE_IP:30460", "voice-relay:80")))
    bad = _exposure_violations(LIVEKIT_MANIFEST, edge, d)
    assert any("reaches Service voice-relay" in b for b in bad), bad


@pytest.mark.parametrize("manifest,upstream", [
    # a Service that shares only the part-of label / a name-alike but selects other pods
    (_svc("voice-relay", "{app: control-plane}", None, "[{port: 80, targetPort: 8000}]"), "voice-relay:80"),
    (_svc("livekit-docs", "{app: docs}", None, "[{port: 80, targetPort: 8080}]"), "voice-docs:80"),
    # part-of is on the Deployment's own metadata, not the pod template: it selects no livekit pod
    (_svc("voice-relay", "{app.kubernetes.io/part-of: enterprise-ai-framework}"), "voice-relay:80"),
    # an alias for livekit that no public route uses
    (ALIAS, "inference-a.internal:8880"),
])
def test_checker_is_quiet_when_no_public_route_reaches_a_livekit_service(tmp_path, manifest, upstream):
    d = _with_manifest(tmp_path / "m", manifest)
    edge = _with_upstream(tmp_path / "e", upstream)
    bad = [b for b in _exposure_violations(LIVEKIT_MANIFEST, edge, d) if "public route" in b]
    assert bad == []
