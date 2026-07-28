"""Live proof for enterpriseaiframework-12b: chat -> our MCP server -> reply.

DONE condition (from the item): a trivial MCP server registered under `mcpServers` in
bundle/librechat/librechat.yaml, and a real chat message through POST /api/agents/chat
shows that tool being invoked and its output present in the streamed reply — verified
against the CLUSTER, not compose.

Why this does more than "config parsed" or "server reachable" (the failure mode the item
warns about, and the same shape as finding 18 — LibreChat silently falling back to a
hardcoded model list when its virtual key was rejected): the assertion is on the
deterministic string our MCP tool returns (`ECHO:<nonce>`), which the model cannot
produce by guessing — it only appears in the reply if the gateway->agents
framework->MCP server->tool call->tool result->model reply chain executed for real, with
a nonce that is regenerated every run so a cached or fixture reply cannot pass it either.

Kept out of `tests/` (which pytest.ini scopes to the hermetic suite) because it talks to
the real cluster over the public hostname, drives a real model through Forge, and spends
a small amount of real money — the same reason tests-live/test_forge.py lives here.

Run: .venv-test/bin/pytest tests-live/test_mcp_echo.py -v --tb=short -p no:cacheprovider
"""

import json
import urllib.parse
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "bundle"

# tests/oidc_login.py drives the real authorization-code login (browser hand-off to
# Keycloak, form POST, callback). Reused rather than reimplemented — this project has
# already been burned once by a login test that passed against a client that a browser
# would not. Importable because pytest.ini sets `pythonpath = tests` for the whole repo.
import oidc_login  # noqa: E402

# A real browser UA. /api/agents/chat rejects non-browser User-Agents with "Illegal
# request" (dogfood-findings.md finding 19) — this is not a bug to work around, it is a
# precondition to satisfy.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# The cluster's public chat surface — not docker-compose. The bundle and the cluster
# have diverged before (dogfood-findings.md finding 17: an OIDC backchannel that worked
# from a developer's machine and failed for real logins), so this suite always talks to
# the same hostname a real user's browser would.
CHAT_URL = "https://gateway.tailcb6ef9.ts.net:8443"

# The custom endpoint model deployed as the surface default (bundle/librechat/librechat.yaml).
# Routed through Forge (glm-5.2@deepinfra proxies DeepInfra via Forge), so a Forge
# credential problem shows up here as a 500/timeout from the model call, not as broken
# MCP wiring — that distinction matters per the item's own note that Forge sessions can
# expire. `fake-large` cannot be used here: it is a deterministic stub with no
# tool-calling behavior at all, so it cannot exercise this path.
MODEL = "glm-5.2@deepinfra"
ENDPOINT_NAME = "Enterprise AI"
ENDPOINT_TYPE = "custom"


def _env() -> dict:
    out: dict[str, str] = {}
    env_file = BUNDLE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


@pytest.fixture(scope="module")
def creds() -> tuple[str, str]:
    e = _env()
    user, password = e.get("BOOTSTRAP_USER"), e.get("BOOTSTRAP_PASSWORD")
    if not user or not password:
        pytest.fail(
            "BOOTSTRAP_USER / BOOTSTRAP_PASSWORD not configured in bundle/.env — "
            "these are the realm identity provisioned by `make up` / deploy/bin/deploy.sh"
        )
    return user, password


@pytest.fixture(scope="module")
def chat_client(creds) -> httpx.Client:
    """One authenticated session against the cluster's public chat surface."""
    user, password = creds
    client = oidc_login.login(CHAT_URL, user, password)
    client.headers.update({"User-Agent": _BROWSER_UA})

    # The chat surface authenticates its API with a bearer token minted from the
    # refresh cookie, not the session cookie alone (see tests/oidc_login.py::whoami).
    refreshed = client.post(f"{CHAT_URL}/api/auth/refresh")
    assert refreshed.status_code == 200, (
        f"session refresh failed ({refreshed.status_code}): {refreshed.text[:300]}"
    )
    token = refreshed.json().get("token")
    assert token, f"no access token in refresh response: {refreshed.text[:300]}"
    client.headers.update({"Authorization": f"Bearer {token}"})

    yield client
    client.close()


