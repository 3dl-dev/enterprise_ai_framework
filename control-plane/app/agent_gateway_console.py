"""The Agents-pillar console proxy — the hermes dashboard, surfaced at /agents/<name>/.

This is the gateway-agent counterpart to `agent_console.py`, and it is deliberately a
SEPARATE adapter, not a reuse of that module's opencode shim (agents-gateway-console.md
Contract C is explicit about this). The two differ in exactly the ways the two pillars
differ:

  * AUTH is delegated to the platform, twice over. The USER is authorised at the portal by
    `require_user()` (Keycloak) and owner-scoped by `agents.console_target()` — a non-owner
    gets the same 404 as a non-existent agent. The proxy then authenticates to the agent's
    OWN dashboard with the console credential enterpriseaiframework-f55 seeded into the key
    Secret, via the dashboard's form-login (`POST /auth/password-login` -> a session
    cookie; the basic-auth gate is cookie-based, NOT HTTP Basic). The browser therefore
    never sees the dashboard's login at all — the portal session is the only front door,
    which is the "one control plane" the standing constraint requires.

  * NO SPA URL-rewrite shim. hermes resolves its own base path from `X-Forwarded-Prefix`
    (verified against the real image by -2ba: every asset href and every redirect comes
    back under `/agents/<name>/`), so this hop injects the forwarding headers and rewrites
    nothing in the body. The entry document is streamed like every other response.

The upstream is named entirely by `console_target(user, name)`; there is no code path here
that can reach a dashboard that function did not name.
"""
from __future__ import annotations

import asyncio
import inspect
import logging

import httpx
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

from . import agents

_log = logging.getLogger("agent-gateway-console")

# No read timeout: the dashboard holds long-lived event/log streams open for the life of a
# tab. Connect/write still time out so a stopped agent (a Service with no endpoints) fails
# fast and says so rather than hanging the page.
_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Hop-by-hop headers (RFC 7230 §6.1) plus `set-cookie`: the dashboard's own session cookies
# belong to the proxy<->dashboard leg, never the browser, so they are stripped from both
# directions. The proxy re-logins on a 401 if its cached cookie goes stale, so dropping an
# upstream token-refresh cookie costs a re-login, never a broken session.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
    "content-encoding", "set-cookie", "cookie", "host",
}

# The login endpoint and provider confirmed against the real image (-2ba /8e4): the basic
# provider authenticates at POST /auth/password-login with a JSON body and mints the
# `hermes_session_*` cookies.
_LOGIN_PATH = "/auth/password-login"
_LOGIN_PROVIDER = "basic"


def _clean(headers, *, drop_cookie: bool = True) -> dict:
    skip = _HOP if drop_cookie else (_HOP - {"cookie"})
    return {k: v for k, v in headers.items() if k.lower() not in skip}


def _prefix(name: str) -> str:
    """This console's mount point, no trailing slash — concatenated with a path."""
    return f"/agents/{name}"


def _forwarded_headers(name: str, request_headers, scheme: str) -> dict:
    """The base-path headers hermes reads to resolve its SPA under /agents/<name>/.

    X-Forwarded-Prefix is the load-bearing one (the dashboard rewrites asset URLs and
    redirects under it); Host/Proto let it build correct absolute URLs. Host comes from the
    portal's own Host header so the dashboard's redirects name the portal origin, not the
    internal Service.
    """
    host = request_headers.get("host", "")
    fwd = {
        "X-Forwarded-Prefix": _prefix(name),
        "X-Forwarded-Proto": scheme or "https",
    }
    if host:
        fwd["X-Forwarded-Host"] = host
    return fwd


