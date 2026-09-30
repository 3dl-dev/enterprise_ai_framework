"""/rtc on the portal origin (enterpriseaiframework-82e): the authenticated door to LiveKit signalling.

The upstream is a REAL websocket server (the `websockets` library) on a loopback port standing
in for LiveKit's /rtc: it records the path and query it was dialled with and echoes frames. The
control plane's real FastAPI app code (`app.rtc_proxy`) and the real `require_user` run; identity
arrives as the header oauth2-proxy sets, from a loopback peer, as in the pod. The expected
values (path, query, echoed bytes) come from what the test sent and the server saw, not from the
proxy's own URL builder.
"""

import asyncio
import sys
import threading
import types
from pathlib import Path

import pytest
import websockets
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if "asyncpg" not in sys.modules:
    _pg = types.ModuleType("asyncpg")
    _pg.Pool = object
    sys.modules["asyncpg"] = _pg

from app import rtc_proxy  # noqa: E402


@pytest.fixture()
def livekit_like(monkeypatch):
    seen = []
    loop = asyncio.new_event_loop()
    started = threading.Event()
    box = {}

    async def handler(ws):
        seen.append(ws.request.path)
        async for m in ws:
            await ws.send(m)

    async def run():
        box["srv"] = await websockets.serve(handler, "127.0.0.1", 0)
        box["port"] = box["srv"].sockets[0].getsockname()[1]
        box["stop"] = asyncio.Event()
        started.set()
        await box["stop"].wait()
        box["srv"].close()
        await box["srv"].wait_closed()

    t = threading.Thread(target=lambda: loop.run_until_complete(run()), daemon=True)
    t.start()
    assert started.wait(10)
    monkeypatch.setenv("LIVEKIT_API_URL", f"http://127.0.0.1:{box['port']}")
    yield seen
    loop.call_soon_threadsafe(box["stop"].set)
    t.join(10)


def _client(peer="127.0.0.1"):
    api = FastAPI()
    api.include_router(rtc_proxy.router)
    return TestClient(api, client=(peer, 40000))


AUTH = {"X-Auth-Request-Preferred-Username": "alice"}


def test_an_authenticated_socket_is_bridged_to_livekit_with_its_query(livekit_like):
    with _client().websocket_connect("/rtc?access_token=abc.def&protocol=16", headers=AUTH) as ws:
        ws.send_bytes(b"\x01\x02join")
        assert ws.receive_bytes() == b"\x01\x02join"
        ws.send_text("hello")
        assert ws.receive_text() == "hello"
    assert livekit_like == ["/rtc?access_token=abc.def&protocol=16"]


def test_a_socket_with_no_identity_never_reaches_livekit(livekit_like):
    with pytest.raises(WebSocketDisconnect) as e:
        with _client().websocket_connect("/rtc?access_token=abc") as ws:
            ws.receive_text()
    assert e.value.code == 1008
    assert livekit_like == []


def test_forged_identity_from_a_non_loopback_peer_never_reaches_livekit(livekit_like):
    # the header is present but the peer is not the oauth2-proxy sidecar: a pod on the network
    with pytest.raises(WebSocketDisconnect) as e:
        with _client(peer="10.42.0.99").websocket_connect("/rtc?access_token=abc", headers=AUTH) as ws:
            ws.receive_text()
    assert e.value.code == 1008
    assert livekit_like == []
