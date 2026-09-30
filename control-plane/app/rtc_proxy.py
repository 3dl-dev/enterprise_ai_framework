"""LiveKit SIGNALLING through the portal origin (enterpriseaiframework-82e).

BARON RULING 2026-09-30 (supersedes the signalling half of gate cfa): the browser reaches
LiveKit's signalling at wss://<portal origin>/rtc. The public edge (Caddy) sends /rtc to the
portal's oauth2-proxy port, exactly as it does /portal/* and /workshop/*, so a request with no
session is redirected to sign in and never touches LiveKit. This route is the second lock: it
only bridges a socket whose identity headers arrive from the loopback sidecar (`require_user`),
and it forwards nothing but the LiveKit websocket. MEDIA is not here and never will be: it stays
on the LAN NodePorts, with no TURN and no public UDP.

The room token in the query string (`access_token`) is still what LiveKit authorises; this route
only authenticates the transport, so a signed-in user cannot use it to reach a room the token
endpoint did not mint them a token for.
"""

import asyncio
import logging
from urllib.parse import urlencode

import websockets
from fastapi import APIRouter, HTTPException, WebSocket

from .livekit_tokens import api_url
from .portal import require_user

router = APIRouter()
_log = logging.getLogger("rtc_proxy")


def _upstream(rest: str, query: str) -> str:
    base = api_url()
    scheme = "wss" if base.startswith("https") else "ws"
    host = base.split("://", 1)[1]
    path = "/rtc" + (f"/{rest}" if rest else "")
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


@router.websocket("/rtc")
@router.websocket("/rtc/{rest:path}")
async def rtc_signal(ws: WebSocket, rest: str = ""):
    try:
        user = require_user(ws)
    except HTTPException:
        await ws.close(code=1008)
        return
    url = _upstream(rest, urlencode(list(ws.query_params.multi_items())))
    await ws.accept()
    try:
        async with websockets.connect(url, max_size=None, open_timeout=15) as upstream:
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
                return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception as exc:  # either side leaving is normal; anything else is worth a line
        _log.info("rtc socket for %s ended: %s: %s", user, type(exc).__name__, exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
