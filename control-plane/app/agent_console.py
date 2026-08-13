"""A terminal console attached to a resident Hermes agent, on the portal's own origin.

WHAT THIS IS, AND THE ONE WORD THAT DEFINES IT: **ATTACH**.

The retarget (docs/design/records/agents-surface-hermes-retarget.md, R3). The resident
daemon is `hermes gateway run` — the pod's own long-lived process — and this console does
NOT spawn it. It opens `hermes --tui` *inside the already-running pod* over the Kubernetes
`pods/exec` subresource; `hermes --tui` is self-contained and coordinates with the daemon
only through the shared on-disk session (`/opt/data/state.db`), so it must run in the same
container/HERMES_HOME, which is exactly what exec gives. Closing the tab ends the view, not
the agent; re-opening reaches the same session. This is the operator terminal — configure,
debug, extend — the thing you console into when chat goes sideways. It is NOT opencode's
web IDE (that was the conflation this retarget removes), and it is NOT a coding surface.

WHY EXEC AND NOT A PROXIED PORT

`hermes gateway run` opens no inbound port, so there is nothing to proxy. exec streams
through the API server to the kubelet to the pod, so the pod needs no Service and no
NodePort — the control plane's `pods/exec` RBAC (deploy/k8s/39-control-plane-rbac.yaml) is
the only door, which is the same one-door posture the opencode proxy had, now with no
listening process on the agent at all.

WHAT MAKES IT THE CALLER'S OWN AGENT AND NOBODY ELSE'S

`/agents/<name>/` carries the instance name and never the owner (Contract 1). The owner is
`require_user()` — the identity oauth2-proxy established, honoured only from loopback (see
portal.py). Both the page and the socket resolve the target through
`agents.console_target()`, which derives `agent-<user>-<name>` from that authenticated name,
re-reads the object and checks its owner LABEL (two (user, name) pairs can derive one object
name when either half has a hyphen), and returns the *running pod* to exec into. There is no
authorisation logic in this file, on purpose: the only pod it can ever exec is one that
function named, in the namespace it named.

THE TWO PROTOCOLS BRIDGED HERE

Browser ↔ this module: JSON text frames — `{"type":"stdin","data":"…"}` for keystrokes and
`{"type":"resize","cols":C,"rows":R}` for a window resize. This module ↔ the API server:
the `v4.channel.k8s.io` exec framing, where every frame's first byte is a channel — 0 stdin
(outbound), 1 stdout, 2 stderr, 3 error — and channel 4 carries a `{"Width","Height"}`
resize. This file only reframes bytes and sizes; it never interprets the agent's content.
"""

import asyncio
import inspect
import json
import logging
import os
import ssl

from fastapi import APIRouter, Depends, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse

from . import agent_usage, agents
from .portal import require_user

router = APIRouter()

_log = logging.getLogger("agent-console")

# The exec subresource, reached the same authenticated way agents.py reaches the API — the
# projected SA token (read per call; the kubelet rotates it) and the cluster CA. The host is
# the DNS name `kubernetes.default.svc`, not KUBERNETES_SERVICE_HOST's IP, so TLS verifies
# against the apiserver cert's DNS SAN rather than depending on an IP SAN being present.
_KUBE_WS = "wss://kubernetes.default.svc:{}".format(
    os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
)
# v4 over v5: v4 is the channel framing every apiserver in support range speaks; v5 only
# adds a close signal this bridge does not need (the socket closing IS the signal).
_EXEC_SUBPROTOCOL = "v4.channel.k8s.io"

# Exec channels (the first byte of every v4 frame).
_STDIN, _STDOUT, _STDERR, _ERR, _RESIZE = 0, 1, 2, 3, 4


def _ssl_context() -> ssl.SSLContext:
    """Trust the cluster CA, the same chain agent_usage verifies the REST calls against."""
    return ssl.create_default_context(cafile=str(agent_usage.CA_FILE))


def _exec_url(namespace: str, pod: str, container: str, command: list[str]) -> str:
    from urllib.parse import urlencode

    params = [
        ("container", container),
        ("stdin", "true"), ("stdout", "true"), ("stderr", "true"), ("tty", "true"),
    ]
    params += [("command", part) for part in command]
    return (f"{_KUBE_WS}/api/v1/namespaces/{namespace}/pods/{pod}/exec"
            f"?{urlencode(params)}")


def _js_string(value: str) -> str:
    """A JavaScript string literal. `name` is slug-constrained, but this file treats the
    path segment as untrusted everywhere, so escape it before it lands in the page."""
    return ('"' + value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("<", "\\u003c").replace("/", "\\/") + '"')


def _terminal_page(name: str) -> str:
    """The console page: xterm, self-hosted from the portal's static assets (no CDN, CSP
    stays strict), talking to the exec bridge below. Nothing agent-specific is baked in
    except the instance name, which selects the socket the browser opens."""
    slug = _js_string(name)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent console — {name}</title>
