"""Serving a user's workshop as a tab, from one origin.

WHY

The workshop used to live at `http://<node-ip>:<per-user-nodeport>`. That is a
different origin, plain HTTP, and only routable on the house LAN — so it could not be
embedded in the HTTPS portal at all, and from any other network it did not fail cleanly,
it hung and timed out. "Everything in one place" cannot be built on a link like that.

So the portal proxies it. You are already authenticated here; this routes you to YOUR pod
and nobody else's, on the same origin as chat, over the same TLS, through the same funnel.

WHAT GUARDS THE POD NOW

Reaching a workspace's ttyd is reaching a shell that holds a spendable key, so losing the
per-pod oauth2-proxy is not a free simplification. Three things replace it:

  1. `require_user` — identity from the sidecar, honoured only from loopback (see
     portal.py). The upstream is chosen from THAT name, never from anything in the URL.
  2. A NetworkPolicy admitting 7681/7682 only from the control-plane pod. The CNI here
     resolves a packet on the destination's ingress rules alone, so naming the source is
     what actually closes workspace-to-workspace traffic — tested in tests-live, not
     assumed.
  3. A per-deployment token that the pod checks on every request, and ttyd's own basic
     auth. Reaching the port is not the same as using it, and this holds even if the
     policy does not.

The user-supplied part of the path never selects the upstream. It is appended to a host
derived from the authenticated username, so there is no request shape that reaches
somebody else's workspace.
"""

import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

from .portal import require_user

router = APIRouter()

SHELL_PORT = int(os.environ.get("WORKSPACE_SHELL_PORT", "7682"))
TTYD_PORT = int(os.environ.get("WORKSPACE_TTYD_PORT", "7681"))
TOKEN = os.environ.get("WORKSPACE_INTERNAL_TOKEN", "")

# Hop-by-hop headers must not be forwarded in either direction; passing them through a
# proxy is how you get a connection that claims to be chunked twice.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}


def _host(user: str) -> str:
    """The Service name for this user's workspace. Derived from identity, never input."""
    return f"ws-{user}"


def _clean(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP}


@router.api_route("/workshop", methods=["GET"], include_in_schema=False)
async def workshop_root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/workshop/", status_code=307)


@router.api_route("/workshop/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "HEAD"],
                  include_in_schema=False)
async def workshop_proxy(path: str, request: Request, user: str = Depends(require_user)):
    if not TOKEN:
        raise HTTPException(503, "workspace proxying is not configured")

    # ttyd serves under its own /terminal base path; everything else is the shell server.
    if path == "terminal" or path.startswith("terminal/"):
        port, upstream_path = TTYD_PORT, "/" + path
    else:
        port, upstream_path = SHELL_PORT, "/" + path

    url = f"http://{_host(user)}:{port}{upstream_path}"
    body = await request.body()
    headers = _clean(request.headers)
    headers["X-Workspace-Token"] = TOKEN
    # ttyd's own basic auth. Same shared credential; the pod checks it independently of
    # the NetworkPolicy.
    if port == TTYD_PORT:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"ws:{TOKEN}".encode()).decode()
    # The Host header would otherwise still name the public origin, which confuses
    # nothing here today but would quietly break any upstream that vhosts.
    headers.pop("host", None)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.request(
                request.method, url, content=body, headers=headers,
                params=dict(request.query_params), follow_redirects=False,
            )
    except httpx.ConnectError:
        raise HTTPException(
            502,
            "your workshop is not running. It may still be starting — give it a moment "
            "and reload.",
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach your workshop: {type(exc).__name__}")

    content = upstream.content
    ctype = upstream.headers.get("content-type", "")

    # The one rewrite. The shell's URLs are all relative to <base>, so moving the whole
    # surface under a prefix is a single attribute rather than a find-and-replace over
    # every fetch in the page. ttyd needs nothing: it builds its websocket URL from
    # location.pathname, so it follows the prefix by itself.
    if "text/html" in ctype and b'<base href="/">' in content:
        content = content.replace(b'<base href="/">', b'<base href="/workshop/">')

    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=_clean(upstream.headers),
        media_type=ctype or None,
    )


@router.websocket("/workshop/terminal/ws")
async def workshop_terminal_ws(ws: WebSocket):
    """Bridge the browser's terminal socket to this user's ttyd.

    Identity comes from the same headers as every other route. It is re-derived here
    rather than trusted from the HTTP session, because a websocket upgrade is a fresh
    request and this is the connection that carries a live shell.
    """
    try:
        user = require_user(ws)          # same predicate; Request and WebSocket both carry
    except HTTPException:                # .client and .headers
        await ws.close(code=1008)
        return
    if not TOKEN:
        await ws.close(code=1011)
        return

    import base64

    import websockets

    upstream_url = f"ws://{_host(user)}:{TTYD_PORT}/terminal/ws"
    auth = base64.b64encode(f"ws:{TOKEN}".encode()).decode()

    # ttyd negotiates the "tty" subprotocol; without it the server accepts the socket and
    # then never speaks, which looks exactly like a hung terminal.
    await ws.accept(subprotocol="tty")
    headers = {"Authorization": f"Basic {auth}", "X-Workspace-Token": TOKEN}

    # websockets renamed this argument between its two client implementations: the asyncio
    # client takes `additional_headers`, the legacy one `extra_headers`, and
    # `websockets.connect` resolves to whichever the installed version ships.
    #
    # It is chosen from the signature rather than by try/except, because `connect()`
    # builds lazily — the TypeError does not surface until the object is awaited, so a
    # try/except around the CALL never fires. Getting this wrong presented as a socket
    # that opened and immediately closed with code 1000, i.e. a terminal that connects
    # and then dies, which sent me looking at ttyd rather than at this line.
    import inspect

    _params = inspect.signature(websockets.connect).parameters
    _header_kw = "additional_headers" if "additional_headers" in _params else "extra_headers"

    try:
        async with websockets.connect(
            upstream_url, subprotocols=["tty"], max_size=None, open_timeout=15,
            **{_header_kw: headers},
        ) as upstream:
            import asyncio

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
    except Exception as exc:
        # Either side going away is the normal end of a terminal session. Anything else
        # has already cost the user their terminal, so it is logged rather than
        # swallowed — a silent `pass` here is what made a TypeError look like ttyd
        # hanging up, and there is no other place that failure becomes visible.
        import logging
        logging.getLogger("workshop").warning(
            "terminal websocket for %s ended: %s: %s", user, type(exc).__name__, exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
