"""Drive one real conversational turn through the chat surface.

WHY THIS EXISTS (enterpriseaiframework-f50). LibreChat changed the wire protocol of
`POST /api/agents/chat/<endpoint>` between v0.8.0 and v0.8.7, and the change is invisible
until you read the response:

    v0.8.0  Content-Type: text/event-stream — the POST *is* the stream. The caller reads
            SSE frames off the POST body and takes the frame carrying `"final": true`.
    v0.8.7  Content-Type: application/json — the POST only *starts* a generation job and
            returns {"streamId", "conversationId", "status": "started"}. The stream is a
            separate `GET /api/agents/chat/stream/:streamId`.

A client written for one returns nothing at all against the other: parsed as SSE, the JSON
body yields zero frames and the caller reports "stream ended with no terminal event" — a
failure that names the wrong thing. Measured against the v0.8.7 bundle, not inferred.

The GET stream is also NOT a reliable place to read the answer after the fact: it is a live
job subscription, and against a fast upstream (the bundle's fakeprovider answers in
milliseconds) the job is finished and reaped before the caller can subscribe — observed
returning 404 for a turn that had already completed successfully.

So this helper does not read the answer off a stream at all. It reads the answer from
`GET /api/messages/<conversationId>`, which is where the answer durably lives on BOTH
versions: the assistant message LibreChat persisted, including its `content` blocks and any
`tool_call` blocks. That is also a stronger claim than a stream frame — a persisted reply
proves the turn completed and was saved, not merely that bytes were emitted.
"""

import json
import time
import urllib.parse
import uuid

NO_PARENT = "00000000-0000-0000-0000-000000000000"

