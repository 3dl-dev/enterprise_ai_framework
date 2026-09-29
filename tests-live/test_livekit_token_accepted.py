"""A REAL LiveKit server accepts the control plane's token and rejects a wrong-secret one (7f6).

Deploys a throwaway `livekit-7f6` pod (the image pinned in deploy/k8s/72-livekit.yaml, keys
supplied by this test), port-forwards its signalling port, mints tokens with the real
`app.livekit_tokens.mint`, and opens the `/rtc` websocket the way a browser client does.
LiveKit answers 101 to a token signed with its secret and 401 to anything else. The pod is
always torn down. Needs kubectl on a dev cluster; absence fails rather than skips.
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
import websockets
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "control-plane"))
import types  # noqa: E402

if "asyncpg" not in sys.modules:  # the driver is the shell; minting opens no connection
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        _pg = types.ModuleType("asyncpg")
        _pg.Pool = object
        sys.modules["asyncpg"] = _pg

from app import livekit_tokens  # noqa: E402

NS, POD, PORT = "enterprise-ai", "livekit-7f6", 17880
KEY, SECRET = "APIvoice7f6", "throwaway-secret-at-least-32-characters-long"


def _image() -> str:
    for d in yaml.safe_load_all((REPO / "deploy/k8s/72-livekit.yaml").read_text()):
        if d and d["kind"] == "Deployment":
            return d["spec"]["template"]["spec"]["containers"][0]["image"]
    raise AssertionError("no livekit Deployment in 72-livekit.yaml")


def _kc(*a, **kw):
    return subprocess.run(["kubectl", "-n", NS, *a], capture_output=True, text=True, **kw)


@pytest.fixture(scope="module")
def livekit():
    _kc("delete", "pod", POD, "--ignore-not-found", "--wait=true")
    cfg = f"port: 7880\nrtc: {{tcp_port: 7881, use_external_ip: false}}\nturn: {{enabled: false}}\nkeys: {{{KEY}: {SECRET}}}\n"
    r = _kc("run", POD, f"--image={_image()}", "--restart=Never", "--port=7880",
            "--", "--config-body", cfg)
    assert r.returncode == 0, r.stderr
    pf = None
    try:
        r = _kc("wait", "--for=condition=Ready", f"pod/{POD}", "--timeout=180s")
        assert r.returncode == 0, r.stderr
        pf = subprocess.Popen(["kubectl", "-n", NS, "port-forward", f"pod/{POD}", f"{PORT}:7880"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        yield f"ws://127.0.0.1:{PORT}"
    finally:
        if pf:
            pf.terminate()
        _kc("delete", "pod", POD, "--ignore-not-found", "--wait=false")


async def _join(url, token):
    try:
        async with websockets.connect(f"{url}/rtc?access_token={token}&protocol=9", open_timeout=15):
            return 101
    except websockets.exceptions.InvalidStatus as e:
        return e.response.status_code


def test_livekit_accepts_the_control_plane_token(livekit):
    tok = livekit_tokens.mint(KEY, SECRET, identity="alice", room="voice-alice.raven")
    assert asyncio.run(_join(livekit, tok)) == 101


def test_livekit_rejects_a_token_signed_with_another_secret(livekit):
    tok = livekit_tokens.mint(KEY, "another-deployments-secret-32-chars-xx", identity="alice",
                              room="voice-alice.raven")
    assert asyncio.run(_join(livekit, tok)) == 401


def test_livekit_rejects_an_expired_token(livekit):
    tok = livekit_tokens.mint(KEY, SECRET, identity="alice", room="voice-alice.raven",
                              now=time.time() - 7200)
    assert asyncio.run(_join(livekit, tok)) == 401
