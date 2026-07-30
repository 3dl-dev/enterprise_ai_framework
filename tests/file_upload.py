"""Upload a file to the chat surface the way its own client does.

enterpriseaiframework-c7c: file search's other half of a real chat turn. The wire shape
below is LibreChat's own (`client/src/hooks/Files/useFileHandling.ts`'s `formData.append`
calls for a message attachment marked `tool_resource: file_search`) — reproduced here
rather than guessed at, because a field this suite invented would only prove that OUR
shape works, not that a real upload does.

`message_file=true` + `tool_resource=file_search` (no `agent_id`) is the "attach this file
to the message I'm about to send" path — `api/server/services/Files/process.js`'s
`processAgentFileUpload` — which is what triggers BOTH halves of the dual-storage write:
the file is saved AND (`tool_resource === EToolResources.file_search`) posted to
`RAG_API_URL/embed` under the AUTHENTICATED uploader's id (`req.user.id`, from the
session — never anything this client sends). The response's `embedded` field is the
server's own claim that the RAG API accepted and indexed it.
"""

import uuid

import httpx

import chat_turn
import oidc_login


def upload_file(client: httpx.Client, chat_url: str, headers: dict, filename: str,
                 content: bytes, mimetype: str = "text/plain",
                 endpoint: str = "Enterprise AI", timeout: float = 60.0) -> dict:
    """POST a message-attachment file marked for file_search. Returns the persisted
    file record LibreChat's own `/api/files` handler responds with (includes
    `file_id`, `embedded`, `filename`)."""
    request_headers = dict(headers or {})
    request_headers.setdefault("Cookie", oidc_login._cookie_header(client))
    # /api/files refuses a non-browser User-Agent with an "Illegal request" error event,
    # same as /api/agents/chat (dogfood-findings.md finding 19; chat_turn.BROWSER_UA).
    request_headers.setdefault("User-Agent", chat_turn.BROWSER_UA)
    data = {
        "endpoint": endpoint,
        "endpointType": "custom",
        "file_id": str(uuid.uuid4()),
        "message_file": "true",
        "tool_resource": "file_search",
    }
    files = {"file": (filename, content, mimetype)}
    response = client.post(
        f"{chat_url}/api/files", data=data, files=files,
        headers=request_headers, timeout=timeout,
    )
    assert response.status_code == 200, (
        f"file upload was refused ({response.status_code}): {response.text[:1000]}"
    )
    assert response.headers.get("content-type", "").startswith("application/json"), (
        f"upload did not return JSON — an SSE error event, most likely: {response.text[:1000]}"
    )
    return response.json()
