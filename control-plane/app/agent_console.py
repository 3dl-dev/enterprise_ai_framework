"""Attaching a console to a resident agent, on the portal's own origin.

WHAT THIS IS, AND THE ONE WORD THAT DEFINES IT: **ATTACH**.

Contract 2 of docs/design/records/agents-surface.md draws the line this file sits on. The
Code/workspace surface spawns a fresh `opencode` per websocket through ttyd, and it dies
when the browser disconnects (finding 43). An Agent is the opposite: `opencode serve` is
the agent pod's own long-lived process (deploy/agent/entrypoint.sh), holding its session
with nothing connected. So this module STARTS NOTHING. There is no code path here that
creates, scales or execs anything; it forwards bytes to a daemon that was already running
before the request arrived and is still running after the socket closes. Closing the tab
is a disconnect, not a shutdown, and re-opening it reaches the same process with the same
session — which is the entire product.

WHY IT IS A PROXY AND NOT A LINK

The same reason workshop.py exists. A per-agent NodePort would be a second origin on a LAN
address: unembeddable, plain HTTP, and — from any other network — a page that hangs rather
than fails. Worse here than for a workspace, because the thing on the other end is an
unattended coding agent holding a spendable key. `deploy/k8s/63-agent-common.yaml` admits
port 4096 from the control-plane pod ALONE and there is deliberately no NodePort at all, so
this proxy is not a convenience over a reachable port; it is the only door.

WHAT MAKES IT THE CALLER'S OWN AGENT AND NOBODY ELSE'S

`/agents/<name>/` carries the instance name and never the owner (Contract 1). The owner is
`require_user()` — the identity oauth2-proxy established, honoured only from loopback (see
portal.py). Every request resolves its upstream through `agents.console_target()`, which
derives `agent-<user>-<name>` from that authenticated name and then re-reads the object and
checks its owner LABEL, because two different (user, name) pairs can derive one object
name when either half contains a hyphen. There is no authorisation logic in this file, on
purpose: the only host it can ever connect to is one that function named.

WHY THE HTML IS REWRITTEN AND A SHIM IS INJECTED

opencode's bundled web console is a single-page app that assumes it is served at the ROOT
of its origin: its `<script src>`/`<link href>` are root-absolute, and its compiled client
resolves the server it talks to as `location.origin` with no path (verified by reading the
served bundle: `location.hostname.includes("opencode.ai") ? "http://localhost:4096" :
location.origin`). Mounted under `/agents/<name>/` on the portal origin, every one of those
requests would leave the prefix and land on the chat surface at the origin root.

Two rewrites fix that, and both are deliberately about URLS ONLY — nothing here interprets
or rewrites the agent's content:

  1. the entry document's root-absolute `src=`/`href=` gain the prefix, so the bundle and
     its stylesheet load from under it (and any later `import()` chunk resolves relative to
     that, for free);
  2. a small inline shim prefixes same-origin, root-absolute URLs passed to `fetch`,
     `XMLHttpRequest`, `EventSource` and `WebSocket`.

The shim is written against web platform APIs rather than against opencode's internals for
the reason the alternative fails: the identifiers in a 3 MB minified bundle change on every
upstream release, so a find-and-replace for `location.origin` would break silently at the
next image rebuild — and "silently" on a surface nobody is watching is the failure mode
this whole record is written against. `integrate, do not reimplement` cuts the same way:
we do not fork opencode's console to make it path-aware, we adapt its URLs at the edge.

WHY RESPONSES ARE STREAMED

`GET /event` is a Server-Sent Events stream that stays open for the life of the console,
and `/api/session/<id>/prompt` streams a reply as it is generated. Buffering a response
before returning it — what workshop.py does, correctly, for a shell page — would hang both
forever. So everything is streamed except the entry document, which is read whole precisely
because it is the one thing that gets rewritten.
"""

import base64
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from . import agents
from .portal import require_user

router = APIRouter()

# Hop-by-hop headers, which must not be forwarded in either direction. `content-length`
# and `content-encoding` go with them because the body is re-framed by this hop: passing
# an upstream's length or encoding alongside a re-chunked stream is how you get a response
# that claims two different framings at once.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}

# No read timeout. The console's event stream is open for as long as the tab is, and a
# read deadline here would drop it on a schedule that looks like the agent dying. Connect
# and write still time out, because a Service with no endpoints — a stopped agent — must
# fail quickly and say so rather than hanging the page.
_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Root-absolute `src="/..."` / `href="/..."` in the entry document. The negative lookahead
# leaves `//host/path` alone: that is protocol-relative and already names another origin.
_ABSOLUTE_REF = re.compile(rb'\b(src|href)="/(?!/)')


def _clean(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP}


def _basic(target: dict) -> str:
    token = base64.b64encode(
        f"{target['username']}:{target['password']}".encode()
    ).decode()
    return f"Basic {token}"


def _prefix(name: str) -> str:
    """This console's mount point. No trailing slash: it is concatenated with a path."""
    return f"/agents/{name}"


