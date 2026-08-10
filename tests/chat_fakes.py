"""Protocol-level fakes for the Slack and Discord connectors (enterpriseaiframework-783).

WHAT THESE ARE, AND WHY THEY ARE NOT MOCKS
==========================================
A mocked SDK proves that a function was called. These are SERVERS: a real TLS listener
speaking real HTTP/1.1, and a real TLS listener speaking real RFC 6455 — so the thing under
test has to open a socket, complete a TLS handshake against a certificate chain it
verifies, form a request with the right method, path, headers and body, and then mask its
websocket frames the way the RFC requires. A poster that never forms an HTTP request cannot
pass, and neither can a receiver that never completes a websocket handshake.

They are also the SOURCE OF EXPECTED VALUES. Every assertion about what was sent reads out
of `HTTPFake.requests` or `WSConn.received` — the server's transcript — and every assertion
about what was received compares against the payload the fake generated. Nothing is
compared against a constant the tool also computes.

THE WEBSOCKET FRAMING HERE IS DELIBERATELY A SECOND, INDEPENDENT IMPLEMENTATION.
`deploy/agent/agentws.py` is the client half; this is the server half, written from RFC 6455
§5.2 rather than by importing anything from the tool. If both halves shared code, a mutual
misunderstanding of the wire format would pass — which is the whole failure mode a
protocol-level fixture exists to catch.

No Slack and no Discord account is touched by any of this. What these fixtures prove is
that the tools speak the protocols those services speak, in the security mode they require
(TLS with certificate verification on); the live half needs real app credentials, which
only a human can supply, and is a prerequisite item rather than something a suite can fake.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# The hostname every fake is reached on. It has to be a name and not an IP so the
# certificate's DNS SAN is what verification checks — the same shape a real provider
# presents. 127.0.0.1 is in the SAN too, so a test that connects by address still works.
FAKE_HOST = "localhost"


# --------------------------------------------------------------------------- TLS

@dataclass
class Certificates:
    ca_file: str
    cert_file: str
    key_file: str


def make_certificates(workdir: Path) -> Certificates:
    """A CA and a server certificate with real SANs, so verification can actually pass.

    Generated rather than checked in: a test that needs an out-of-date certificate to be
    replaced once a year is a test that will be disabled instead. The CA exists so the
    NEGATIVE case is meaningful too — the same connection with this CA absent from the
    trust store must be refused, and without a chain to verify there would be nothing to
    refuse.
    """
    workdir.mkdir(parents=True, exist_ok=True)

    def openssl(*args):
        proc = subprocess.run(["openssl", *args], capture_output=True, text=True,
                              timeout=120, cwd=str(workdir))
        assert proc.returncode == 0, f"openssl {args[0]} failed: {proc.stderr}"

    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
            "-out", "ca.pem", "-days", "3650", "-subj", "/CN=agent-chat-test-ca")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
            "-out", "server.csr", "-subj", f"/CN={FAKE_HOST}")
    (workdir / "san.cnf").write_text(f"subjectAltName=DNS:{FAKE_HOST},IP:127.0.0.1\n")
    openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", "server.pem", "-days", "3650",
            "-extfile", "san.cnf")
    return Certificates(ca_file=str(workdir / "ca.pem"),
                        cert_file=str(workdir / "server.pem"),
                        key_file=str(workdir / "server.key"))


def _server_context(certs: Certificates) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs.cert_file, certs.key_file)
    return context


# --------------------------------------------------------------------------- HTTP

@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict
    body: bytes

    @property
    def json(self) -> dict | None:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode())
        except json.JSONDecodeError:
            return None

    @property
    def authorization(self) -> str:
        return self.headers.get("authorization", "")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass
class Response:
    status: int = 200
    body: dict | list | str = field(default_factory=dict)
    content_type: str = "application/json"


class HTTPFake:
    """A TLS HTTP/1.1 server the test owns, recording every request verbatim.

    `handler(request) -> Response`. Everything it received is on `.requests`, which is what
    the assertions read: the authorization header, the path, and the exact JSON body that
    went over the socket.
    """

    def __init__(self, certs: Certificates, handler):
        self.certs = certs
        self.handler = handler
        self.requests: list[RecordedRequest] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                record = RecordedRequest(
                    method=method,
                    path=self.path,
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=body,
                )
                fake.requests.append(record)
                response = fake.handler(record)
                payload = (response.body.encode() if isinstance(response.body, str)
                           else json.dumps(response.body).encode())
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def log_message(self, *_args):
                pass  # the transcript is `.requests`, not stderr

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = _server_context(certs).wrap_socket(self.server.socket,
                                                                server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"https://{FAKE_HOST}:{self.port}"

    def paths(self) -> list[str]:
        return [r.path for r in self.requests]

    def only(self, path: str) -> RecordedRequest:
        """The single request to `path`. Fails loudly if there was not exactly one."""
        matching = [r for r in self.requests if r.path == path]
        assert len(matching) == 1, (
            f"expected exactly one request to {path}, saw {len(matching)}; "
            f"all paths: {self.paths()}"
        )
        return matching[0]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------- websocket

class WSConn:
    """One accepted websocket connection, framed from RFC 6455 §5.2 directly.

    Independent of deploy/agent/agentws.py on purpose: a shared codec would let a mutual
    misreading of the wire format pass as agreement.
    """

    def __init__(self, sock, handshake: dict):
        self.sock = sock
        self.handshake = handshake
        self.received: list[str] = []
        self._buf = b""

    # ---- reading (client -> server frames, which MUST be masked)
    def _read(self, count: int, deadline: float) -> bytes:
        while len(self._buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the client sent nothing within the timeout")
            self.sock.settimeout(min(remaining, 2.0))
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            if not chunk:
                raise ConnectionError("the client closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def recv_text(self, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        while True:
            head = self._read(2, deadline)
            opcode = head[0] & 0x0F
            masked = bool(head[1] & 0x80)
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8, deadline))[0]
            # THE ASSERTION THAT MAKES THIS A PROTOCOL TEST. RFC 6455 §5.3 requires every
            # client-to-server frame to be masked, and real Slack and real Discord drop a
            # connection that sends an unmasked one. A client that got this wrong would
            # "work" against a permissive fixture and fail in production.
            assert masked, (
                "the client sent an UNMASKED frame. RFC 6455 §5.3 requires client frames "
                "to be masked, and Slack and Discord both close the connection on one."
            )
            mask = self._read(4, deadline)
            payload = self._read(length, deadline) if length else b""
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("the client closed the connection")
            if opcode in (0x9, 0xA):
                continue
            text = payload.decode()
            self.received.append(text)
            return text

    def recv_json(self, timeout: float = 10) -> dict:
        return json.loads(self.recv_text(timeout))

    # ---- writing (server -> client frames, which MUST NOT be masked)
    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        self.sock.sendall(bytes(header) + payload)

    def send_json(self, obj) -> None:
        self.send_text(json.dumps(obj))

    def ping(self, payload: bytes = b"are-you-there") -> None:
        self.sock.sendall(bytes([0x89, len(payload)]) + payload)

    def close(self) -> None:
        try:
            self.sock.sendall(bytes([0x88, 2]) + struct.pack("!H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class WSFake:
    """A TLS websocket server the test owns. `on_connect(conn)` runs per connection."""

    def __init__(self, certs: Certificates, on_connect, path: str = "/link"):
        self.certs = certs
        self.on_connect = on_connect
        self.path = path
        self.handshakes: list[dict] = []
        self.errors: list[BaseException] = []
        self._context = _server_context(certs)

        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"wss://{FAKE_HOST}:{self.port}{self.path}"

    def _serve(self):
        while not self._stop.is_set():
            try:
                self.listener.settimeout(0.5)
                raw, _addr = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(raw,), daemon=True).start()

    def _handle(self, raw):
        try:
            sock = self._context.wrap_socket(raw, server_side=True)
            sock.settimeout(10)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            handshake = {"request_line": lines[0], "headers": headers}
            self.handshakes.append(handshake)

            key = headers.get("sec-websocket-key", "")
            accept = base64.b64encode(
                hashlib.sha1((key + GUID).encode()).digest()).decode()
            sock.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                          "Upgrade: websocket\r\n"
                          "Connection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            conn = WSConn(sock, handshake)
            conn._buf = rest
            try:
                self.on_connect(conn)
            finally:
                conn.close()
        except BaseException as exc:  # recorded, so a fixture fault is not a mystery
            self.errors.append(exc)

    def close(self):
        self._stop.set()
        try:
            self.listener.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- running the CLI

def run_cli(cli: Path, args, env_extra: dict, timeout: float = 120):
    """Run a real agent tool as a subprocess, the way opencode's shell tool would.

    The environment is built from scratch rather than inherited-and-patched: the tools read
    their whole configuration from AGENT_SLACK_* / AGENT_DISCORD_*, so a leaked variable
    from the developer's shell would be a test that passes for a reason the pod does not
    have.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/tmp")}
    env.update(env_extra)
    return subprocess.run([str(cli), *args], capture_output=True, text=True,
                          timeout=timeout, env=env)
