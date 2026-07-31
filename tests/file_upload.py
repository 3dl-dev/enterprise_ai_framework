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


# A 1x1 transparent PNG — the smallest byte sequence `sharp` (the server's image
# processing library) will accept as a real image rather than reject outright.
ONE_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
    b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def upload_image(client: httpx.Client, chat_url: str, headers: dict, filename: str,
                  content: bytes = ONE_PIXEL_PNG, mimetype: str = "image/png",
                  endpoint: str = "Enterprise AI", timeout: float = 60.0) -> dict:
    """POST a real image attachment the way LibreChat's own client does — NOT the
    file_search path above.

    enterpriseaiframework-282, the vision leg. Reproduced from
    `client/src/hooks/Files/useFileHandling.ts`'s `startUpload` (read out of the running
    v0.8.7 image rather than guessed at, same reasoning as `upload_file` above):
    `width`/`height` being present in the form data — not the mimetype, not a file
    extension — is what routes the client to `POST /api/files/images`
    (`useUploadFileMutation`'s `if (width !== '' && height !== '') uploadImage(...)`)
    instead of the plain `/api/files` used for documents; server-side,
    `api/server/routes/files/images.js#filterFile` REQUIRES both fields and rejects a
    request missing either. No `tool_resource` is set — that field is what routes a
    plain-file upload to `processAgentFileUpload`/RAG embedding instead, and an image
    attachment goes through `processImageFile` instead, which never touches rag-api.

    The returned file record's `type` (e.g. `image/webp`, from the server's own
    configured `imageOutputType`, not necessarily `mimetype` above) is what
    `BaseClient#processAttachments` keys on (`file.type.startsWith('image/')`) to route
    the attachment to `addImageURLs` when a later turn references this `file_id` —
    unconditionally, with no per-model vision-capability check at that layer, so this
    reaches ANY model's outbound request once attached, real or fake.
    """
    request_headers = dict(headers or {})
    request_headers.setdefault("Cookie", oidc_login._cookie_header(client))
    request_headers.setdefault("User-Agent", chat_turn.BROWSER_UA)
    data = {
        "endpoint": endpoint,
        "endpointType": "custom",
        "file_id": str(uuid.uuid4()),
        "message_file": "true",
        "width": "1",
        "height": "1",
    }
    files = {"file": (filename, content, mimetype)}
    response = client.post(
        f"{chat_url}/api/files/images", data=data, files=files,
        headers=request_headers, timeout=timeout,
    )
    assert response.status_code == 200, (
        f"image upload was refused ({response.status_code}): {response.text[:1000]}"
    )
    assert response.headers.get("content-type", "").startswith("application/json"), (
        f"upload did not return JSON — an SSE error event, most likely: {response.text[:1000]}"
    )
    return response.json()