# /api/agents/chat rejects non-browser User-Agents with "Illegal request"
# (dogfood-findings.md finding 19). A precondition, not a workaround.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def build_payload(text, model, endpoint, endpoint_type="custom", mcp_servers=None,
                  manual_skills=None, execute_code=False, web_search=False,
                  skills_catalog=False, prompt_prefix=None, file_search=False,
                  files=None, reasoning_effort=None):
    """The body LibreChat's own client sends for an ephemeral-agent turn.

    `ephemeralAgent.mcp` is what attaches an MCP server to an otherwise plain
    custom-endpoint turn; the entries are server names from `mcpServers` in
    librechat.yaml, not tool names.

    `manual_skills` is the `$`-popover's wire shape (enterpriseaiframework-6ff):
    a top-level `manualSkills` array of skill NAMES (not `ephemeralAgent.mcp`'s
    shape) plus `ephemeralAgent.skills: true`, the per-conversation skills-badge
    toggle. Both are required — packages/api/src/agents/skills.ts only resolves
    `manualSkills` against an `accessibleSkillIds` set that is empty unless the
    toggle is on (`resolveAgentScopedSkillIds`'s ephemeral-agent branch falls
    through to `ephemeralSkillsToggle ? ... : []`), so sending names without the
    toggle silently primes nothing.

    `ephemeralAgent.execute_code` (enterpriseaiframework-082) is the per-turn switch for
    the codeapi sandbox — a real zod key (data-provider/src/models.ts's
    tModelSpecPresetSchema has the modelSpec-level twin, `executeCode`; this is the
    ephemeral-agent one, read in api/server/services/Endpoints/agents/added.ts:
    `ephemeralAgent?.execute_code === true || modelSpec?.executeCode === true`). Needed
    when a turn is sent against a bare model name (e.g. "fake-large") rather than
    through a modelSpec, which is exactly how the hermetic suite talks to the fake
    upstream.

    `web_search=True` sets `ephemeralAgent.web_search`, which is the ONLY thing that
    attaches the built-in web_search tool to a turn on a custom endpoint. In v0.8.7 the
    check is literally `ephemeralAgent?.web_search === true || modelSpec?.webSearch ===
    true` — a truthy value is not enough, and configuring `webSearch` in librechat.yaml
    does NOT attach the tool by itself. A turn sent without this flag gets no tool, the
    model answers from weights alone, and the reply can look entirely reasonable while
    citing nothing — so a test that forgot the flag would be testing the model's memory.

    `skills_catalog=True` (enterpriseaiframework-e6f) is the OTHER skills toggle,
    distinct from `manual_skills` above: it sets `ephemeralAgent.skills = True` with NO
    `manualSkills` names, which is what exposes the model's own `skill(name)` tool over
    the FULL accessible catalog (`resolveAgentScopedSkillIds`'s ephemeral-agent branch:
    `agent.skills_enabled` unset -> `ephemeralSkillsToggle ? scopeSkillIds(..., undefined)
    : []` — the toggle alone, no forced name, yields the whole catalog). This is
    "automatic discovery" — the model deciding on its own whether a loaded skill
    applies — as opposed to `manual_skills`, which is the `$`-popover forcing one in.
    Setting both is redundant, not additive: `manual_skills` already flips the same
    toggle, it just also forces specific names via `manualSkills`.

    `prompt_prefix` sets the top-level `promptPrefix` field of the request body.
    `packages/api/src/agents/load.ts#loadEphemeralAgent` reads it as
    `req.body?.promptPrefix` and uses it as the ephemeral agent's `instructions` whenever
    the turn carries no model-spec-level `promptPrefix` of its own (`const instructions =
    typeof modelPromptPrefix === "string" ? modelPromptPrefix : requestPromptPrefix`).
    This is the SAME field a modelSpec's own `preset.promptPrefix` populates when a
    conversation is opened through a spec (`applyModelSpecPreset` merges it into the
    parsed body before `buildOptions` strips it into `model_parameters`) — sending it
    directly here exercises the identical server-side code path without needing to
    resolve a `spec` name against `modelSpecs.list` first, which is what lets a test
    compare "this system prompt" against "no system prompt" on a turn sent by bare
    model + endpoint, exactly as every other test in this module already does.

    `file_search=True` (enterpriseaiframework-c7c) sets `ephemeralAgent.file_search`,
    the file_search twin of `web_search` above — `packages/api/src/agents/added.ts`'s
    check is the identical shape: `ephemeralAgent?.file_search === true ||
    modelSpec?.fileSearch === true`. Without it the turn gets no file_search tool even
    if `files` below names real, previously-uploaded, embedded files.

    `reasoning_effort` (enterpriseaiframework-282) sets the top-level `reasoning_effort`
    field of the request body — a real key of librechat-data-provider's
    `tConversationSchema` (peer of `temperature`/`top_p`, not nested under anything), so
    this exercises the exact same field the Parameters panel writes rather than a
    synthetic shape. `@librechat/api`'s `getOpenAILLMConfig`/`applyReasoningConfig` reads
    it off `modelOptions` and, for a `custom` (OpenAI-shaped) endpoint with no Responses
    API, sets `llmConfig.reasoning_effort` directly on the outbound request — the same
    field deploy/gateway/strip_reasoning.py's docstring names as one GLM ignores for
    disabling reasoning. `drop_params: true` in litellm_settings means an upstream that
    does not recognise the field drops it rather than erroring, so this is safe to send
    against the bundle's fakes too.

    `files` is the wire shape LibreChat's own upload response already comes in
    (`[{file_id, filename, type, ...}, ...]` from `POST /api/files`) — passed straight
    through as the message's top-level `files` field, `api/server/controllers/
    agents/request.js`'s `req.body.files`. THIS ALONE DOES NOT GRANT ACCESS: the
    server re-resolves every file_id against `req.user.id`
    (`packages/data-schemas/src/methods/file.ts#updateFileUsage`'s owner-scoped
    query) before it becomes an attachment the file_search tool can see — a file_id
    naming someone else's document is silently dropped, not served. That
    fail-closed, identity-from-the-session (not from this field) behavior is exactly
    what tests/test_file_search.py's cross-user test exercises.
    """
    body = {
        "text": text,
        "endpoint": endpoint,
        "endpointType": endpoint_type,
        "model": model,
        "sender": "User",
        "isCreatedByUser": True,
        "messageId": str(uuid.uuid4()),
        "parentMessageId": NO_PARENT,
        "conversationId": None,
        "error": False,
        "isContinued": False,
        "isTemporary": False,
        "isRegenerate": False,
    }
    ephemeral_agent = {}
    if mcp_servers:
        ephemeral_agent["mcp"] = list(mcp_servers)
    if manual_skills:
        ephemeral_agent["skills"] = True
        body["manualSkills"] = list(manual_skills)
    elif skills_catalog:
        ephemeral_agent["skills"] = True
    if execute_code:
        ephemeral_agent["execute_code"] = True
    if web_search:
        ephemeral_agent["web_search"] = True
    if file_search:
        ephemeral_agent["file_search"] = True
    if ephemeral_agent:
        body["ephemeralAgent"] = ephemeral_agent
    if prompt_prefix is not None:
        body["promptPrefix"] = prompt_prefix
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    if files:
        body["files"] = list(files)
    return body


