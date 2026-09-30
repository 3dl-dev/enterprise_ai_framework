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
