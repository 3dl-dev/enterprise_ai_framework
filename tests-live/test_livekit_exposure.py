"""LiveKit signalling/media must not be reachable from the public edge (items 7f6/4d2).

BARON RULING 2026-09-29 (gate cfa): LiveKit MEDIA is LAN/VPN only.
BARON RULING 2026-09-30 (82e): SIGNALLING is the one public piece: wss://<origin>/rtc behind the
portal's oauth2-proxy. So /rtc must DEMAND AUTHENTICATION (302 to sign-in / 401 / 403) and every
LiveKit port and media path must stay closed.
BARON RULING 2026-09-30: "we do not guarantee that ai.3dl.one is LAN only." Private DNS is NOT
a protection, so this probes as if the name were public: EDGE_HOST lists the hostname AND the
site's real WAN IP (discovered at runtime, never committed: `ssh gateway curl -s
https://ifconfig.me`, then the EDGE_WAN_IP Actions variable). The Host/SNI sent to a bare IP is
EDGE_SNI (default ai.3dl.one), so the request is the one a browser would make.

    EDGE_CONTROL_HOST=github.com EDGE_HOST=ai.3dl.one,<wan-ip> pytest tests-live/test_livekit_exposure.py

Probed per edge target: TCP 7880 7881 30780 30781 30782 3478 5349; /rtc over 80 and 443 (auth demanded);
UDP media 7882/30782/3478 with a real STUN binding request (LiveKit's UDP mux answers one).
EDGE_HOST is required; a missing value fails rather than skips. The gate is
.github/workflows/livekit-edge-probe.yml (GitHub-hosted runner, outside the LAN, on a schedule).

CONTROLS (a probe that only ever sees "refused" proves nothing): github.com:443 must connect
and refuse 7880 through the same connect_ex; a public STUN server must answer the same STUN
datagram. The static checker in control-plane/tests/test_livekit_tokens.py is the primary
guarantee; this probe is the runtime backstop.
"""

import os
import socket
import socketserver
import struct

import httpx
import pytest

LIVEKIT_TCP_PORTS = (7880, 7881, 30780, 30781, 30782, 3478, 5349)
LIVEKIT_UDP_PORTS = (7882, 30782, 3478)
SNI = os.environ.get("EDGE_SNI", "ai.3dl.one")
PUBLIC_STUN = ("stun.l.google.com", 19302)


def _hosts() -> list[str]:
    return [h.strip() for h in os.environ.get("EDGE_HOST", "").split(",") if h.strip()]


@pytest.fixture(params=_hosts() or ["<unset>"])
def edge(request) -> str:
    if request.param == "<unset>":
        pytest.fail("set EDGE_HOST to the public hostname AND WAN IP to probe")
    return request.param


def _control() -> str:
    control = os.environ.get("EDGE_CONTROL_HOST") or (_hosts() or [""])[0]
    if not control:
        pytest.fail("set EDGE_CONTROL_HOST (a host whose :443 is public) for the positive control")
    return control


def _stun_request() -> bytes:
    return struct.pack("!HHI", 0x0001, 0, 0x2112A442) + os.urandom(12)


