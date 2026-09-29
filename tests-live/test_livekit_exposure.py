"""LiveKit signalling/media must not be reachable from the public edge (item 7f6).

BARON RULING 2026-09-29 (gate cfa): LiveKit is LAN/VPN only. This probes the PUBLIC
hostname from wherever the test runs (run it from outside the LAN/VPN, e.g. a cloud
runner) and fails if any LiveKit port answers or if `/rtc` upgrades to a websocket.

    EDGE_HOST=ai.3dl.one pytest tests-live/test_livekit_exposure.py

EDGE_HOST is required; a missing value fails rather than skips.
"""

import os
import socket

import httpx
import pytest

LIVEKIT_TCP_PORTS = (7880, 7881, 30780, 30781, 3478, 5349)


@pytest.fixture(scope="module")
def edge() -> str:
    host = os.environ.get("EDGE_HOST", "")
    if not host:
        pytest.fail("set EDGE_HOST to the public hostname to probe (e.g. ai.3dl.one)")
    return host


@pytest.mark.parametrize("port", LIVEKIT_TCP_PORTS)
def test_no_livekit_port_answers_on_the_public_host(edge, port):
    s = socket.socket()
    s.settimeout(5)
    try:
        assert s.connect_ex((edge, port)) != 0, f"{edge}:{port} accepted a TCP connection"
    finally:
        s.close()


def test_rtc_signalling_path_does_not_reach_livekit(edge):
    r = httpx.get(
        f"https://{edge}/rtc?access_token=x",
        headers={"Connection": "Upgrade", "Upgrade": "websocket",
                 "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="},
        timeout=10,
    )
    assert r.status_code != 101, "the edge upgraded /rtc to a websocket: LiveKit is routed"
    assert r.text.strip() != "OK" and r.headers.get("content-type", "").startswith("text/html"), (
        "the edge answered /rtc with something that is not the chat catch-all page"
    )
