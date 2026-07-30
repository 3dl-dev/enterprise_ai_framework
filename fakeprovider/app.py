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
import time
from collections import defaultdict

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="fake-provider")

# Deterministic pseudo-token accounting. The gateway meters what the upstream reports,
# so these numbers flow all the way through to the ledger and the tests assert on them.
CHARS_PER_TOKEN = 4


# HOW A TEST KNOWS A REQUEST WAS SERVED FROM THE GATEWAY'S CACHE
#
# It cannot use the response. Every field this provider returns — the body text, the
# completion id, the token counts — is a pure function of (model, prompt), precisely so
# tests can assert on them. A cached reply and a freshly generated one are therefore
# byte-identical, and "the id matched, so it was a cache hit" proves nothing at all. A
# probe written that way passes whether the cache works or not.
#
# The one observable that separates the two is whether this process was called. So it
# counts, per prompt, and reports the counts. A test that asserts "over-budget requests
# are refused even when the answer is cached" has to establish the answer really was
# cached, or it is asserting nothing; this is the ground truth it establishes it with.
#
# Test-fixture scope only. This service stands in for a provider account and never runs
# in a real deployment — see docs/design/dogfood-scope.md item 8. It is unauthenticated
# for the same reason the rest of it is.
CALLS_BY_PROMPT: defaultdict[str, int] = defaultdict(int)

# WHY THE COUNTER SHIPS A BOOT ID WITH IT
#
# CALLS_BY_PROMPT lives in this process and nowhere else, so anything that restarts this
# container silently resets it to zero — `restart: unless-stopped` after an OOM, a
# `compose up --build` while a suite is running, a health-check restart. Every test that
# reads it then reads a smaller number than the truth, and the direction that goes wrong is
# the dangerous one: "the second call did not reach the provider, so it was a cache hit" is
# exactly what a zeroed counter looks like. The cache test would pass with the cache
# switched off.
#
# So the counter is published with the identity of the process that counted. A test takes
# the boot id before its measurement and asserts it is the same one afterwards; if this
# process restarted in between, the test says so instead of quietly believing the count.
BOOT_ID = hashlib.sha256(f"{os.getpid()}\x00{time.time_ns()}".encode()).hexdigest()[:16]


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def record_call(prompt: str) -> None:
    CALLS_BY_PROMPT[prompt_digest(prompt)] += 1


def count_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


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


# A prompt containing this marker makes the upstream fail with a 500.
#
# There are three ways a request can end without the caller getting an answer, and they
# take three different code paths through the gateway, so "a failed request is not billed"
# has to be asked of each one separately:
#
#   1. refused at the budget          -> raised in user_api_key_auth, before the router
#   2. refused at the key's model list -> also before the router
#   3. the upstream itself fails       -> past the router, onto the FAILURE CALLBACK,
#                                         which is pointed at the same ledger
#
# Only the third can plausibly write a spend row, and it was the one no test could reach,
# because a stand-in provider that always succeeds cannot produce it. Injection is by
# prompt marker rather than a header: LiteLLM forwards the message body verbatim but does
# not pass arbitrary client headers through to the upstream.
FAIL_MARKER = "__fakeprovider_fail_500__"


@openai_router.post("/chat/completions")
async def openai_chat(request: Request):
    body = await request.json()
    model = body.get("model", "fake-gpt-large")
    prompt = extract_prompt(body.get("messages", []))
    if FAIL_MARKER in prompt:
        # Recorded before failing, so a test can still tell "the upstream was called and
        # blew up" apart from "the gateway never called the upstream at all".
        record_call(prompt)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "fake-provider was asked to fail",
                               "type": "server_error"}},
        )
    text = reply_text(prompt, model)
    prompt_tokens = count_tokens(prompt)
    completion_tokens = count_tokens(text)
    created = int(time.time())
    cid = "fakecmpl-" + prompt_digest(prompt)
    record_call(prompt)

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
    text = reply_text(prompt, model)
    input_tokens = count_tokens(prompt)
    output_tokens = count_tokens(text)
    mid = "fakemsg-" + prompt_digest(prompt)
    record_call(prompt)

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


@app.get("/debug/calls")
async def debug_calls(prompt: str | None = None):
    """How many times this provider was actually asked to generate.

    `prompt` is the exact prompt text a test sent; the count comes back for that prompt
    alone. Without it, every prompt this process has seen. See CALLS_BY_PROMPT above for
    why a test cannot get this from the response body.
    """
    if prompt is not None:
        return {"prompt_digest": prompt_digest(prompt),
                "calls": CALLS_BY_PROMPT[prompt_digest(prompt)],
                "boot_id": BOOT_ID}
    return {"total": sum(CALLS_BY_PROMPT.values()), "by_prompt": dict(CALLS_BY_PROMPT),
            "boot_id": BOOT_ID}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "fake-provider"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