def stun_answers(host: str, port: int, timeout: float = 4.0) -> bool:
    """True iff `host:port/udp` replies to a STUN binding request with a binding response."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(_stun_request(), (host, port))
        data, _ = s.recvfrom(2048)
        return len(data) >= 20 and data[:2] == b"\x01\x01"
    except OSError:  # timeout, ICMP refused, unreachable: no answer
        return False
    finally:
        s.close()


def test_positive_control_a_genuinely_public_port_connects():
    """github.com:443 (in CI) is public. It must connect from this vantage point, else every
    'refused' below is vacuous and the run fails."""
    with socket.create_connection((_control(), 443), timeout=10):
        pass


def test_control_host_refuses_livekit_ports_through_the_same_probe():
    """The identical connect_ex probe reports a LiveKit port on a reachable public host as NOT
    connected, so 'not connected' is a real signal. (github.com does not run LiveKit.)"""
    s = socket.socket()
    s.settimeout(5)
    try:
        assert s.connect_ex((_control(), 7880)) != 0
    finally:
        s.close()


def test_positive_control_udp_stun_probe_can_see_an_answering_server():
    """The STUN probe used for the UDP media ports can see 'answered': a public STUN server
    replies to the very datagram sent to the edge. Without this a silent edge proves nothing."""
    assert stun_answers(*PUBLIC_STUN), "public STUN did not answer: the UDP probe is blind from here"


@pytest.mark.parametrize("port", LIVEKIT_TCP_PORTS)
def test_no_livekit_tcp_port_answers_on_the_edge(edge, port):
    s = socket.socket()
    s.settimeout(5)
    try:
        assert s.connect_ex((edge, port)) != 0, f"{edge}:{port} accepted a TCP connection"
    finally:
        s.close()


@pytest.mark.parametrize("port", LIVEKIT_UDP_PORTS)
def test_no_livekit_udp_port_answers_stun_on_the_edge(edge, port):
    assert not stun_answers(edge, port), f"{edge}:{port}/udp answered a STUN binding request"


def _get_rtc(edge: str, scheme: str, port: int | None = None):
    """GET /rtc as a browser would (Host/SNI = SNI) against the edge target, websocket-upgrade
    headers included. None when the target does not answer HTTP at all from this vantage: the
    caller MUST treat that as UNREACHABLE (see `_unreachable`), never as a pass."""
    port = port or (443 if scheme == "https" else 80)
    try:
        with httpx.Client(verify=False, timeout=10, follow_redirects=False) as c:
            return c.get(
                f"{scheme}://{edge}:{port}/rtc?access_token=x",
                headers={"Host": SNI, "Connection": "Upgrade", "Upgrade": "websocket",
                         "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="},
                extensions={"sni_hostname": SNI},
            )
    except httpx.TransportError:
        return None


def _required_reachable() -> set[str]:
    return {h.strip() for h in os.environ.get("EDGE_REQUIRE_REACHABLE", "").split(",") if h.strip()}


def _unreachable(edge: str, scheme: str) -> None:
    """A target that did not answer HTTP proved NOTHING about /rtc: it is reported as UNREACHABLE,
    never as a pass. A target named in EDGE_REQUIRE_REACHABLE (the site's WAN IP, in the workflow)
    must answer https, so an unreachable one FAILS; any other (the name, which public DNS may map
    to a private IP) is reported as a skip with the reason, not a green."""
    msg = f"UNREACHABLE: {edge} did not answer {scheme} /rtc from this vantage; nothing was verified"
    if edge in _required_reachable() and scheme == "https":
        pytest.fail(msg + " (listed in EDGE_REQUIRE_REACHABLE)")
    pytest.skip(msg)


def _assert_not_livekit(r) -> None:
    assert r.status_code != 101, "the edge upgraded /rtc to a websocket without a session: LiveKit is routed unauthenticated"
    assert r.text.strip() != "OK", "the edge answered /rtc with LiveKit's health body"
    assert "livekit" not in r.text.lower() and "livekit" not in str(r.headers).lower()


def _demands_authentication(r) -> bool:
    """BARON RULING 2026-09-30: /rtc is the portal's signalling door, behind oauth2-proxy. With no
    session it must answer 302 to the sign-in (never a 101 upgrade, a 200, or LiveKit's own
    body) or 401/403. A 404 means the route is ABSENT, which is now a defect too."""
    if r.status_code in (401, 403):
        return True
    if r.status_code in (301, 302, 303, 307):
        loc = r.headers.get("location", "")
        return "/oauth2/" in loc or "/realms/" in loc or "sign_in" in loc
    return False


@pytest.mark.parametrize("scheme", ["https", "http"])
def test_rtc_signalling_path_demands_authentication_and_never_reaches_livekit(edge, scheme):
    r = _get_rtc(edge, scheme)
    if r is None:
        _unreachable(edge, scheme)
    _assert_not_livekit(r)
    if scheme == "https":
        assert _demands_authentication(r), (
            f"{edge} answered /rtc with {r.status_code} (location={r.headers.get('location')!r}); "
            "expected a demand for authentication (302 to oauth2-proxy sign-in, or 401/403)")


def test_rtc_is_present_and_demands_authentication_on_at_least_one_edge_target():
    """A silent edge cannot be told from an absent route, and absent is now a defect: at least one
    probed target must actually answer /rtc over https with the authentication demand."""
    answered = {h: _get_rtc(h, "https") for h in _hosts()}
    ok = [h for h, r in answered.items() if r is not None and _demands_authentication(r)]
    seen = {h: (r.status_code if r is not None else None) for h, r in answered.items()}
    assert ok, f"no edge target demanded authentication for /rtc: {seen}"


def test_demand_classifier_tells_a_sign_in_redirect_from_livekit_and_from_absence():
    """Both-ways control for the classifier: a local responder answering each way is classified
    by the same function the live probe uses."""
    import http.server
    import threading

    class R(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code, loc = {"/rtc": (302, "/portal/oauth2/sign_in?rd=/rtc"), "/absent": (404, None),
                         "/open": (200, None)}[self.path.split("?")[0]]
            self.send_response(code)
            if loc:
                self.send_header("Location", loc)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK" if code == 200 else b"..")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), R)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path):
            return httpx.get(f"http://127.0.0.1:{srv.server_port}{path}", follow_redirects=False)
        assert _demands_authentication(get("/rtc"))
        assert not _demands_authentication(get("/absent"))
        assert not _demands_authentication(get("/open"))
    finally:
        srv.shutdown()


def test_rtc_probe_can_tell_a_livekit_like_responder_from_the_catch_all():
    """Both-ways control for the /rtc check: a local server answering LiveKit's plain OK body
    is flagged by the same assertion, so the check is not vacuous."""
    import http.server
    import threading

    class Fake(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = httpx.get(f"http://127.0.0.1:{srv.server_port}/rtc", timeout=5, follow_redirects=False)
        with pytest.raises(AssertionError, match="health body"):
            _assert_not_livekit(r)
    finally:
        srv.shutdown()


def _serve_raw(reply: bytes):
    """A real TCP responder that writes `reply` verbatim to whatever connects (so status codes and
    headers reach the probe over the wire, exactly as an edge's would). Returns (server, port)."""
    import threading

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            self.request.sendall(reply)

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_rtc_probe_flags_a_websocket_upgrade_and_livekit_marks_and_passes_a_plain_refusal():
    """Both-ways control for the `!= 101`, health-body and livekit-mark assertions, driven through
    the same `_get_rtc` + `_assert_not_livekit` the live probe uses, against real HTTP on the wire:
    a 101 Switching Protocols, a body/header naming livekit, and a 302 to sign-in are told apart."""
    cases = {
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n": "upgraded",
        b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nlivekit here": "livekit",
        b"HTTP/1.1 200 OK\r\nServer: livekit\r\nContent-Length: 2\r\n\r\nno": "livekit",
    }
    for reply, expect in cases.items():
        srv, port = _serve_raw(reply)
        try:
            r = _get_rtc("127.0.0.1", "http", port)
            assert r is not None, "the local responder must be reachable"
            with pytest.raises(AssertionError) as e:
                _assert_not_livekit(r)
            assert expect in str(e.value).lower() or expect in r.text.lower() or expect in str(r.headers).lower()
        finally:
            srv.shutdown()
    srv, port = _serve_raw(b"HTTP/1.1 302 Found\r\nLocation: /oauth2/sign_in?rd=/rtc\r\nContent-Length: 0\r\n\r\n")
    try:
        r = _get_rtc("127.0.0.1", "http", port)
        _assert_not_livekit(r)  # a plain sign-in redirect is NOT flagged
        assert _demands_authentication(r)
    finally:
        srv.shutdown()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_an_unreachable_rtc_target_is_reported_unreachable_not_passed(monkeypatch):
    """Both-ways control for the unreachable handling: the same probe against a port nothing listens
    on yields None; `_unreachable` then SKIPS with the UNREACHABLE reason for an ordinary target and
    FAILS for one in EDGE_REQUIRE_REACHABLE. A reachable target does not take this path."""
    assert _get_rtc("127.0.0.1", "https", _closed_port()) is None
    monkeypatch.delenv("EDGE_REQUIRE_REACHABLE", raising=False)
    with pytest.raises(pytest.skip.Exception, match="UNREACHABLE"):
        _unreachable("203.0.113.9", "https")
    monkeypatch.setenv("EDGE_REQUIRE_REACHABLE", "203.0.113.9")
    with pytest.raises(pytest.fail.Exception, match="UNREACHABLE"):
        _unreachable("203.0.113.9", "https")
    with pytest.raises(pytest.skip.Exception):  # http :80 on a required host is not required
        _unreachable("203.0.113.9", "http")
    srv, port = _serve_raw(b"HTTP/1.1 302 Found\r\nLocation: /oauth2/sign_in\r\nContent-Length: 0\r\n\r\n")
    try:
        assert _get_rtc("127.0.0.1", "http", port) is not None
    finally:
        srv.shutdown()
