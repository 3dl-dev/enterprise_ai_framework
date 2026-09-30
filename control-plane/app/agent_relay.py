"""The agent relay: a Raven's turn to its owner's hermes or openclaw agent (agents-raven.md,
Contract G).

openclaw (enterpriseaiframework-b12) differs only at the upstream: its gateway's OpenAI-
compatible endpoint on :18789 takes the owner's trusted-proxy identity, not a bearer key, and
reads `model` as an agent selector (pinned to its default agent). Everything below, the timeouts,
cancel, cap, header allow-list, derived session key and audit, is target-independent.

    Raven pod ──:8000──▶ control plane ──:8642──▶ the owner's hermes agent's API server
                 (68-raven-common.yaml)     (66-agent-console-common.yaml: control plane only)

Agents cannot reach each other at the network layer, deliberately: a NetworkPolicy cannot say
"same owner", so any rule letting a Raven pod reach :8642 would let every Raven reach every
user's hermes. The relay is the only door, and this module is what happens after the router
(`agent_manager.relay_chat_completions`) has authenticated the token and
`agents.relay_target` has owner-checked the target and resolved its address and credential
FROM THE CLUSTER OBJECT. Nothing here reads an address, an owner or a credential from the
request.

An agent driving another agent unattended is not a browser tab, so the console proxies'
`read=None` (agent_gateway_console.py) is NOT inherited. The stream rules, each one tested
in control-plane/tests/test_agent_relay.py:

  * TIMEOUTS. An idle timeout (no upstream byte for AGENT_RELAY_IDLE_TIMEOUT, default 120 s)
    and a total timeout per turn (AGENT_RELAY_TOTAL_TIMEOUT, default 900 s), plus connect and
    write timeouts. On expiry the upstream request is closed and the client gets an
    OpenAI-shaped error (a 504 before the reply started, an `error` event after), never a
    silent truncation.
  * CANCEL ON CLIENT DISCONNECT. A task watches the ASGI receive channel for
    `http.disconnect` for the whole turn, including the wait for the upstream's first byte;
    when Raven goes away the upstream request is closed at once, so an abandoned turn does
    not keep the target thinking and spending.
  * A CONCURRENT-STREAM CAP PER TOKEN (AGENT_RELAY_MAX_STREAMS, default 4; the next is 429
    with Retry-After). A denial-of-service guard on this process's connections and worker
    slots, NOT a quota: it counts only streams open right now and limits nothing over time
    (no per-day count, no spend cap, no agent count; BARON RULING: no quotas). In-process,
    which is correct for `replicas: 1` (40-control-plane.yaml); a scaled-out control plane
    needs a shared count.
  * THE SESSION KEY IS DERIVED HERE. `X-Hermes-Session-Key` (hermes's long-term memory scope)
    is a hash of (owner, raven, target), so there is one conversation per Raven/agent pair
    and a Raven can neither pick nor collide with another scope.
  * INBOUND HEADERS ARE ALLOW-LISTED. Only content-type and accept are forwarded. The
    Raven's `Authorization` (its agent-manager token) never reaches an agent, and no inbound
    `X-Hermes-*` header (session id, session key, anything) passes; the target's own API key
    and the derived session key are set by the relay.
  * EVERY TURN IS AUDITED: one `agent.relay.turn` row, actor `agent-manager:<owner>/<raven>`,
    target `<owner>/<name>`, with the outcome (ok, upstream_error, timeout_idle,
    timeout_total, client_cancel, capped, too_large), the upstream status, the duration and
    byte counts. Never the prompt or the reply: the audit records that a turn happened, not
    what it said. A turn refused before it reached a target (unknown, not owned, stopped) is
    the router's `agent-manager.denied` row instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import defaultdict

import anyio
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import db
from .agent_gateway_console import openclaw_trusted_headers

# Only these inbound headers are forwarded. Everything else, Authorization and X-Hermes-*
# included, is dropped by construction rather than by a deny list that can miss a name.
_FORWARD_REQUEST = ("content-type", "accept")
# Only these upstream response headers come back. The agent's own session id and anything
# hop-by-hop stay on the relay<->agent leg.
_FORWARD_RESPONSE = ("content-type", "cache-control")

CONNECT_TIMEOUT = 10.0
WRITE_TIMEOUT = 30.0
RETRY_AFTER_SECONDS = 5

# Open relay streams per agent-manager token (keyed by the token's SHA-256). A gauge of
# connections open NOW, never a counter over time.
_open_streams: dict[str, int] = defaultdict(int)


def _limits() -> tuple[float, float, int, int]:
    """(idle s, total s, max streams per token, max request bytes), read per turn from the
    environment so an operator's change applies without a code path to forget it."""
    return (float(os.environ.get("AGENT_RELAY_IDLE_TIMEOUT", "120")),
            float(os.environ.get("AGENT_RELAY_TOTAL_TIMEOUT", "900")),
            int(os.environ.get("AGENT_RELAY_MAX_STREAMS", "4")),
            int(os.environ.get("AGENT_RELAY_MAX_BODY", str(8 * 1024 * 1024))))