<link rel="stylesheet" href="/portal/static/xterm.min.css">
<style>
  html, body {{ margin: 0; height: 100%; background: #0b0e14; }}
  #term {{ position: absolute; inset: 0; padding: 6px; }}
  #note {{ position: absolute; left: 0; right: 0; bottom: 0; font: 13px/1.5 ui-monospace,
           monospace; color: #d7dae0; background: #1b2130; padding: 6px 10px; display: none; }}
</style>
</head>
<body>
<div id="term"></div>
<div id="note"></div>
<script src="/portal/static/xterm.min.js"></script>
<script src="/portal/static/xterm-addon-fit.min.js"></script>
<script>
(function() {{
  var NAME = {slug};
  var term = new Terminal({{ cursorBlink: true, fontFamily: "ui-monospace, monospace",
                            fontSize: 14, theme: {{ background: "#0b0e14" }} }});
  var fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById("term"));
  fit.fit();
  var note = document.getElementById("note");
  function say(msg) {{ note.textContent = msg; note.style.display = "block"; }}

  var scheme = location.protocol === "https:" ? "wss" : "ws";
  var ws = new WebSocket(scheme + "://" + location.host + "/agents/" + NAME + "/ws");
  ws.binaryType = "arraybuffer";

  function sendResize() {{
    fit.fit();
    if (ws.readyState === WebSocket.OPEN) {{
      ws.send(JSON.stringify({{ type: "resize", cols: term.cols, rows: term.rows }}));
    }}
  }}
  ws.onopen = function() {{ sendResize(); term.focus(); }};
  ws.onmessage = function(ev) {{
    if (typeof ev.data === "string") term.write(ev.data);
    else term.write(new Uint8Array(ev.data));
  }};
  ws.onclose = function() {{ say("Console session ended. Reload this tab to reconnect — the agent keeps running."); }};
  ws.onerror = function() {{ say("Console connection error. Reload to reconnect."); }};
  term.onData(function(d) {{
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({{ type: "stdin", data: d }}));
  }});
  window.addEventListener("resize", sendResize);
}})();
</script>
</body>
</html>"""


@router.get("/agents/{name}", include_in_schema=False)
async def agent_console_redirect(name: str, user: str = Depends(require_user)):
    # Trailing slash so the page is a stable base, same as /portal and /workshop.
    return RedirectResponse(f"/agents/{name}/", status_code=307)


@router.get("/agents/{name}/", include_in_schema=False)
async def agent_console_page(name: str, user: str = Depends(require_user)):
    """The terminal page for the caller's own agent. Resolving the target here (not only on
    the socket) means a non-owner or a stopped agent gets the real 404/409 as a page, rather
    than a blank terminal that fails silently on connect."""
    await agents.console_target(user, name)
    return HTMLResponse(_terminal_page(name))


@router.websocket("/agents/{name}/ws")
async def agent_console_ws(ws: WebSocket, name: str):
    """Bridge the browser terminal to `hermes --tui` inside the caller's own pod.

    It ATTACHES: the exec starts a client that shares the daemon's on-disk session and
    leaves the daemon untouched when the socket closes. All owner-scoping is in
    `console_target`; this function can only exec the pod that function named.
    """
    try:
        user = require_user(ws)                 # same predicate; WebSocket carries .client
        target = await agents.console_target(user, name)
    except Exception:
        # 1008 policy violation, and nothing about whose agent it is or whether it exists —
        # the same safe direction console_target answers 404/409 in.
        await ws.close(code=1008)
        return

    url = _exec_url(target["namespace"], target["pod"], target["container"],
                    target["command"])
    import websockets

    # websockets renamed this header kwarg between its two client implementations and
    # connect() builds lazily, so a wrong name never surfaces at the call site. Pick it from
    # the signature — the same fix workshop.py/agent proxy used.
    header_kw = (
        "additional_headers"
        if "additional_headers" in inspect.signature(websockets.connect).parameters
        else "extra_headers"
    )

    await ws.accept()
    try:
        async with websockets.connect(
            url,
            subprotocols=[_EXEC_SUBPROTOCOL],
            ssl=_ssl_context(),
            max_size=None,
            open_timeout=15,
            **{header_kw: {"Authorization": f"Bearer {agent_usage._token()}"}},
        ) as upstream:

            async def to_pod():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    text = msg.get("text")
                    if text is None:
                        continue
                    try:
                        frame = json.loads(text)
                    except ValueError:
                        continue
                    kind = frame.get("type")
                    if kind == "stdin":
                        await upstream.send(bytes([_STDIN]) + frame["data"].encode())
                    elif kind == "resize":
                        size = json.dumps({"Width": int(frame["cols"]),
                                           "Height": int(frame["rows"])}).encode()
                        await upstream.send(bytes([_RESIZE]) + size)

            async def to_browser():
                async for msg in upstream:
                    if isinstance(msg, str):
                        msg = msg.encode()
                    if not msg:
                        continue
                    channel, payload = msg[0], msg[1:]
                    if channel in (_STDOUT, _STDERR) and payload:
                        await ws.send_bytes(payload)
                    # channel 3 (error) carries the exec's terminating status; the socket
                    # closing is what the page reacts to, so it needs no separate surfacing.

            done, pending = await asyncio.wait(
                [asyncio.create_task(to_pod()), asyncio.create_task(to_browser())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception as exc:  # noqa: BLE001
        # Either side going away is the normal end of a console session. Anything else has
        # cost the user their terminal, and this warning is the only place it becomes
        # visible — a silent pass here is what made failures look like a hung agent.
        _log.warning("console exec for %s/%s ended: %s: %s",
                     user, name, type(exc).__name__, exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