def _shim(name: str) -> bytes:
    """Keep the console's own requests inside its prefix.

    Injected into the entry document, ahead of the bundle. It rewrites only the URL, only
    when it is same-HOST and root-absolute, and only when it is not already prefixed —
    `location.host` rather than `location.origin` because the pty socket is `ws://` or
    `wss://`, whose origin never equals the page's.

    `Proxy` for the two constructors rather than a wrapper function, so that statics
    (`WebSocket.OPEN`), `instanceof` and the prototype chain all survive untouched; the
    console reads `WebSocket.OPEN` and a hand-rolled wrapper would drop it.
    """
    prefix = _prefix(name)
    return (
        "<script>(function(){var P=" + _js_string(prefix) + ";"
        "function mine(p){return p===P||p.indexOf(P+'/')===0;}"
        "function fix(u){try{"
        "if(u===null||u===undefined)return u;"
        "var s=String(u);"
        "if(s.charAt(0)==='/'&&s.charAt(1)!=='/')return mine(s)?s:P+s;"
        "var a=new URL(s,location.href);"
        "if(a.host!==location.host||mine(a.pathname))return s;"
        "a.pathname=P+a.pathname;return a.toString();"
        "}catch(e){return u;}}"
        "var f=window.fetch;"
        "window.fetch=function(i,o){"
        "if(typeof i==='string'||i instanceof URL)return f.call(this,fix(i),o);"
        "if(i&&i.url)return f.call(this,new Request(fix(i.url),i),o);"
        "return f.call(this,i,o);};"
        "var xo=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(){"
        "arguments[1]=fix(arguments[1]);return xo.apply(this,arguments);};"
        "['EventSource','WebSocket'].forEach(function(n){var C=window[n];if(!C)return;"
        "window[n]=new Proxy(C,{construct:function(t,a){a[0]=fix(a[0]);"
        "return Reflect.construct(t,a);}});});"
        "})();</script>"
    ).encode()


def _js_string(value: str) -> str:
    """A JavaScript string literal. The name is a slug, but this is not the place to
    assume it: everything else about this file treats the path segment as untrusted."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\u003c") + '"'


def _rewrite_entry_document(body: bytes, name: str) -> bytes:
    """Prefix the entry document's root-absolute references and inject the shim."""
    body = _ABSOLUTE_REF.sub(rb'\1="' + _prefix(name).encode() + rb'/', body)
    lower = body.lower()
    head = lower.find(b"<head>")
    if head != -1:
        cut = head + len(b"<head>")
        return body[:cut] + _shim(name) + body[cut:]
    # No <head> to inject into. Prepending still runs the shim before anything the
    # document loads, which is the only ordering that matters.
    return _shim(name) + body


def _upstream_path(scope: dict, name: str, decoded: str) -> str:
    """The path to ask the daemon for, with its percent-encoding intact.

    The ASGI server hands FastAPI a DECODED path, so the `{path:path}` parameter has
    already lost the difference between `%2F` and `/`. That difference is load-bearing on
    at least one of opencode's routes — `/api/fs/read/*` carries an encoded absolute file
    path as its wildcard — and a proxy that re-sent the decoded form would ask for a
    different resource than the console asked for. `raw_path` is the original bytes, so
    where the server provides it (uvicorn does) the suffix is forwarded verbatim.
    """
    prefix = f"/agents/{name}"
    raw = scope.get("raw_path") or b""
    if raw:
        candidate = raw.split(b"?", 1)[0].decode("latin-1")
        if candidate.startswith(prefix):
            return candidate[len(prefix):] or "/"
    return "/" + decoded


def _unreachable(name: str, exc: Exception) -> HTTPException:
    return HTTPException(
        502,
        f"could not reach your agent {name!r} ({type(exc).__name__}). If you stopped it, "
        "start it from the Agents tab; if you just created it, give it a moment and "
        "reload.",
    )


@router.get("/agents/{name}", include_in_schema=False)
async def agent_console_root(name: str, user: str = Depends(require_user)):
    # Trailing slash, for the same reason /portal does it: the console's own relative URLs
    # must resolve under /agents/<name>/ rather than against the origin root.
    return RedirectResponse(f"/agents/{name}/", status_code=307)


@router.api_route("/agents/{name}/{path:path}", methods=_METHODS,
                  include_in_schema=False)
