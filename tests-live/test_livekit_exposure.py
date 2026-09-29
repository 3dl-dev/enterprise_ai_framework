"""LiveKit signalling/media must not be reachable from the public edge (item 7f6).

BARON RULING 2026-09-29 (gate cfa): LiveKit is LAN/VPN only. This probes the PUBLIC
hostname from wherever the test runs (run it from outside the LAN/VPN, e.g. a cloud
runner) and fails if any LiveKit port answers or if `/rtc` upgrades to a websocket.

    EDGE_CONTROL_HOST=gateway.tailcb6ef9.ts.net EDGE_HOST=gateway.tailcb6ef9.ts.net,ai.3dl.one pytest tests-live/test_livekit_exposure.py

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


def test_positive_control_a_genuinely_public_port_connects():
    """The probe can tell reachable from unreachable. EDGE_CONTROL_HOST is the host whose 443
    IS public (the Tailscale Funnel name); it must connect from this vantage point. If the
    runner cannot reach anything, every 'refused' below is vacuous, so this fails the run."""
    control = os.environ.get("EDGE_CONTROL_HOST") or (_hosts() or [""])[0]
    if not control:
        pytest.fail("set EDGE_CONTROL_HOST (a host whose :443 is public) for the positive control")
    with socket.create_connection((control, 443), timeout=10):
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
    try:
        r = httpx.get(
            f"https://{edge}/rtc?access_token=x",
            headers={"Connection": "Upgrade", "Upgrade": "websocket",
                     "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="},
            timeout=10,
        )
    except httpx.TransportError:
        return  # not reachable from here at all (e.g. a LAN-only name): LiveKit is not exposed
    assert r.status_code != 101, "the edge upgraded /rtc to a websocket: LiveKit is routed"
    assert r.text.strip() != "OK" and r.headers.get("content-type", "").startswith("text/html"), (
        "the edge answered /rtc with something that is not the chat catch-all page"
    )
