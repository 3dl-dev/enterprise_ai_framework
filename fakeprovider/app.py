"""Fake upstream provider.

Stands in for Anthropic and OpenAI so the bundle comes up and is testable end to end
with no provider account and no spend (scope item 8).

It speaks enough of both wire formats for the gateway to treat it as a real upstream:
non-streaming and streaming chat completions, with token counts in the shape each
format reports them. Responses are deterministic given the request, so tests can
assert on them.
"""

import hashlib
import json
import os
import re
import time

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="fake-provider")

# Deterministic pseudo-token accounting. The gateway meters what the upstream reports,
# so these numbers flow all the way through to the ledger and the tests assert on them.
CHARS_PER_TOKEN = 4

# --------------------------------------------------------------------------
# Prompt capture — a test seam, not a feature. reply_text() only ever hands a
# caller a hash of what it received, which is enough to prove two requests
# differed but not enough to prove WHAT changed. enterpriseaiframework-6ff
# needs the stronger claim ("this exact secret string, which lives nowhere but
# a SKILL.md body, reached the upstream request") without guessing at
# LibreChat's internal message-join format. So every request's fully
# extracted prompt is kept in a bounded ring buffer, inspectable over
# /debug/prompts by any test that knows a substring (a nonce) to search for.
# Nothing about the deterministic reply_text()/reply-digest contract other
# tests already rely on changes.
# --------------------------------------------------------------------------
_CAPTURED_PROMPTS: list[dict] = []
_CAPTURE_LIMIT = 500


def _capture_prompt(model: str, prompt: str) -> None:
    _CAPTURED_PROMPTS.append({"model": model, "prompt": prompt})
    if len(_CAPTURED_PROMPTS) > _CAPTURE_LIMIT:
        del _CAPTURED_PROMPTS[: len(_CAPTURED_PROMPTS) - _CAPTURE_LIMIT]


@app.get("/debug/prompts")
async def debug_prompts(contains: str | None = None):
    """Recent extracted prompts, optionally filtered to ones containing `contains`.

    `contains` should be a nonce unique to the test's own turn — the buffer is
    shared across every caller hitting this process, including other tests
    running in the same suite.
    """
    if contains:
        return [c for c in _CAPTURED_PROMPTS if contains in c["prompt"]]
    return _CAPTURED_PROMPTS


def count_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


# --------------------------------------------------------------------------
# Code-execution tool calling (enterpriseaiframework-082)
#
# fakeprovider cannot run arbitrary code, so no in-bundle test could otherwise prove a
# model-driven code execution — every other test in this suite proves "the gateway/chat
# wiring works", never "an answer arrived that fakeprovider could not have guessed".
#
# The fix is an inversion, not a workaround: fakeprovider learns to turn a marker in the
# user's own message into a real `bash_tool` call (LibreChat's actual code-execution tool
# name, Constants.BASH_TOOL in @librechat/agents), and to relay whatever the REAL sandbox
# sends back verbatim as the final reply — while staying deliberately unable to compute
# the answer itself. That is what makes a correct sha256 (or any other value the marker's
# command produces) appearing in a persisted conversation evidence of execution having
# actually happened, rather than a plausible string a stub or a real model guessed.
# --------------------------------------------------------------------------

EXECUTE_MARKER_RE = re.compile(r"EXECUTE_BASH:(.*)", re.DOTALL)

BASH_TOOL_NAME = "bash_tool"

# --------------------------------------------------------------------------
# MCP tool calling (enterpriseaiframework-471, gap 1)
#
# The adversary's finding: done-condition (1) ("a tool invoked in chat is invocable in
# the terminal agent and behaves the same") was proven on the terminal side by actually
# running opencode against a real mcp-echo container, but on the chat side by nothing
# more than two config files naming the same URL — a passing test that would not notice
# a broken LibreChat MCP client, a namespacing regression, or a routing break between
# the chat pod and mcp-echo.
#
# Same inversion as EXECUTE_BASH/bash_tool above, for the same reason: fakeprovider
# cannot itself be mcp-echo (a real MCP server, reached over streamable-http, is a
# separate process this stub does not speak the protocol of), so it only ever decides
# WHETHER to ask the agents framework to call mcp-echo's `echo` tool — never what that
# tool returns. LibreChat's agents framework does the real MCP round trip and hands the
# real result back as a `role: tool` message, which the find_tool_result()/relay branch
# below already forwards verbatim, unmodified by which tool produced it.
#
# `echo_mcp_echo` is not invented here: it is LibreChat's own namespacing of an MCP
# tool (`<tool>_mcp_<server>`), reverse-engineered and recorded in
# tests-live/test_mcp_echo.py (kept only as a live cluster reference now that its
# `_send_message` is dead against the pinned v0.8.7 wire protocol — see
# enterpriseaiframework-a70).
# --------------------------------------------------------------------------

