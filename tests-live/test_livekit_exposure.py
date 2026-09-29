"""LiveKit signalling/media must not be reachable from the public edge (item 7f6).

BARON RULING 2026-09-29 (gate cfa): LiveKit is LAN/VPN only. This probes the PUBLIC
hostname from wherever the test runs (run it from outside the LAN/VPN, e.g. a cloud
runner) and fails if any LiveKit port answers or if `/rtc` upgrades to a websocket.

    EDGE_HOST=ai.3dl.one,203.0.113.7 pytest tests-live/test_livekit_exposure.py

EDGE_HOST is required (comma-separated: the hostname and the public IP it resolves to); a
missing value fails rather than skips. The gate is .github/workflows/livekit-edge-probe.yml,
which runs this from a GitHub-hosted runner, i.e. outside the LAN/VPN. A test that only ever
sees "refused" proves nothing, so the POSITIVE CONTROL asserts 443 on the same host DOES
connect: if the runner cannot reach the edge at all, the probe fails instead of passing.
UDP media (30782) cannot be probed: an unsolicited UDP datagram gets no reply either way.
"""

import os
import socket

import httpx
import pytest

LIVEKIT_TCP_PORTS = (7880, 7881, 30780, 30781, 3478, 5349)


def _hosts() -> list[str]:
    return [h.strip() for h in os.environ.get("EDGE_HOST", "").split(",") if h.strip()]


@pytest.fixture(params=_hosts() or ["<unset>"])
def edge(request) -> str:
    if request.param == "<unset>":
        pytest.fail("set EDGE_HOST to the public hostname(s) to probe (e.g. ai.3dl.one)")
    return request.param


def test_positive_control_the_public_port_443_connects(edge):
    """The probe can tell reachable from unreachable: 443 IS public and must connect."""
    with socket.create_connection((edge, 443), timeout=10):
        pass


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