def session_key(owner: str, raven: str, target: str) -> str:
    """The hermes memory scope for one (Raven, target) pair, derived server-side.

    JSON-encoded before hashing so no choice of names can make two pairs collide by
    shifting a separator. Well inside hermes's 256-character limit, no control characters.
    """
    digest = hashlib.sha256(json.dumps([owner, raven, target]).encode()).hexdigest()
    return f"eaf-relay-{digest[:48]}"


def _error_body(message: str, code: str) -> dict:
    return {"error": {"message": message, "type": "relay_error", "code": code}}


class Turn:
    """One relay turn's slot in the per-token cap and its single audit row."""

    def __init__(self, mgr, name: str, idle: float, total: float, max_body: int):
        self.mgr, self.name = mgr, name
        self.idle, self.total, self.max_body = idle, total, max_body
        self.started = time.monotonic()
        self.deadline = self.started + total
        self.bytes_in = 0
        self.bytes_out = 0
        self.status: int | None = None
        self._held = True
        self._audited = False

    def _release(self) -> None:
        if self._held:
            self._held = False
            key = self.mgr.token_hash
            _open_streams[key] -= 1
            if _open_streams[key] <= 0:
                del _open_streams[key]

    def abandon(self) -> None:
        """Give the slot back without an audit row (the router audited the refusal)."""
        self._release()

    async def finish(self, outcome: str) -> None:
        """Release the slot and write the turn's ONE audit row. Idempotent, and shielded:
        it runs from a cancelled stream too, and a cancelled audit is a missing one."""
        self._release()
        if self._audited:
            return
        self._audited = True
        with anyio.CancelScope(shield=True):
            await db.audit(
                self.mgr.actor, "agent.relay.turn", f"{self.mgr.owner}/{self.name}",
                via="agent-manager", owner=self.mgr.owner, raven=self.mgr.raven,
                outcome=outcome, upstream_status=self.status,
                duration_ms=int((time.monotonic() - self.started) * 1000),
                bytes_in=self.bytes_in, bytes_out=self.bytes_out,
            )