def _sse_events(raw_body):
    """Parse `data:` frames out of an SSE body that has already been read."""
    events = []
    for line in raw_body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


def start_turn(client, chat_url, payload, headers=None, timeout=180.0):
    """POST the turn and return (conversation_id, legacy_final_event_or_None).

    Handles both protocols. The second element is only populated on the v0.8.0 shape,
    where the terminal event is the only thing the POST hands back.

    `headers` must carry the surface's bearer token and session cookie; the User-Agent is
    added here because the route refuses a non-browser one outright (finding 19) and the
    resulting 401/"Illegal request" reads as an auth problem rather than a UA problem.
    """
    url = f"{chat_url}/api/agents/chat/{urllib.parse.quote(payload['endpoint'], safe='')}"
    request_headers = {"Accept": "text/event-stream", "User-Agent": BROWSER_UA}
    request_headers.update(headers or {})
    response = client.post(
        url, json=payload, headers=request_headers, timeout=timeout
    )
    assert response.status_code == 200, (
        f"chat turn was refused ({response.status_code}): {response.text[:500]}"
    )

    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        # v0.8.6+ — a job handle, not the answer.
        started = response.json()
        conversation_id = started.get("conversationId")
        assert conversation_id, f"no conversationId in {started}"
        return conversation_id, None

    # v0.8.0 — the POST body is the stream.
    events = _sse_events(response.text)
    for event in events:
        # An `event: error` frame carries {"text": ...} and no "final" key. Name it here
        # rather than reporting "no terminal event" further down, which blames the wrong
        # thing.
        if set(event.keys()) == {"text"}:
            raise AssertionError(f"chat surface returned an error event: {event['text']}")
    final = next((e for e in events if e.get("final") is True), None)
    assert final is not None, "stream ended with no terminal ('final': true) event"
    conversation_id = (
        final.get("conversation", {}).get("conversationId")
        or (final.get("responseMessage") or {}).get("conversationId")
    )
    assert conversation_id, f"terminal event carried no conversationId: {final}"
    return conversation_id, final


def wait_for_reply(client, chat_url, conversation_id, headers=None, timeout=180.0,
                   poll=2.0):
    """Return the persisted assistant message for a conversation.

    Polls rather than sleeps: generation is asynchronous on v0.8.6+, and a fixed sleep
    either flakes on a slow model or wastes the whole budget on a fast one.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get(
            f"{chat_url}/api/messages/{conversation_id}",
            headers=headers or {}, timeout=60.0,
        )
        if r.status_code == 200:
            last = r.json()
            replies = [m for m in last if not m.get("isCreatedByUser")]
            if replies:
                reply = replies[-1]
                # `unfinished` is set while the message is still being written.
                if not reply.get("unfinished"):
                    return reply
        time.sleep(poll)
    raise AssertionError(
        f"no assistant reply was persisted for conversation {conversation_id} "
        f"within {timeout}s; last read: {last}"
    )


def reply_text(message):
    """The assistant's visible text, from either the `text` field or `content` blocks.

    v0.8.6+ persists the answer as typed content blocks and leaves the flat `text` field
    empty for agent turns; v0.8.0 populated `text`. Reading only one of them silently
    yields "" on the other version, which asserts as "the model said nothing".
    """
    blocks = message.get("content") or []
    from_blocks = "".join(
        b.get("text", "") for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    )
    return from_blocks or (message.get("text") or "")


def tool_calls(message):
    """Every tool_call content block on a persisted assistant message."""
    return [
        b for b in (message.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_call"
    ]


def send_turn(client, chat_url, text, model, endpoint, endpoint_type="custom",
              mcp_servers=None, manual_skills=None, execute_code=False,
              web_search=False, skills_catalog=False, prompt_prefix=None,
              file_search=False, files=None, reasoning_effort=None, headers=None,
              timeout=180.0):
    """One turn, end to end, on either protocol. Returns the persisted assistant message."""
    payload = build_payload(
        text, model, endpoint, endpoint_type, mcp_servers,
        manual_skills=manual_skills, execute_code=execute_code,
        web_search=web_search, skills_catalog=skills_catalog,
        prompt_prefix=prompt_prefix, file_search=file_search, files=files,
        reasoning_effort=reasoning_effort,
    )
    conversation_id, _ = start_turn(
        client, chat_url, payload, headers=headers, timeout=timeout
    )
    return wait_for_reply(
        client, chat_url, conversation_id, headers=headers, timeout=timeout
    )