MCP_ECHO_MARKER_RE = re.compile(r"CALL_MCP_ECHO:(.*)", re.DOTALL)

MCP_ECHO_TOOL_NAME = "echo_mcp_echo"


def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        # OpenAI/Anthropic content-block shape.
        content = "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type", "text") == "text"
        )
    return str(content)


def find_tool_result(messages: list) -> str | None:
    """The content of the most recent tool-role message, if this turn already carries
    a `bash_tool` result to relay. None on the turn that should trigger the call."""
    for m in messages or []:
        if m.get("role") == "tool":
            return _message_text(m)
    return None


def find_exec_command(messages: list) -> str | None:
    """The command from a user-authored `EXECUTE_BASH:<command>` marker, most recent
    user message first. None if no message carries one."""
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        match = EXECUTE_MARKER_RE.search(_message_text(m))
        if match:
            return match.group(1).strip()
    return None


def find_mcp_echo_arg(messages: list) -> str | None:
    """The text argument from a user-authored `CALL_MCP_ECHO:<text>` marker, most recent
    user message first. None if no message carries one."""
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        match = MCP_ECHO_MARKER_RE.search(_message_text(m))
        if match:
            return match.group(1).strip()
    return None


def tool_call_id(command: str) -> str:
    return "call_" + hashlib.sha256(command.encode()).hexdigest()[:24]


def reply_text(prompt: str, model: str) -> str:
    """Deterministic reply. Same prompt + model always yields the same bytes."""
    digest = hashlib.sha256(f"{model}\x00{prompt}".encode()).hexdigest()[:12]
    return f"[fake-provider {model}] ack {digest}"


def extract_prompt(messages: list) -> str:
    parts = []
    for m in messages or []:
        content = m.get("content", "")
        if isinstance(content, list):
            # Anthropic-style content blocks
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        parts.append(str(content))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# OpenAI-compatible surface
# --------------------------------------------------------------------------

openai_router = APIRouter()


@openai_router.get("/models")
async def openai_models():
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "fake-provider"}
            for m in ("fake-gpt-large", "fake-gpt-small")
        ],
    }


def _tool_call_completion(
    cid: str, model: str, created: int, tool_name: str, tool_args: dict
) -> dict:
    """A single non-streamed tool-call completion requesting `tool_name(**tool_args)`.
    Generic over the tool identity — used for both `bash_tool` (enterpriseaiframework-082)
    and `echo_mcp_echo` (enterpriseaiframework-471, gap 1); fakeprovider never computes
    either tool's answer, only decides to ask for the call.
    """
    call_id = tool_call_id(tool_name + json.dumps(tool_args, sort_keys=True))
    arguments = json.dumps(tool_args)
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": count_tokens(json.dumps(tool_args)),
            "completion_tokens": count_tokens(arguments),
            "total_tokens": count_tokens(json.dumps(tool_args)) + count_tokens(arguments),
        },
    }


