"""fakeprovider's subagent-tool-calling inversion (enterpriseaiframework-00d).

Mirrors tests/test_fakeprovider_mcp_echo.py's structure for the same reason: proves the
marker/relay logic that tests/test_scope_items.py's real, running-bundle chat turn
depends on, in isolation and without needing the stack up, so a failure in fakeprovider's
own request parsing is diagnosed here rather than surfacing as a confusing timeout in the
full integration test.

WHAT THIS DOES AND DOES NOT PROVE: fakeprovider stands in for the MODEL, never for
LibreChat's own SubagentExecutor (a real, in-process child-graph spawn — there is no
second network peer to stand in for here, unlike mcp-echo). This file proves fakeprovider
asks for the real `subagent` tool (Constants.SUBAGENT in @librechat/agents) with the
arguments SubagentToolSchema actually requires, and relays whatever tool-role result comes
back verbatim. It does not and cannot prove a child graph actually ran, was billed, or was
attributed to anyone — that is the real, running-bundle chat turn in
tests/test_scope_items.py, which is the only place two genuinely distinct HTTP
completions (parent + child) can be observed landing on the gateway's own ledger.
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
    spec = importlib.util.spec_from_file_location("fakeprovider_app_subagent", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fakeprovider_app_subagent"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fp():
    return _load_app()


@pytest.fixture(scope="module")
def client(fp):
    return TestClient(fp.app)


def test_a_call_subagent_marker_triggers_the_real_subagent_tool_not_invented(client):
    """The tool name must be LibreChat's own `subagent` (Constants.SUBAGENT in
    @librechat/agents) with the exact argument shape SubagentToolSchema requires
    (`description`, `subagent_type`) — an invented name or shape would never route to a
    real delegation at all, and the failure would look like "the model didn't call a
    tool", blaming the wrong layer.
    """
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "messages": [{"role": "user", "content": "CALL_SUBAGENT:self:do the thing"}],
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
    assert tool_calls[0]["function"]["name"] == "subagent"
    arguments = json.loads(tool_calls[0]["function"]["arguments"])
    assert arguments == {"description": "do the thing", "subagent_type": "self"}


def test_the_subagent_tool_call_id_is_deterministic_for_the_same_argument(client):
    body = {
        "model": "fake-gpt-large",
        "messages": [{"role": "user", "content": "CALL_SUBAGENT:self:same task"}],
    }
    first = client.post("/v1/chat/completions", json=body).json()
    second = client.post("/v1/chat/completions", json=body).json()
    id_a = first["choices"][0]["message"]["tool_calls"][0]["id"]
    id_b = second["choices"][0]["message"]["tool_calls"][0]["id"]
    assert id_a == id_b


def test_a_subagent_tool_result_is_relayed_verbatim_not_recomputed(client):
    """Once the subagent's real final answer is in the conversation (a `role: tool`
    message — the shape LibreChat's SubagentTool node appends after the child graph
    actually completes), fakeprovider must echo it byte-for-byte. Generalized off
    bash_tool/echo_mcp_echo's identical relay branch; nothing subagent-specific is added
    here because none is needed.
    """
    real_subagent_output = "the child agent's own real final answer, not guessed"
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "messages": [
                {"role": "user", "content": "CALL_SUBAGENT:self:whatever"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {
                                "name": "subagent",
                                "arguments": json.dumps(
                                    {"description": "whatever", "subagent_type": "self"}
                                ),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_xyz", "content": real_subagent_output},
            ],
        },
    )
    assert r.status_code == 200
    message = r.json()["choices"][0]["message"]
    assert r.json()["choices"][0]["finish_reason"] == "stop"
    assert message["content"] == real_subagent_output
    assert "tool_calls" not in message


def test_streaming_subagent_call_carries_the_same_argument(client):
    """The agents framework may request a streaming completion — every real chat turn in
    this bundle does. tool_calls must reassemble to the same tool name and arguments as
    the non-streaming path."""
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "stream": True,
            "messages": [{"role": "user", "content": "CALL_SUBAGENT:self:stream task"}],
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
    assert names == ["subagent"]
    arguments = "".join(
        d["function"].get("arguments", "") for d in tool_call_deltas
    )
    assert json.loads(arguments) == {"description": "stream task", "subagent_type": "self"}
    finish_reasons = [e["choices"][0].get("finish_reason") for e in events]
    assert "tool_calls" in finish_reasons


def test_a_plain_message_and_other_markers_are_still_untouched(client):
    """The subagent addition must not shadow the existing deterministic-ack default or
    the other markers — exercised elsewhere, this just pins that adding a fifth branch
    did not reorder or break the earlier ones."""
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


def test_a_malformed_marker_with_no_colon_is_not_matched(client):
    """`CALL_SUBAGENT:` without a second colon-separated task has no `subagent_type` —
    it must fall through to the deterministic-ack default rather than crash or invent a
    type."""
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "messages": [{"role": "user", "content": "CALL_SUBAGENT:nocolonhere"}],
        },
    )
    assert r.status_code == 200
    message = r.json()["choices"][0]["message"]
    assert message["content"].startswith("[fake-provider fake-gpt-large] ack ")
    assert "tool_calls" not in message
