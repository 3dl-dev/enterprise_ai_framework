"""enterpriseaiframework-471, done-condition 1: a tool invoked in chat is invocable in
the terminal agent and behaves the same.

WHAT "THE SAME TOOL" MEANS HERE, VERIFIED RATHER THAN ASSUMED: the chat surface's own
proof (tests-live/test_mcp_echo.py) is that a real chat turn against the real cluster
calls mcp-echo's `echo` tool and the reply carries `ECHO:<nonce>` — a string nothing but
that server can produce. This file proves the SAME server, reached the SAME way
(`type: streamable-http`/`remote`, the `/mcp` path, no auth), is invocable from
deploy/workspace/opencode.json — the terminal agent's real config, loaded from disk and
only patched at the two points a bundle-side test cannot reach a cluster DNS name or a
paid model (the mcp URL's host:port, and the model/provider) — by running the REAL
opencode 1.x binary against a REAL mcp-echo container and reading the REAL tool result
back out of the request opencode makes to continue the conversation.

WHY A NONCE, not a fixed string: this file regenerates it every run so a cached or
invented reply cannot pass — the same reasoning tests-live/test_mcp_echo.py's own
docstring gives for the chat-side proof.

WHAT IS MOCKED AND WHY: the model behind opencode's configured provider is
tests/opencode_stub.py, an OpenAI-compatible SSE stub — opencode's ai-sdk client
requires `stream: true`, and a real model is unnecessary and paid. It decides only
WHETHER to ask for a tool call; the tool itself is never mocked — the request opencode
sends back after the fact carries mcp-echo's own real response, and the assertions below
are on that response's exact bytes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from opencode_stub import ChatCompletionStub, free_port, has_tool_result

REPO = Path(__file__).resolve().parent.parent
OPENCODE_CONFIG_PATH = REPO / "deploy/workspace/opencode.json"
LIBRECHAT_YAML = REPO / "bundle/librechat/librechat.yaml"
MCP_ECHO_DIR = REPO / "mcp-echo"

TIMEOUT = 60.0


def _require(binary: str) -> None:
    if shutil.which(binary) is None:
        pytest.fail(
            f"`{binary}` is not on PATH. This test runs the real binary rather than "
            f"mocking it, per the ground-source rule for this item — install it "
            f"(deploy/workspace/Dockerfile documents the pinned `opencode` install "
            f"command) rather than skipping."
        )


@pytest.fixture(scope="module", autouse=True)
def _preconditions():
    _require("docker")
    _require("opencode")


# ---------------------------------------------------------------- static config parity


def test_workspace_config_registers_the_identical_mcp_server_chat_uses():
    """Structural proof that this is the SAME server, not a lookalike: same transport,
    same host:port, same path — read out of both real config files, not restated here.
    """
    workspace_cfg = json.loads(OPENCODE_CONFIG_PATH.read_text())
    echo = workspace_cfg.get("mcp", {}).get("echo")
    assert echo is not None, "deploy/workspace/opencode.json has no mcp.echo entry"
    assert echo["type"] == "remote"
    assert echo["url"] == "http://mcp-echo:8080/mcp"

    chat_cfg = yaml.safe_load(LIBRECHAT_YAML.read_text())
    chat_echo = chat_cfg["mcpServers"]["echo"]
    assert chat_echo["type"] == "streamable-http"
    assert chat_echo["url"] == echo["url"], (
        "the terminal agent and the chat surface point at different URLs for the "
        "server named 'echo' -- they are no longer the same server"
    )


# ---------------------------------------------------------------- real invocation


@pytest.fixture(scope="module")
def mcp_echo_container():
    """The real mcp-echo server (mcp-echo/app.py), built and run standalone -- no
    compose, no cluster. Its tool return value (ECHO:<input>) is the same proof
    artifact tests-live/test_mcp_echo.py uses for the chat side.
    """
    tag = f"eaf-test-mcp-echo-{uuid.uuid4().hex[:8]}"
    name = f"eaf-test-mcp-echo-{uuid.uuid4().hex[:8]}"
    build = subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(MCP_ECHO_DIR)],
        capture_output=True, text=True, timeout=180,
    )
    assert build.returncode == 0, f"docker build mcp-echo failed:\n{build.stderr}"

    port = free_port()
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8080", tag],
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, f"docker run mcp-echo failed: {run.stderr}"

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    up = False
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=2)
            if r.status_code == 200:
                up = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    if not up:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail(f"mcp-echo never became healthy:\n{logs.stdout}\n{logs.stderr}")

    yield {"port": port, "name": name}

    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


def _runtime_config(mcp_port: int, model_base_url: str, tmp_path: Path) -> Path:
    """deploy/workspace/opencode.json, loaded from disk and adapted at exactly the two
    points a bundle-side test cannot reach: the mcp URL's host (mcp-echo is a cluster
    DNS name, unreachable outside it) and the model/provider (a real paid model is
    unnecessary here). Everything else -- the mcp block's shape, the instructions list
    -- is the real, committed file.
    """
    cfg = json.loads(OPENCODE_CONFIG_PATH.read_text())
    cfg.pop("skills", None)  # not exercised by this test; its directory does not exist here
    cfg["provider"] = {
        "stub": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Stub",
            "options": {"baseURL": model_base_url, "apiKey": "sk-stub"},
            "models": {"stub-model": {"name": "Stub Model"}},
        }
    }
    cfg["model"] = "stub/stub-model"
    cfg["mcp"]["echo"]["url"] = f"http://127.0.0.1:{mcp_port}/mcp"

    out = tmp_path / "opencode.json"
    out.write_text(json.dumps(cfg))
    return out


def test_the_terminal_agent_invokes_the_real_echo_tool_and_relays_its_real_result(
    mcp_echo_container, tmp_path
):
    nonce = f"parity-{uuid.uuid4().hex[:12]}"
    expected = f"ECHO:{nonce}"

    def responder(payload: dict):
        messages = payload.get("messages", [])
        if has_tool_result(messages):
            tool_msg = next(m for m in messages if m.get("role") == "tool")
            return {"role": "assistant", "content": f"final: {tool_msg['content']}"}, "stop"
        return (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "echo_echo",
                        "arguments": json.dumps({"text": nonce}),
                    },
                }],
            },
            "tool_calls",
        )

    stub = ChatCompletionStub(responder).start()
    try:
        cfg_path = _runtime_config(mcp_echo_container["port"], stub.base_url, tmp_path)
        result = subprocess.run(
            ["opencode", "run", f"call the echo tool with text {nonce}",
             "--model", "stub/stub-model"],
            env={**_full_env(), "OPENCODE_CONFIG": str(cfg_path)},
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"opencode exited {result.returncode}\nstdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
        assert expected in result.stdout, (
            f"the terminal agent's reply never carried the real mcp-echo output "
            f"({expected!r}); stdout was:\n{result.stdout}"
        )

        # And directly on the wire: the SECOND request opencode made to the model must
        # carry a `role: tool` message whose content is exactly mcp-echo's real output --
        # a string only that server, not this stub, could have produced.
        tool_messages = [
            m for req in stub.requests for m in req.get("messages", [])
            if m.get("role") == "tool"
        ]
        assert tool_messages, "opencode never sent a follow-up request with a tool result"
        assert any(expected in json.dumps(m.get("content")) for m in tool_messages), (
            f"no tool-result message carried {expected!r}: {tool_messages}"
        )
    finally:
        stub.stop()


def _full_env() -> dict:
    return dict(os.environ)