def _upstream_path(scope: dict, name: str, decoded: str) -> str:
    """The path to ask the dashboard for, percent-encoding intact.

    The ASGI server hands a DECODED `{path:path}`; `raw_path` keeps the original bytes, so
    where the server provides it the suffix is forwarded verbatim (the dashboard has routes
    whose wildcards carry encoded segments, and re-sending the decoded form would ask for a
    different resource).
    """
    prefix = _prefix(name)
    raw = scope.get("raw_path") or b""
    if raw:
        candidate = raw.split(b"?", 1)[0].decode("latin-1")
        if candidate.startswith(prefix):
            return candidate[len(prefix):] or "/"
    return "/" + decoded


def _unreachable(name: str, exc: Exception) -> HTTPException:
    return HTTPException(
        502,
        f"could not reach your agent {name!r}'s console ({type(exc).__name__}). If you "
        "stopped it, start it from the Agents tab; if you just created it, give it a moment "
        "and reload.",
    )


# ---- session-cookie cache -------------------------------------------------------------
#
# One logged-in dashboard session per agent, reused across the many requests a console page
# makes. Keyed by the upstream host (agent-<user>-<name>), which console_target has already
# owner-scoped — a caller can only ever reach the entry for an agent they own. Re-login is
# lazy: a request that comes back 401 clears the entry and logs in once more.
_SESSIONS: dict[str, str] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(host: str) -> asyncio.Lock:
    lock = _LOCKS.get(host)
    if lock is None:
        lock = _LOCKS[host] = asyncio.Lock()
    return lock


