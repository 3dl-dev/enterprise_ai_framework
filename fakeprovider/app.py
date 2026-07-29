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
from fastapi.responses import StreamingResponse

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


@openai_router.post("/chat/completions")
async def openai_chat(request: Request):
    body = await request.json()
    model = body.get("model", "fake-gpt-large")
    prompt = extract_prompt(body.get("messages", []))
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
        return {"prompt_digest": prompt_digest(prompt), "calls": CALLS_BY_PROMPT[prompt_digest(prompt)]}
    return {"total": sum(CALLS_BY_PROMPT.values()), "by_prompt": dict(CALLS_BY_PROMPT)}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "fake-provider"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
