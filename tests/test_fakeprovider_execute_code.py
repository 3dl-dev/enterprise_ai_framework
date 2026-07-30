"""fakeprovider's code-execution tool-calling inversion (enterpriseaiframework-082).

fakeprovider cannot run arbitrary code — that is precisely the property that makes a
correct answer to something like "sha256 of this nonce" in a real chat conversation
evidence of a genuine sandbox execution, rather than a plausible-looking stub reply. This
file proves that inversion directly against the real FastAPI app (imported in-process,
no network, no docker): given an `EXECUTE_BASH:<command>` marker, fakeprovider must ask
for the REAL `bash_tool` (LibreChat's actual code-execution tool, not an invented name)
rather than answer from its own deterministic-ack logic; and given a tool result already
in the conversation, it must relay that result's exact bytes, never substitute its own.

This does not replace tests/test_scope_items.py's real, running-bundle integration test
for enterpriseaiframework-082 (docker compose up, a real chat turn, a real codeapi
sandbox execution) — it proves the harness piece that test depends on, in isolation and
without needing the stack up, so a failure in the marker/relay logic is diagnosed here
rather than surfacing as a confusing timeout in the full integration test.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "fakeprovider" / "app.py"


def _load_app():
    """Import fakeprovider/app.py by path — it is not on pythonpath (pytest.ini scopes
    that to tests/ and tests-live/), and it is not a package."""
    spec = importlib.util.spec_from_file_location("fakeprovider_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fakeprovider_app"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fp():
    return _load_app()


@pytest.fixture(scope="module")
def client(fp):
    return TestClient(fp.app)


def test_a_plain_message_still_gets_the_deterministic_ack(client):
    """No marker, no tool result: existing behavior (every other test in this suite
    depends on it) must be untouched by the code-execution addition."""
    r = client.post(
        "/v1/chat/completions",
        json={"model": "fake-gpt-large", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    message = r.json()["choices"][0]["message"]
    assert message["content"].startswith("[fake-provider fake-gpt-large] ack ")
    assert "tool_calls" not in message


def test_an_execute_bash_marker_triggers_a_real_bash_tool_call_not_invented(client):
    """The tool name must be LibreChat's actual code-execution tool
    (Constants.BASH_TOOL = 'bash_tool' in @librechat/agents 3.2.46, the version pinned by
    LibreChat v0.8.7) — an invented name would never be routed to the sandbox at all, and
    the failure would look like "the model didn't call a tool", blaming the wrong layer.
    """
    command = 'python3 -c "print(1+1)"'
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "messages": [{"role": "user", "content": f"EXECUTE_BASH:{command}"}],
        },
    )
    assert r.status_code == 200
    message = r.json()["choices"][0]["message"]
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert message["content"] is None
    tool_calls = message["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["type"] == "function"
    assert tool_calls[0]["function"]["name"] == "bash_tool"
    arguments = json.loads(tool_calls[0]["function"]["arguments"])
    assert arguments == {"command": command}


def test_the_tool_call_id_is_deterministic_for_the_same_command(client):
    """LangChain's tool-call loop matches a ToolMessage back to its call by id. A
    non-deterministic id would still work end to end, but this pins the property this
    stub relies on rather than leaving it accidental."""
    body = {
        "model": "fake-gpt-large",
        "messages": [{"role": "user", "content": "EXECUTE_BASH:echo hi"}],
    }
    first = client.post("/v1/chat/completions", json=body).json()
    second = client.post("/v1/chat/completions", json=body).json()
    id_a = first["choices"][0]["message"]["tool_calls"][0]["id"]
    id_b = second["choices"][0]["message"]["tool_calls"][0]["id"]
    assert id_a == id_b


def test_a_tool_result_is_relayed_verbatim_not_recomputed(client):
    """The core of the inversion: once the sandbox's real output is in the conversation
    (a `role: tool` message, the shape LibreChat's agents framework appends), fakeprovider
    must echo it byte-for-byte — never substitute its own deterministic-ack text. A real
    sha256 the sandbox computed is the thing this stub is deliberately unable to produce
    itself, so relaying it unmodified is the only way it can appear in the reply at all.
    """
    real_sandbox_output = "stdout:\n" + hashlib.sha256(b"only-the-real-sandbox-knows-this").hexdigest() + "\n"
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "messages": [
                {"role": "user", "content": "EXECUTE_BASH:python3 -c \"...\""},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "bash_tool", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_abc", "content": real_sandbox_output},
            ],
        },
    )
    assert r.status_code == 200
    message = r.json()["choices"][0]["message"]
    assert r.json()["choices"][0]["finish_reason"] == "stop"
    assert message["content"] == real_sandbox_output
    assert "tool_calls" not in message


def test_streaming_tool_call_carries_the_same_command(client):
    """The agents framework may request a streaming completion. The tool_calls must
    reassemble to the same function name and arguments as the non-streaming path —
    dropping this would silently break code execution only for streamed turns."""
    command = "date +%s"
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fake-gpt-large",
            "stream": True,
            "messages": [{"role": "user", "content": f"EXECUTE_BASH:{command}"}],
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
    assert names == ["bash_tool"]
    arguments = "".join(
        d["function"].get("arguments", "") for d in tool_call_deltas
    )
    assert json.loads(arguments) == {"command": command}
    finish_reasons = [e["choices"][0].get("finish_reason") for e in events]
    assert "tool_calls" in finish_reasons