def _send_message(client: httpx.Client, text: str) -> dict:
    """POST a real chat message and return the terminal SSE event as a dict.

    The route is POST /api/agents/chat/<endpoint>, which LibreChat's own client hits
    for "ephemeral agent" turns — i.e. any message sent on a non-agents endpoint with
    ad-hoc tools attached, which is exactly how MCP tools reach a plain custom-endpoint
    conversation (reverse-engineered from librechat-data-provider's createPayload and
    api/server/services/Endpoints/agents/build.js: a non-"agents" endpoint forces
    agent_id to the literal ephemeral id, and model_parameters + ephemeralAgent.mcp
    become the ad-hoc agent's model and tools for this one turn).
    """
    body = {
        "text": text,
        "endpoint": ENDPOINT_NAME,
        "endpointType": ENDPOINT_TYPE,
        "model": MODEL,
        "sender": "User",
        "isCreatedByUser": True,
        "messageId": str(uuid.uuid4()),
        "parentMessageId": "00000000-0000-0000-0000-000000000000",
        "conversationId": None,
        "error": False,
        "isContinued": False,
        "isTemporary": False,
        "isRegenerate": False,
        # This is the field that attaches an MCP server to an otherwise plain
        # custom-endpoint turn. "echo" is the server name under `mcpServers` in
        # librechat.yaml, not a tool name — the tool names it exposes are namespaced
        # as `<tool>_mcp_<server>` (e.g. `echo_mcp_echo`).
        "ephemeralAgent": {"mcp": ["echo"]},
    }
    url = f"{CHAT_URL}/api/agents/chat/{urllib.parse.quote(ENDPOINT_NAME, safe='')}"

    final_event: dict | None = None
    with client.stream(
        "POST", url, json=body, headers={"Accept": "text/event-stream"}, timeout=120,
    ) as r:
        assert r.status_code == 200, (
            f"chat request failed ({r.status_code}); "
            f"if this is a 500 from the model call, check Forge credentials "
            f"(bundle/.env FORGE_API_KEY) before suspecting the MCP wiring: {r.read()[:500]}"
        )
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if event.get("final") is True:
                final_event = event
            # An `event: error` SSE frame carries {"text": "..."} with no "final" key —
            # surface it immediately rather than reporting "no final event" later,
            # which would name the wrong failure.
            if "text" in event and set(event.keys()) == {"text"}:
                pytest.fail(f"chat surface returned an error event: {event['text']}")

    assert final_event is not None, "stream ended with no terminal ('final': true) event"
    return final_event


class TestMCPWiring:
    """enterpriseaiframework-12b: substrate for web-search, skills, code-execution."""

    def test_tool_call_executes_and_its_output_reaches_the_reply(self, chat_client):
        nonce = uuid.uuid4().hex[:12]
        marker = f"ECHO:NONCE-{nonce}"
        prompt = (
            f"Call the echo tool with the text argument set to exactly "
            f"NONCE-{nonce} (no quotes, no extra characters), then reply with "
            f"only the tool's exact output string and nothing else."
        )

        final = _send_message(chat_client, prompt)
        response_message = final.get("responseMessage") or {}
        content = response_message.get("content") or []

        tool_calls = [c for c in content if c.get("type") == "tool_call"]
        assert tool_calls, (
            f"model produced no tool_call content block at all — it answered from "
            f"training instead of calling a tool. Full response content: {content}"
        )

        mcp_calls = [
            c for c in tool_calls
            if "echo" in (c.get("tool_call") or {}).get("name", "")
        ]
        assert mcp_calls, (
            f"a tool was called, but not our MCP echo tool "
            f"(expected a name containing 'echo_mcp_echo'): {tool_calls}"
        )

        tool_call = mcp_calls[0]["tool_call"]
        # The tool's own recorded output, independent of whether the model then
        # bothered to relay it — this is the part that proves the MCP server, not
        # just the model, actually ran.
        assert marker in (tool_call.get("output") or ""), (
            f"our MCP server's recorded tool output does not contain the nonce we "
            f"sent it ({marker!r}); the tool call did not reach our server, or our "
            f"server did not run: {tool_call}"
        )

        # And the marker must also appear in the assistant's visible reply — the
        # other half of the item's done condition ("its output present in the
        # streamed reply"), not just recorded server-side.
        reply_text = "".join(
            c.get("text", "") for c in content if c.get("type") == "text"
        )
        assert marker in reply_text, (
            f"the tool executed and returned {marker!r}, but that text never "
            f"reached the assistant's reply: {reply_text!r}"
        )
