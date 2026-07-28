"""Drive a workspace the way a person does: log in, then type into the terminal.

This project has been burned by tests that pass while the product is broken — a scripted
HTTP client once proved login worked while every real login failed. So nothing here
asserts from configuration. The login is a real authorization-code exchange against
Keycloak through oauth2-proxy, and the shell is driven over ttyd's actual websocket
protocol with the session cookie that login produced. If any part of the chain is wrong,
these functions hang or raise rather than quietly succeeding.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time

import httpx

NS = "enterprise-ai"

_FORM_ACTION = re.compile(
    r'<form[^>]*\bid="kc-form-login"[^>]*\baction="([^"]+)"', re.IGNORECASE | re.DOTALL
)
_FORM_ACTION_FALLBACK = re.compile(
    r'<form[^>]*\baction="([^"]+)"[^>]*\bmethod="post"', re.IGNORECASE | re.DOTALL
)

# ttyd's wire protocol (1.7). One-byte opcode, then the payload.
_INPUT = "0"
_RESIZE = "1"
_OUT = ord("0")


def secret(name: str, key: str) -> str:
    """Read a value out of a namespace Secret via kubectl.

    Deliberately not from bundle/.env: the workspace credentials are minted into the
    cluster and never written to the repo, and a test that reads them from a file would
    pass against a stale copy.
    """
    out = subprocess.run(
        ["kubectl", "-n", NS, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        capture_output=True, text=True, check=True,
    ).stdout
    import base64

    return base64.b64decode(out).decode()


def login(base_url: str, username: str, password: str) -> httpx.Client:
    """Return a client holding an authenticated oauth2-proxy session for `base_url`.

    Verification is off because the identity provider is reached over its public name
    while the test client may resolve it differently; the point being proved is that the
    authorization-code exchange completes, not that we trust our own cert chain.
    """
    client = httpx.Client(verify=False, follow_redirects=True, timeout=60.0)

    start = client.get(f"{base_url}/")
    assert start.status_code == 200, f"workspace returned {start.status_code}"
    assert "kc-form-login" in start.text or 'name="password"' in start.text, (
        f"no identity-provider login form; landed on {start.url}\n{start.text[:400]}"
    )

    match = _FORM_ACTION.search(start.text) or _FORM_ACTION_FALLBACK.search(start.text)
    assert match, "could not find the login form action"
    action = match.group(1).replace("&amp;", "&")

    done = client.post(
        action,
        data={"username": username, "password": password, "credentialId": ""},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return client


def cookie_header(client: httpx.Client) -> str:
    return "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)


def run_in_terminal(base_url: str, client: httpx.Client, commands: list[str],
                    settle: float = 3.0, timeout: float = 240.0) -> str:
    """Type `commands` into the browser terminal and return everything it printed.

    Goes through /ws — the same websocket the browser opens — so a proxy that forwards
    HTML but drops the upgrade shows up as a failure here instead of as a blank terminal
    for the user.
    """
    return asyncio.run(_drive(base_url, cookie_header(client), commands, settle, timeout))


async def _drive(base_url: str, cookie: str, commands: list[str],
                 settle: float, timeout: float) -> str:
    import websockets

    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    transcript: list[str] = []

    async with websockets.connect(
        ws_url,
        additional_headers={"Cookie": cookie},
        subprotocols=["tty"],
        max_size=None,
        open_timeout=30,
    ) as ws:
        # ttyd's handshake. Without this first frame the server never starts the pty and
        # the connection just sits there, which is indistinguishable from a hung shell.
        await ws.send(json.dumps({"AuthToken": "", "columns": 200, "rows": 50}))
        await ws.send(_RESIZE + json.dumps({"columns": 200, "rows": 50}))

        async def pump() -> None:
            async for message in ws:
                if isinstance(message, str):
                    message = message.encode()
                if message and message[0] == _OUT:
                    transcript.append(message[1:].decode("utf-8", "replace"))

        reader = asyncio.create_task(pump())
        await asyncio.sleep(2.0)

        deadline = time.monotonic() + timeout
        for command in commands:
            await ws.send(_INPUT + command + "\n")
            # Wait for the terminal to go quiet rather than for a fixed duration: aider
            # takes tens of seconds to answer and `make test` takes one, and a fixed
            # sleep either wastes minutes or truncates the interesting part.
            last = len(transcript)
            quiet_since = time.monotonic()
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                if len(transcript) != last:
                    last = len(transcript)
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since > settle:
                    break

        reader.cancel()

    return "".join(transcript)
