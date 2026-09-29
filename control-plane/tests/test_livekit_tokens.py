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
of 2026-09-29 (gate cfa): LAN/VPN only, no public NodePort/TURN. `_exposure_violations` is
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
    room_a = _as("a", raven="b-x").json()["room"]
    room_ab = _as("a-b", raven="x").json()["room"]
    assert room_a != room_ab
    # And each is refused the other's, through the real endpoint.
    assert _as("a", raven="b-x", room=room_ab).status_code == 403
    assert _as("a-b", raven="x", room=room_a).status_code == 403


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


def _exposure_violations(manifest: Path, caddyfile: Path) -> list[str]:
    """Every way the files put LiveKit on the public edge, or reach for a LiveKit-Cloud piece."""
    docs = [d for d in yaml.safe_load_all(manifest.read_text()) if d]
    bad = []
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
    edge = caddyfile.read_text().lower()
    for token in ("livekit", "7880", "7881", "7882", "30780", "30781", "30782", "3478", "5349"):
        if token in edge:
            bad.append(f"the public edge routes {token}")
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