async def agent_console_proxy(name: str, path: str, request: Request,
                              user: str = Depends(require_user)):
    """Forward one request to the caller's own resident daemon.

    The upstream is named entirely by `console_target(user, name)`. `path` is appended to
    it and can only ever be a path on the host that function returned — the same shape
    workshop.py relies on, and the reason no request can be built that reaches somebody
    else's agent.
    """
    target = await agents.console_target(user, name)
    # The Agents pillar (hermes/openclaw gateway agents) has its OWN native console, proxied
    # by a separate adapter that authenticates with a session cookie and needs no SPA shim
    # (agents-gateway-console.md Contract C). Only the opencode/interim path below runs
    # through this module's Basic-auth + entry-document rewrite.
    if target.get("type") == "hermes":
        from . import agent_gateway_console
        return await agent_gateway_console.proxy_http(user, name, path, request, target)

    url = (f"http://{target['host']}:{target['port']}"
           f"{_upstream_path(request.scope, name, path)}")

    headers = _clean(request.headers)
    # The daemon's own lock, held even where the NetworkPolicy is not (see
    # deploy/agent/entrypoint.sh). The browser never sees it: it is added on this hop and
    # the credential lives in the agent's Secret, which only the control plane can read.
    headers["Authorization"] = _basic(target)
    # Otherwise the Host header still names the public origin.
    headers.pop("host", None)
    # Compressed upstream bytes cannot be rewritten, and the entry document is the one
    # response this hop rewrites. Asking for identity costs nothing on a LAN hop and
    # removes a whole class of "it worked until the daemon started gzipping" bug.
    headers["accept-encoding"] = "identity"

    body = await request.body()
    client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        upstream_request = client.build_request(
            request.method, url, content=body, headers=headers,
            # QueryParams, not `dict(request.query_params)`: a dict keeps only the LAST
            # value of a repeated key, and the console's session listing repeats them.
            params=httpx.QueryParams(request.url.query),
        )
        upstream = await client.send(upstream_request, stream=True,
                                     follow_redirects=False)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise _unreachable(name, exc)

    ctype = upstream.headers.get("content-type", "")
    out_headers = _clean(upstream.headers)

    if "text/html" in ctype:
        # THE ONE BUFFERED CASE. Read whole, rewritten, and closed here: it is a 3 KB
        # entry document, and rewriting is impossible without holding all of it.
        try:
            content = await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        return Response(
            content=_rewrite_entry_document(content, name),
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=ctype,
        )

    async def relay():
        try:
            # aiter_bytes, not aiter_raw: `content-encoding` is stripped from the headers
            # above, so the bytes that go out must be the decoded ones. `identity` was
            # requested, but a daemon that compresses anyway would otherwise produce a
            # response whose headers and body disagree.
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=ctype or None,
    )


@router.websocket("/agents/{name}/{path:path}")
async def agent_console_ws(ws: WebSocket, name: str, path: str):
    """Bridge the console's socket to the caller's own daemon.

    opencode's console opens a websocket for its terminal panel (`/api/pty/<id>/connect`).
    Identity is re-derived here rather than inherited from an earlier HTTP request,
    exactly as workshop.py does it and for the same reason: an upgrade is a fresh request,
    and this is the connection that carries a live agent.

    It ATTACHES like every other route here. The daemon and its session exist before this
    socket opens; dropping the socket ends the view, not the agent.
    """
    try:
        user = require_user(ws)          # same predicate; WebSocket carries .client
        target = await agents.console_target(user, name)
    except HTTPException:
        # 1008 policy violation. Nothing about whose agent it is, or whether it exists,
        # for the reason console_target answers 404 rather than 403.
        await ws.close(code=1008)
        return

    # Gateway agents (hermes) bridge through their own adapter — cookie auth, no shim.
    if target.get("type") == "hermes":
        from . import agent_gateway_console
        return await agent_gateway_console.proxy_ws(ws, user, name, path, target)

    import asyncio
    import inspect

    import websockets

    # The subprotocols the browser asked for, forwarded unchanged. Naming one here would
    # be guessing at opencode's, and a mismatched subprotocol presents as a socket that
    # opens and then never speaks — the failure workshop.py records hunting down.
    offered = [
        p.strip()
        for p in (ws.headers.get("sec-websocket-protocol") or "").split(",")
        if p.strip()
    ]
    upstream_url = (f"ws://{target['host']}:{target['port']}"
                    f"{_upstream_path(ws.scope, name, path)}")
    if ws.scope.get("query_string"):
        upstream_url += "?" + ws.scope["query_string"].decode()

    # websockets renamed this between its two client implementations and `connect()`
    # builds lazily, so the TypeError never surfaces at the call site. Chosen from the
    # signature, exactly as workshop.py does — the same fix, not a second guess at it.
    header_kw = (
        "additional_headers"
        if "additional_headers" in inspect.signature(websockets.connect).parameters
        else "extra_headers"
    )

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=offered or None,
            max_size=None,
            open_timeout=15,
            **{header_kw: {"Authorization": _basic(target)}},
        ) as upstream:
            negotiated = getattr(upstream, "subprotocol", None)
            await ws.accept(subprotocol=negotiated)

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
    except Exception as exc:  # noqa: BLE001
        # Either side going away is the normal end of a console session. Anything else has
        # already cost the user their terminal, and this is the only place that failure
        # becomes visible — a silent `pass` here is what made a TypeError look like a
        # hanging daemon on the workshop surface.
        import logging
        logging.getLogger("agent-console").warning(
            "console websocket for %s/%s ended: %s: %s", user, name,
            type(exc).__name__, exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