async def _login(base: str, target: dict) -> str:
    """Form-login to the dashboard, returning a `Cookie:` header for the session."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{base}{_LOGIN_PATH}",
                json={
                    "provider": _LOGIN_PROVIDER,
                    "username": target["username"],
                    "password": target["password"],
                    "next": "/",
                },
            )
    except httpx.HTTPError as exc:
        raise _unreachable(target.get("host", "?"), exc)
    if resp.status_code != 200:
        raise HTTPException(
            502,
            "the agent's console rejected the platform credential "
            f"(HTTP {resp.status_code}); it may be mid-restart — reload in a moment.",
        )
    # Rebuild the Cookie header from the raw Set-Cookie `name=value` pairs, VERBATIM — the
    # session token is quoted (`hermes_session_at="..."`) and re-parsing it through a cookie
    # jar strips the quotes, which the dashboard then rejects. This is exactly the bytes a
    # browser echoes back, which is what the real dashboard verified (-8e4).
    pairs = [sc.split(";", 1)[0].strip() for sc in resp.headers.get_list("set-cookie")]
    cookie = "; ".join(p for p in pairs if p)
    if not cookie:
        raise HTTPException(502, "the agent's console returned no session on login.")
    return cookie


async def _cookie_for(base: str, host: str, target: dict, *, force: bool = False) -> str:
    async with _lock_for(host):
        if force:
            _SESSIONS.pop(host, None)
        cached = _SESSIONS.get(host)
        if cached:
            return cached
        cookie = await _login(base, target)
        _SESSIONS[host] = cookie
        return cookie


async def call(target: dict, method: str, path: str, *, json=None) -> httpx.Response:
    """One authenticated JSON call to the agent's dashboard API (Contract D).

    The server-to-server path the control plane uses to drive an agent's own console —
    change its model, restart its gateway — WITHOUT pods/exec and without touching the seed
    (enterpriseaiframework-840). Reuses the cached console session and re-logins once on a
    stale 401, exactly as the proxy does. `target` is owner-scoped by `console_target`, so
    there is no path here that reaches a dashboard the caller does not own.
    """
    base = f"http://{target['host']}:{target['port']}"

    async def _do(cookie: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                return await client.request(
                    method, f"{base}{path}", json=json, headers={"Cookie": cookie})
            except httpx.HTTPError as exc:
                raise _unreachable(target.get("host", "?"), exc)

    cookie = await _cookie_for(base, target["host"], target)
    resp = await _do(cookie)
    if resp.status_code == 401:
        cookie = await _cookie_for(base, target["host"], target, force=True)
        resp = await _do(cookie)
    return resp


async def proxy_http(user: str, name: str, path: str, request: Request,
                     target: dict) -> Response:
    """Forward one HTTP request to the caller's own hermes dashboard.

    Streams every response (the entry document included — hermes needs no rewrite). The
    session cookie is injected on this hop; a 401 triggers exactly one re-login-and-retry
    so an expired cookie is invisible to the user rather than a spuriously broken console.
    """
    base = f"http://{target['host']}:{target['port']}"
    url = f"{base}{_upstream_path(request.scope, name, path)}"
    body = await request.body()

    base_headers = _clean(request.headers)
    base_headers.update(_forwarded_headers(name, request.headers, request.url.scheme))
    base_headers["accept-encoding"] = "identity"

    async def _send(cookie: str):
        client = httpx.AsyncClient(timeout=_TIMEOUT)
        headers = dict(base_headers, cookie=cookie)
        try:
            req = client.build_request(
                request.method, url, content=body, headers=headers,
                params=httpx.QueryParams(request.url.query),
            )
            upstream = await client.send(req, stream=True, follow_redirects=False)
            return client, upstream
        except httpx.HTTPError as exc:
            await client.aclose()
            raise _unreachable(name, exc)

    cookie = await _cookie_for(base, target["host"], target)
    client, upstream = await _send(cookie)
    if upstream.status_code == 401:
        # Stale session — log in again once and retry. A second 401 is a real failure.
        await upstream.aclose()
        await client.aclose()
        cookie = await _cookie_for(base, target["host"], target, force=True)
        client, upstream = await _send(cookie)

    out_headers = _clean(upstream.headers)
    ctype = upstream.headers.get("content-type", "")

    async def relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(), status_code=upstream.status_code, headers=out_headers,
        media_type=ctype or None,
    )


async def proxy_ws(ws: WebSocket, user: str, name: str, path: str, target: dict) -> None:
    """Bridge the dashboard's websocket (/api/pty, /api/ws, ...) to the caller's own agent.

    The session cookie and the forwarding headers ride the upgrade, so the SPA's ws-ticket
    handshake (which it drives over this same proxy) and the socket itself both authenticate
    as the logged-in console session. A browser disconnect ends the view, never the agent.
    """
    import websockets

    base = f"http://{target['host']}:{target['port']}"
    cookie = await _cookie_for(base, target["host"], target)

    offered = [
        p.strip()
        for p in (ws.headers.get("sec-websocket-protocol") or "").split(",")
        if p.strip()
    ]
    upstream_url = (f"ws://{target['host']}:{target['port']}"
                    f"{_upstream_path(ws.scope, name, path)}")
    if ws.scope.get("query_string"):
        upstream_url += "?" + ws.scope["query_string"].decode()

    headers = _forwarded_headers(name, ws.headers, "https")
    headers["Cookie"] = cookie

    # `websockets` renamed this between client implementations and connect() builds lazily,
    # so the wrong kw never surfaces at the call site — chosen from the signature, the same
    # fix agent_console.py uses.
    header_kw = (
        "additional_headers"
        if "additional_headers" in inspect.signature(websockets.connect).parameters
        else "extra_headers"
    )

    try:
        async with websockets.connect(
            upstream_url, subprotocols=offered or None, max_size=None,
            open_timeout=15, **{header_kw: headers},
        ) as upstream:
            await ws.accept(subprotocol=getattr(upstream, "subprotocol", None))

            async def to_upstream():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if (data := msg.get("bytes")) is not None:
                        await upstream.send(data)
                    elif (text := msg.get("text")) is not None:
                        await upstream.send(text)

            async def to_browser():
                async for msg in upstream:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            done, pending = await asyncio.wait(
                [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception as exc:  # noqa: BLE001 - normal end-of-session, surfaced not swallowed
        _log.warning("gateway console websocket for %s/%s ended: %s: %s",
                     user, name, type(exc).__name__, exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