def _tool_call_stream(cid: str, model: str, created: int, tool_name: str, tool_args: dict):
    """The streamed twin of `_tool_call_completion` — opencode's ai-sdk client and
    LibreChat's SSE upstream parsing both require `stream: true`, so the non-streamed
    shape above is never actually reachable in this deployment; kept for callers/tests
    that post `stream: false` directly."""
    call_id = tool_call_id(tool_name + json.dumps(tool_args, sort_keys=True))
    arguments = json.dumps(tool_args)
    prompt_str = json.dumps(tool_args)

    def chunk(delta: dict, finish=None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    async def stream():
        yield chunk({"role": "assistant", "content": None})
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": ""},
                    }
                ]
            }
        )
        yield chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]}
        )
        final = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {
                "prompt_tokens": count_tokens(prompt_str),
                "completion_tokens": count_tokens(arguments),
                "total_tokens": count_tokens(prompt_str) + count_tokens(arguments),
            },
        }
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@openai_router.post("/chat/completions")
async def openai_chat(request: Request):
    body = await request.json()
    model = body.get("model", "fake-gpt-large")
    messages = body.get("messages", [])
    created = int(time.time())

    # Capture before any early return, so a tool-call or relay turn is recorded too
    # (enterpriseaiframework-6ff's skill tests read this to see what LibreChat sent).
    _capture_prompt(model, extract_prompt(messages))

    # Code-execution tool calling takes priority over the deterministic-ack default
    # (enterpriseaiframework-082). Two states, mutually exclusive: either this turn
    # already carries the real sandbox's result to relay, or it carries a fresh
    # EXECUTE_BASH: marker to turn into a tool call. Neither path lets fakeprovider
    # compute an answer itself.
    tool_result = find_tool_result(messages)
    if tool_result is not None:
        cid = "fakecmpl-" + hashlib.sha256(tool_result.encode()).hexdigest()[:16]
        prompt_tokens = count_tokens(tool_result)
        completion_tokens = count_tokens(tool_result)
        if not body.get("stream"):
            return {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        # Relayed VERBATIM — this is the real sandbox's own output,
                        # not anything fakeprovider produced or altered.
                        "message": {"role": "assistant", "content": tool_result},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }

        def relay_chunk(delta: dict, finish=None) -> str:
            payload = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload)}\n\n"

        async def relay_stream():
            yield relay_chunk({"role": "assistant", "content": ""})
            for word in tool_result.split(" "):
                yield relay_chunk({"content": word + " "})
            final = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(relay_stream(), media_type="text/event-stream")

    exec_command = find_exec_command(messages)
    if exec_command is not None:
        cid = "fakecmpl-" + hashlib.sha256(exec_command.encode()).hexdigest()[:16]
        if not body.get("stream"):
            return _tool_call_completion(cid, model, created, BASH_TOOL_NAME, {"command": exec_command})
        return _tool_call_stream(cid, model, created, BASH_TOOL_NAME, {"command": exec_command})

    mcp_echo_arg = find_mcp_echo_arg(messages)
    if mcp_echo_arg is not None:
        cid = "fakecmpl-" + hashlib.sha256(("mcp-echo:" + mcp_echo_arg).encode()).hexdigest()[:16]
        if not body.get("stream"):
            return _tool_call_completion(cid, model, created, MCP_ECHO_TOOL_NAME, {"text": mcp_echo_arg})
        return _tool_call_stream(cid, model, created, MCP_ECHO_TOOL_NAME, {"text": mcp_echo_arg})

    prompt = extract_prompt(messages)
    text = reply_text(prompt, model)
    prompt_tokens = count_tokens(prompt)
    completion_tokens = count_tokens(text)
    cid = "fakecmpl-" + hashlib.sha256(prompt.encode()).hexdigest()[:16]

    if not body.get("stream"):
        return {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def chunk(delta: dict, finish=None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    async def stream():
        yield chunk({"role": "assistant", "content": ""})
        # Emit word by word so a client can observe real incremental delivery.
        for word in text.split(" "):
            yield chunk({"content": word + " "})
        final = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Anthropic-native surface
# --------------------------------------------------------------------------

anthropic_router = APIRouter()


@anthropic_router.post("/messages")
async def anthropic_messages(request: Request):
    body = await request.json()
    model = body.get("model", "fake-claude-large")
    prompt = extract_prompt(body.get("messages", []))
    if body.get("system"):
        prompt = str(body["system"]) + "\n" + prompt
    _capture_prompt(model, prompt)
    text = reply_text(prompt, model)
    input_tokens = count_tokens(prompt)
    output_tokens = count_tokens(text)
    mid = "fakemsg-" + hashlib.sha256(prompt.encode()).hexdigest()[:16]

    if not body.get("stream"):
        return {
            "id": mid,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    async def stream():
        yield sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": mid,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            },
        )
        yield sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        for word in text.split(" "):
            yield sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": word + " "},
                },
            )
        yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        )
        yield sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(stream(), media_type="text/event-stream")


app.include_router(openai_router, prefix="/v1")
app.include_router(anthropic_router, prefix="/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "fake-provider"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
