"""fakeprovider's MCP-tool-calling inversion (enterpriseaiframework-471, gap 1).

Mirrors tests/test_fakeprovider_execute_code.py's structure for the same reason: this
proves the marker/relay logic that tests/test_workspace_mcp_parity.py's real, running-bundle
chat turn depends on, in isolation and without needing the stack up, so a failure in
fakeprovider's own request parsing is diagnosed here rather than surfacing as a confusing
timeout in the full integration test.

WHAT THIS DOES AND DOES NOT PROVE: fakeprovider stands in for the MODEL, never for
mcp-echo (a real MCP server reached over streamable-http, a different protocol
entirely). This file proves fakeprovider asks for the real, correctly-namespaced tool
(`echo_mcp_echo`, LibreChat's own `<tool>_mcp_<server>` convention) and relays whatever
comes back verbatim. It does not and cannot prove mcp-echo itself runs — that is
test_workspace_mcp_parity.py's real chat-turn test, against the real running bundle.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "fakeprovider" / "app.py"


def _load_app():
    """Import fakeprovider/app.py by path — see test_fakeprovider_execute_code.py's
    identical helper for why (it is not on pythonpath and not a package)."""
    spec = importlib.util.spec_from_file_location("fakeprovider_app_mcp", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fakeprovider_app_mcp"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fp():
    return _load_app()


@pytest.fixture(scope="module")
def client(fp):
    return TestClient(fp.app)


def test_a_call_mcp_echo_marker_triggers_the_real_namespaced_tool_not_invented(client):
    """The tool name must be LibreChat's own MCP namespacing convention
    (`<tool>_mcp_<server>` — reverse-engineered and recorded in
    tests-live/test_mcp_echo.py, whose `_send_message` is otherwise dead against the
    pinned v0.8.7 wire protocol per enterpriseaiframework-a70). An invented name would
    never route to mcp-echo at all, and the failure would look like "the model didn't
    call a tool", blaming the wrong layer.
    """
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "messages": [{"role": "user", "content": "CALL_MCP_ECHO:hello-471"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    message = body["choices"][0]["message"]
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert message["content"] is None
    tool_calls = message["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["type"] == "function"
    assert tool_calls[0]["function"]["name"] == "echo_mcp_echo"
    arguments = json.loads(tool_calls[0]["function"]["arguments"])
    assert arguments == {"text": "hello-471"}


def test_the_mcp_tool_call_id_is_deterministic_for_the_same_argument(client):
    body = {
        "model": "fake-gpt-large",
        "messages": [{"role": "user", "content": "CALL_MCP_ECHO:same-arg"}],
    }
    first = client.post("/v1/chat/completions", json=body).json()
    second = client.post("/v1/chat/completions", json=body).json()
    id_a = first["choices"][0]["message"]["tool_calls"][0]["id"]
    id_b = second["choices"][0]["message"]["tool_calls"][0]["id"]
    assert id_a == id_b


def test_an_mcp_echo_tool_result_is_relayed_verbatim_not_recomputed(client):
    """The core of the inversion, generalized off bash_tool: once mcp-echo's real
    output is in the conversation (a `role: tool` message, the shape LibreChat's agents
    framework appends after actually calling the MCP server), fakeprovider must echo it
    byte-for-byte — the ECHO:<nonce> prefix only mcp-echo's own `echo()` function
    produces (mcp-echo/app.py), never something this stub invents.
    """
    real_mcp_output = "ECHO:only-the-real-mcp-echo-server-knows-this-nonce"
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "messages": [
                {"role": "user", "content": "CALL_MCP_ECHO:whatever"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {"name": "echo_mcp_echo", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_xyz", "content": real_mcp_output},
            ],
        },
    )
    assert r.status_code == 200
    message = r.json()["choices"][0]["message"]
    assert r.json()["choices"][0]["finish_reason"] == "stop"
    assert message["content"] == real_mcp_output
    assert "tool_calls" not in message


def test_streaming_mcp_tool_call_carries_the_same_argument(client):
    """The agents framework may request a streaming completion (opencode's ai-sdk
    client always does). tool_calls must reassemble to the same function name and
    arguments as the non-streaming path — dropping this would silently break the MCP
    tool call only for streamed turns."""
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "stream": True,
            "messages": [{"role": "user", "content": "CALL_MCP_ECHO:stream-me"}],
        },
    ) as r:
        events = []
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            events.append(json.loads(payload))

    tool_call_deltas = [
        d
        for e in events
        for d in (e["choices"][0]["delta"].get("tool_calls") or [])
    ]
    assert tool_call_deltas, f"no tool_calls deltas in stream: {events}"
    names = [d["function"]["name"] for d in tool_call_deltas if "name" in d.get("function", {})]
    assert names == ["echo_mcp_echo"]
    arguments = "".join(
        d["function"].get("arguments", "") for d in tool_call_deltas
    )
    assert json.loads(arguments) == {"text": "stream-me"}
    finish_reasons = [e["choices"][0].get("finish_reason") for e in events]
    assert "tool_calls" in finish_reasons


def test_a_plain_message_and_execute_bash_are_still_untouched(client):
    """The MCP-echo addition must not shadow the existing deterministic-ack default or
    the bash_tool marker — both are exercised elsewhere, this just pins that adding a
    third branch did not reorder or break the first two."""
    r = client.post(
        "/v1/chat/completions",
        json={"model": "fake-gpt-large", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    message = r.json()["choices"][0]["message"]
    assert message["content"].startswith("[fake-provider fake-gpt-large] ack ")
    assert "tool_calls" not in message

    r2 = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "messages": [{"role": "user", "content": 'EXECUTE_BASH:python3 -c "print(1)"'}],
        },
    )
    assert r2.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash_tool"