async def open_turn(mgr, name: str, request: Request) -> Turn:
    """Take a stream slot for this token, or 429. Checked before anything else is done."""
    idle, total, max_streams, max_body = _limits()
    if _open_streams.get(mgr.token_hash, 0) >= max_streams:
        capped = Turn(mgr, name, idle, total, max_body)
        capped._held = False  # it never took a slot
        await capped.finish("capped")
        raise HTTPException(
            429, f"at most {max_streams} relay streams may be open per agent-manager token; "
                 "retry when one finishes",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
    _open_streams[mgr.token_hash] += 1
    return Turn(mgr, name, idle, total, max_body)


async def _read_body(request: Request, limit: int) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"a relay turn's request body is limited to {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _disconnected(request: Request) -> None:
    """Return when the client has gone. Called only after the body has been read, so the
    only thing left on the receive channel is `http.disconnect`."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


def _expired(turn: Turn) -> str:
    """Which timeout a wait of min(idle, remaining) that ran out has hit."""
    return "timeout_total" if time.monotonic() >= turn.deadline - 0.001 else "timeout_idle"


def _wait_for(turn: Turn) -> float:
    return max(0.0, min(turn.idle, turn.deadline - time.monotonic()))


async def _drain_cancelled(task: asyncio.Task | None) -> None:
    """Cancel `task` if it is still running and wait for it, retrieving any exception."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    with anyio.CancelScope(shield=True):
        await asyncio.gather(task, return_exceptions=True)


class _RelayResponse(StreamingResponse):
    """A StreamingResponse whose cleanup runs however the response ends: normally, on a
    timeout, on a client disconnect noticed by Starlette, or before the body ever started.
    Without this, a client that vanished before the response started would leak its slot
    and its upstream request, and the turn would never be audited."""

    def __init__(self, content, cleanup, **kw):
        super().__init__(content, **kw)
        self._cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._cleanup()


async def relay(turn: Turn, target: dict, request: Request) -> Response:
    """Forward the turn to `target` (from agents.relay_target) and stream the reply back."""
    try:
        body = await _read_body(request, turn.max_body)
    except HTTPException:
        await turn.finish("too_large")
        raise
    except BaseException:
        await turn.finish("client_cancel")
        raise
    turn.bytes_in = len(body)

    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQUEST}
    skey = session_key(turn.mgr.owner, turn.mgr.raven, turn.name)
    if target["type"] == "openclaw":
        # openclaw's gateway has no per-agent key: it runs trusted-proxy auth, so what it
        # accepts is the OWNER identity, asserted here from `target["user"]` (the label-
        # verified owner of the Deployment, agents.relay_target), never from the request.
        # The `model` field is an agent selector there, not a provider model: the Raven's
        # `model: <agent-name>` (which hermes ignores) would be an unknown agent, so it is
        # pinned to the gateway's default agent; the agent's own configured model answers.
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            await turn.finish("bad_request")
            raise HTTPException(400, "a relay turn's body must be a JSON object")
        payload["model"] = "openclaw"
        body = json.dumps(payload).encode()
        headers.update(openclaw_trusted_headers(target["user"]))
        headers["x-openclaw-session-key"] = skey
    else:
        headers["authorization"] = f"Bearer {target['key']}"
        headers["x-hermes-session-key"] = skey
    headers["accept-encoding"] = "identity"
    url = f"http://{target['host']}:{target['port']}/v1/chat/completions"

    # read=None: the idle and total timeouts below are the one place reads are bounded, so
    # an expiry is reported as the timeout it is rather than as a transport error.
    client = httpx.AsyncClient(timeout=httpx.Timeout(
        connect=CONNECT_TIMEOUT, read=None, write=WRITE_TIMEOUT, pool=CONNECT_TIMEOUT))
    gone = asyncio.create_task(_disconnected(request))
    sending = asyncio.create_task(client.send(
        client.build_request("POST", url, content=body, headers=headers), stream=True))
    upstream: httpx.Response | None = None

    async def close_all() -> None:
        await _drain_cancelled(sending)
        if upstream is not None:
            await upstream.aclose()
        elif sending.done() and not sending.cancelled() and sending.exception() is None:
            await sending.result().aclose()
        await client.aclose()
        await _drain_cancelled(gone)

    # ---- phase 1: until the upstream's response headers (a non-streamed hermes turn sends
    # nothing until it is done, so this wait is most of such a turn).
    try:
        done, _ = await asyncio.wait({sending, gone}, timeout=_wait_for(turn),
                                     return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        with anyio.CancelScope(shield=True):
            await close_all()
            await turn.finish("client_cancel")
        raise
    if sending not in done:
        outcome = "client_cancel" if gone in done else _expired(turn)
        with anyio.CancelScope(shield=True):
            await close_all()
            await turn.finish(outcome)
        if outcome == "client_cancel":
            return Response(status_code=499)
        return JSONResponse(_error_body(
            f"the agent {turn.name!r} sent nothing for "
            f"{turn.idle if outcome == 'timeout_idle' else turn.total:g}s; the turn was ended",
            outcome), status_code=504)
    try:
        upstream = sending.result()
    except httpx.HTTPError as exc:
        with anyio.CancelScope(shield=True):
            await close_all()
            await turn.finish("upstream_error")
        return JSONResponse(_error_body(
            f"the agent {turn.name!r} is not reachable ({type(exc).__name__}); it may still "
            "be starting", "upstream_unreachable"), status_code=502)

    turn.status = upstream.status_code
    ctype = upstream.headers.get("content-type", "")
    sse = "text/event-stream" in ctype
    # `tail` keeps the last bytes forwarded so the SSE terminator is found across a chunk
    # boundary. An OpenAI client stops reading at `data: [DONE]` and hangs up, often before
    # the agent closes its end; that turn is complete, not cancelled.
    state = {"outcome": None, "tail": b"", "done": False}

    def complete_or(outcome: str) -> str:
        return "ok" if state["done"] and upstream.status_code < 400 else outcome

    def error_chunk(message: str, code: str) -> bytes:
        payload = json.dumps(_error_body(message, code))
        return f"data: {payload}\n\n".encode() if sse else payload.encode()

    async def pump():
        chunks = upstream.aiter_raw()
        pending: asyncio.Task | None = None
        try:
            while True:
                pending = asyncio.ensure_future(chunks.__anext__())
                done, _ = await asyncio.wait({pending, gone}, timeout=_wait_for(turn),
                                             return_when=asyncio.FIRST_COMPLETED)
                if pending not in done:
                    if gone in done:
                        state["outcome"] = complete_or("client_cancel")
                        return
                    state["outcome"] = _expired(turn)
                    limit = turn.idle if state["outcome"] == "timeout_idle" else turn.total
                    yield error_chunk(f"the agent {turn.name!r} sent nothing for {limit:g}s; "
                                      "the turn was ended", state["outcome"])
                    return
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    state["outcome"] = "ok" if upstream.status_code < 400 else "upstream_error"
                    return
                except httpx.HTTPError as exc:
                    state["outcome"] = "upstream_error"
                    yield error_chunk(f"the agent {turn.name!r} broke off the reply "
                                      f"({type(exc).__name__})", "upstream_error")
                    return
                pending = None
                turn.bytes_out += len(chunk)
                if sse and not state["done"]:
                    state["tail"] = (state["tail"] + chunk)[-64:]
                    state["done"] = b"data: [DONE]" in state["tail"]
                yield chunk
        finally:
            await _drain_cancelled(pending)

    async def cleanup() -> None:
        await close_all()
        # No outcome recorded means the stream was cut from outside: Starlette noticed the
        # client leave (or could not send to it) and cancelled the pump.
        await turn.finish(state["outcome"] or complete_or("client_cancel"))

    out = {k: v for k, v in upstream.headers.items() if k.lower() in _FORWARD_RESPONSE}
    return _RelayResponse(pump(), cleanup, status_code=upstream.status_code, headers=out,
                          media_type=None)
