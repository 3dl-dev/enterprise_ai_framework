"""A minimal RFC 6455 client, shared by `agent-slack` and `agent-discord`.

WHY THIS FILE EXISTS AT ALL, GIVEN "INTEGRATE, DO NOT REIMPLEMENT"
=================================================================
`agent-email` is ~400 lines of argument parsing over `smtplib` and `imaplib` because the
standard library already ships RFC 5321 and RFC 3501. For websockets it ships nothing:
there is no `websocket` module in CPython, and there is no way to add one here — the agent
pod runs the WORKSPACE image byte-for-byte and Contract 6 of
docs/design/records/agents-surface.md forbids editing deploy/workspace/, including the
Dockerfile that decides what `pip install` put in it.

So the choice was not "library or hand-rolled". It was:

  1. Hand-roll the client half of RFC 6455 — this file, ~200 lines, no extensions, no
     fragmentation on send, no compression, no autobahn-grade edge cases.
  2. Give up RECEIVING. Slack's Socket Mode and Discord's Gateway are both websocket-only
     for inbound; the alternative is a PUBLIC HTTPS endpoint per agent (Slack Events API,
     Discord Interactions), which means publishing an inbound route from the internet into
     a pod that holds a spendable API key. That is a strictly worse trade than 200 lines.
  3. Rebuild the agent image with a websocket library, which Contract 6 forbids.

(1) is what shipped. The scope is deliberately the client subset those two services use:
one connection, text frames, client-masked as the RFC requires, ping answered with pong,
close observed. Anything outside that is an error rather than a silent best-effort — a
half-understood frame on a socket carrying a company's chat traffic should stop, not guess.

THE HANDSHAKE IS VERIFIED, NOT ASSUMED
======================================
`Sec-WebSocket-Accept` is recomputed and compared. Skipping that check is the standard
shortcut and it is wrong for the same reason certificate verification matters in
`agent-email`: an unattended agent has nobody watching to notice that "the websocket" was
actually a proxy, a captive portal, or an HTTP 200 that happened to hold the connection
open.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

# RFC 6455 §1.3. Constant, not a magic number: the server proves it spoke websocket by
# hashing our key with it, which is the only thing separating a real upgrade from any
# other server that answered 101.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


class WebSocketError(Exception):
    """The connection, the handshake, or a frame was not what the RFC requires."""


class WebSocket:
    """One connection. Not thread-safe, and deliberately not a connection pool."""

    def __init__(self, sock, url: str):
        self.sock = sock
        self.url = url
        self.closed = False
        self._buf = b""

    # ------------------------------------------------------------------ reading
    def _read(self, count: int, deadline: float) -> bytes:
        while len(self._buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no data from the websocket within the timeout")
            # Capped so a long overall deadline still notices a closed socket promptly,
            # and so SIGINT is not swallowed for minutes by one blocking recv.
            self.sock.settimeout(min(remaining, 5.0))
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            if not chunk:
                self.closed = True
                raise WebSocketError("the server closed the connection mid-frame")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def _read_frame(self, deadline: float):
        head = self._read(2, deadline)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8, deadline))[0]
        if masked:
            # RFC 6455 §5.1: a server MUST NOT mask. A masked frame here means the peer is
            # not the server it claims to be, or is a proxy rewriting the stream.
            raise WebSocketError("the server sent a masked frame, which RFC 6455 forbids")
        payload = self._read(length, deadline) if length else b""
        return fin, opcode, payload

    def recv(self, timeout: float) -> str | None:
        """One complete text message, or None once the peer has closed.

        Control frames are handled here rather than surfaced: a caller reading chat
        messages should never have to know that a ping arrived. Both services ping.
        """
        deadline = time.monotonic() + timeout
        message = b""
        while True:
            fin, opcode, payload = self._read_frame(deadline)
            if opcode == OP_CLOSE:
                self.closed = True
                return None
            if opcode == OP_PING:
                # Answered, and answered with the same payload the RFC requires. An
                # unanswered ping is how a long-lived receive gets dropped after a minute
                # with no error anyone can point at.
                self._write_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CONTINUATION:
                message += payload
            elif opcode in (OP_TEXT, OP_BINARY):
                message = payload
            else:
                raise WebSocketError(f"unknown websocket opcode {opcode:#x}")
            if fin:
                return message.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ writing
    def _write_frame(self, opcode: int, payload: bytes) -> None:
        # MASKED, always. RFC 6455 §5.3 requires every client frame to be masked, and
        # Slack and Discord both drop the connection on an unmasked one — which presents
        # as "receive works for a while and then stops", with nothing in any log.
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.settimeout(30)
        self.sock.sendall(bytes(header) + masked)

    def send(self, text: str) -> None:
        self._write_frame(OP_TEXT, text.encode("utf-8"))

    def close(self) -> None:
        try:
            if not self.closed:
                self._write_frame(OP_CLOSE, struct.pack("!H", 1000))
        except Exception:
            pass
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


def connect(url: str, *, ca_file: str | None = None, timeout: float = 30,
            headers: dict | None = None) -> WebSocket:
    """Open one websocket. `wss` verifies the certificate chain, with no way to turn it off.

    Same refusal as `agent-email`: the caller is an unattended process holding a bot token
    that can post as a company, so there is nobody to notice an interception. A private CA
    goes in the trust store (AGENT_*_CA_FILE), it does not turn verification off.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise WebSocketError(f"not a websocket url: {url.split('?')[0]}")
    if not parts.hostname:
        raise WebSocketError("websocket url has no host")
    secure = parts.scheme == "wss"
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    raw = socket.create_connection((parts.hostname, port), timeout=timeout)
    if secure:
        context = (ssl.create_default_context(cafile=ca_file) if ca_file
                   else ssl.create_default_context())
        sock = context.wrap_socket(raw, server_hostname=parts.hostname)
    else:
        sock = raw

    key = base64.b64encode(secrets.token_bytes(16)).decode()
    host_header = parts.hostname + (f":{parts.port}" if parts.port else "")
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    sock.settimeout(timeout)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    connection = WebSocket(sock, url)
    deadline = time.monotonic() + timeout
    while b"\r\n\r\n" not in connection._buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            connection.close()
            raise WebSocketError("the server never finished the websocket handshake")
        sock.settimeout(min(remaining, 5.0))
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            connection.close()
            raise WebSocketError("the server closed the connection during the handshake")
        connection._buf += chunk

    head, _, rest = connection._buf.partition(b"\r\n\r\n")
    connection._buf = rest
    head_lines = head.decode("latin-1").split("\r\n")
    status = head_lines[0]
    if " 101" not in status:
        connection.close()
        raise WebSocketError(f"the server refused the websocket upgrade: {status}")
    received = {}
    for line in head_lines[1:]:
        name, _, value = line.partition(":")
        received[name.strip().lower()] = value.strip()
    expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if received.get("sec-websocket-accept") != expected:
        connection.close()
        raise WebSocketError(
            "the server answered 101 but its Sec-WebSocket-Accept does not match the key "
            "we sent, so whatever is on the other end is not speaking websocket."
        )
    return connection
