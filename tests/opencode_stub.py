"""A minimal OpenAI-compatible chat-completions stub for driving the REAL opencode
binary in tests, without needing a real paid model behind it.

Why this exists at all: opencode's provider client (@ai-sdk/openai-compatible) requests
`stream: true` unconditionally, so a plain non-streaming JSON response is silently
rejected and the run produces no output. This speaks just enough of the SSE
`chat.completion.chunk` wire shape (the same shape fakeprovider/app.py already speaks
for the chat surface, see `_tool_call_stream` there) for opencode's real tool-call loop
to work end to end: a response can request a tool call, and a second request will then
carry the REAL tool's result if opencode's own MCP client actually executed it.

This stub decides WHAT to answer (via `responder`); it never decides what a real MCP
tool call returns — that still goes to whatever server opencode's config points the tool
at. Using it does not mock away the thing any of these tests are actually proving: the
terminal agent's tool-execution loop, and its instructions-file loading.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from typing import Callable

Responder = Callable[[dict], tuple[dict, str]]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ChatCompletionStub:
    """`responder(payload) -> (message, finish_reason)` where `message` is an OpenAI
    chat message dict (optionally carrying `tool_calls`) and `finish_reason` is
    "stop" or "tool_calls". Every request body is captured verbatim in `.requests`."""

    def __init__(self, responder: Responder):
        self.responder = responder
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        handler = self._make_handler()
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "ChatCompletionStub":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _make_handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body)
                with outer._lock:
                    outer.requests.append(payload)

                message, finish_reason = outer.responder(payload)
                model = payload.get("model", "stub-model")

                def chunk(delta: dict, fr: str | None = None) -> bytes:
                    frame = {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": fr}],
                    }
                    return f"data: {json.dumps(frame)}\n\n".encode()

                chunks = [chunk({"role": "assistant", "content": None})]
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    call = tool_calls[0]
                    chunks.append(chunk({
                        "tool_calls": [{
                            "index": 0, "id": call["id"], "type": "function",
                            "function": {"name": call["function"]["name"], "arguments": ""},
                        }]
                    }))
                    chunks.append(chunk({
                        "tool_calls": [{
                            "index": 0,
                            "function": {"arguments": call["function"]["arguments"]},
                        }]
                    }))
                else:
                    chunks.append(chunk({"content": message.get("content") or ""}))
                chunks.append(chunk({}, fr=finish_reason))
                chunks.append(b"data: [DONE]\n\n")
                data = b"".join(chunks)

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
                pass

        return Handler


def has_tool_result(messages: list[dict]) -> bool:
    return any(m.get("role") == "tool" for m in messages)


def system_text(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            return m["content"]
    return ""
