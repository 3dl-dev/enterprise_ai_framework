"""Contract H, the control-plane half: the voice registry, the voice session token, the audio
relay and the Raven turn adapter (agents-raven.md, item enterpriseaiframework-82e).

THE SHAPE, END TO END

    browser --(LiveKit token from /portal/api/voice/token)--> LiveKit room
    voice worker (one job per room) joins the room, hears the user, and calls THIS module's
    routes under /voice/v1 with a per-session bearer:

      POST /voice/v1/audio/transcriptions   STT, forwarded to the gateway on the RAVEN'S key
      POST /voice/v1/audio/speech           TTS, forwarded on the same key, in the PINNED voice
      POST /voice/v1/chat/completions       the LLM node: one Raven turn over its own /rpc

    The worker holds no user credential and no gateway key. Everything it may do is what the
    session token names: one (owner, raven, room), for an hour.

WHY A RELAY AND NOT THE GATEWAY DIRECTLY

Voice spend must land on `<owner>::agents/<raven>` (Contract 1). The Raven's integrated key is
in its `-key` Secret, which the control plane can read and a worker pod must never be able to.
So the relay looks the key up per call and injects it; it never returns it and never logs it.

WHAT THE CALLER CANNOT CHOOSE

The model and the voice come from the REGISTRY, not the request body. The worker is told the
same values (dispatch metadata) so its plugins are configured coherently, but a compromised
worker asking for another voice or a costlier model is silently held to the registry's.
Only whitelisted, scalar request fields are forwarded.

WHO MAY CALL IT

Only a non-loopback peer. The portal is reachable only from the sidecar on loopback (see
portal.require_user); the voice routes are the opposite door: the worker's pod IP. Loopback
is refused so the two doors can never be confused, and the session token is checked after
that. The owner is re-checked against the cluster on every call (a Raven deleted mid-session
stops working immediately).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import logging
import os
import time
import uuid
import wave

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import agents, db, gateway
from .portal import require_user

_log = logging.getLogger("voice")
router = APIRouter()

WORKER_AGENT_NAME = "eaf-voice"  # the name the worker registers under; dispatch is explicit
VOICE_ANNOTATION = "agent.enterprise-ai/voice"
SESSION_AUD = "eaf-voice-session"
SESSION_TTL_SECONDS = int(os.environ.get("VOICE_SESSION_TTL", "3600"))

# Per-process signing key for session tokens unless the deployment pins one. The worker never
# holds it (so it cannot mint a session for a Raven it was not dispatched to), and a control
# plane restart only ends the live conversations, which the browser reconnects.
_EPHEMERAL_SESSION_SECRET = uuid.uuid4().hex + uuid.uuid4().hex


def _session_secret() -> str:
    return os.environ.get("VOICE_SESSION_SECRET") or _EPHEMERAL_SESSION_SECRET


# ----------------------------------------------------------------- the catalogue

# Deployment list of voices (agents-raven.md "Per-agent voice registry"). Local defaults:
# Speaches (MIT) serving Kokoro-82M (Apache-2.0) behind the gateway's speech-stt / speech-tts
# names (bundle/litellm/config.base.yaml). A deployment overrides it with AGENT_VOICES, a JSON
# object {voice-id: {provider, stt_model, tts_model, voice}}.
_DEFAULT_VOICES = {
    "kokoro-heart": {"provider": "local-speech", "stt_model": "speech-stt",
                     "tts_model": "speech-tts", "voice": "af_heart"},
    "kokoro-michael": {"provider": "local-speech", "stt_model": "speech-stt",
                       "tts_model": "speech-tts", "voice": "am_michael"},
    "kokoro-emma": {"provider": "local-speech", "stt_model": "speech-stt",
                    "tts_model": "speech-tts", "voice": "bf_emma"},
}
_ENTRY_KEYS = ("provider", "stt_model", "tts_model", "voice")


def voices() -> dict[str, dict]:
    raw = os.environ.get("AGENT_VOICES", "").strip()
    if not raw:
        return dict(_DEFAULT_VOICES)
    try:
        cat = json.loads(raw)
        assert isinstance(cat, dict) and cat
        for vid, ent in cat.items():
            assert gateway.AGENT_SLUG.match(vid), vid
            assert all(isinstance(ent.get(k), str) and ent[k] for k in _ENTRY_KEYS), vid
    except (ValueError, AssertionError, AttributeError) as exc:
        # A malformed catalogue must not silently fall back to the defaults: that would put
        # an operator's agents on voices they did not choose.
        raise HTTPException(500, f"AGENT_VOICES is malformed ({exc}); fix the deployment") from exc
    return {vid: {k: ent[k] for k in _ENTRY_KEYS} for vid, ent in cat.items()}


def default_voice_id() -> str:
    want = os.environ.get("AGENT_VOICE_DEFAULT", "")
    cat = voices()
    return want if want in cat else next(iter(cat))


def resolve(voice_id: str | None) -> tuple[str, dict]:
    """(voice-id, entry) for a pinned id, or the default when unpinned or no longer offered."""
    cat = voices()
    vid = voice_id if voice_id in cat else default_voice_id()
    return vid, cat[vid]


def _pinned(deployment: dict) -> str | None:
    return ((deployment.get("metadata") or {}).get("annotations") or {}).get(VOICE_ANNOTATION)


class PinBody(BaseModel):
    voice: str


@router.get("/portal/api/agents/{name}/voice")
async def get_voice(name: str, user: str = Depends(require_user)):
    async with agents._client() as client:
        dep = await agents._owned_deployment(client, user, name)
    pinned = _pinned(dep)
    vid, entry = resolve(pinned)
    return {"agent": name, "voice": vid, "pinned": pinned in voices(), "entry": entry,
            "voices": sorted(voices())}


@router.post("/portal/api/agents/{name}/voice")
async def pin_voice(name: str, body: PinBody, user: str = Depends(require_user)):
    """Pin an agent's voice. The id is checked against the catalogue: a body value never
    reaches an annotation, a request or a TTS call verbatim."""
    if body.voice not in voices():
        raise HTTPException(400, f"unknown voice {body.voice!r}; choose one of {sorted(voices())}")
    async with agents._client() as client:
        await agents._owned_deployment(client, user, name)
        await agents._patch(client, "apps/v1", "Deployment", agents.object_name(user, name),
                            {"metadata": {"annotations": {VOICE_ANNOTATION: body.voice}}})
    await db.audit(user, "agent.voice", f"{user}/{name}", voice=body.voice)
    return {"agent": name, "voice": body.voice, "entry": voices()[body.voice]}


# ----------------------------------------------------------------- the session token

def mint_session(user: str, raven: str, room: str, *, ttl: int | None = None,
                 now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    claims = {"aud": SESSION_AUD, "sub": user, "raven": raven, "room": room,
              "iat": issued, "exp": issued + (SESSION_TTL_SECONDS if ttl is None else ttl)}
    return jwt.encode(claims, _session_secret(), algorithm="HS256")


def dispatch_metadata(session: str, entry: dict) -> str:
    """What the worker is told at dispatch: the bearer to use and the pinned voice id, so its
    TTS plugin is configured to match (the relay enforces the registry's voice and MODELS
    regardless: the worker names only the OpenAI contract models `whisper-1` / `tts-1`, which
    the relay maps to the registry's gateway model names)."""
    return json.dumps({"session": session, "voice": entry["voice"]})


class Session:
    def __init__(self, user: str, raven: str, room: str, deployment: dict):
        self.user, self.raven, self.room, self.deployment = user, raven, room, deployment

    @property
    def obj(self) -> str:
        return agents.object_name(self.user, self.raven)


async def session_from(request: Request) -> Session:
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1", ""):
        raise HTTPException(403, "the voice relay is reachable from a pod, not from the portal proxy")
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "a voice session token is required")
    try:
        claims = jwt.decode(auth[7:].strip(), _session_secret(), algorithms=["HS256"],
                            audience=SESSION_AUD)
        user, raven, room = claims["sub"], claims["raven"], claims["room"]
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(401, "invalid or expired voice session token") from None
    if room != f"voice-{user}.{raven}":  # a token is bound to exactly one room
        raise HTTPException(401, "invalid voice session token")
    async with agents._client() as client:
        try:
            dep = await agents._owned_deployment(client, user, raven)
        except HTTPException:
            raise HTTPException(401, "this voice session's agent no longer exists") from None
    return Session(user, raven, room, dep)


async def _raven_key(s: Session) -> str:
    async with agents._client() as client:
        secret = await agents._get(client, "v1", "Secret", f"{s.obj}-key")
    raw = ((secret or {}).get("data") or {}).get("OPENAI_API_KEY")
    if not raw:
        raise HTTPException(503, f"agent {s.raven!r} has no gateway key to bill voice to")
    return base64.b64decode(raw).decode()


def audio_base() -> str:
    """The gateway's OpenAI-shaped base for audio: the SAME endpoint the Raven's own model
    traffic uses (agents.gateway_base(): AGENT_GATEWAY_BASE, else the backend its key was
    minted by; an fr- key sent to LiteLLM is a 401), overridable for a dedicated speech edge."""
    return (os.environ.get("VOICE_GATEWAY_BASE") or agents.gateway_base()).rstrip("/")


_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Scalar fields the relay forwards besides the registry-pinned ones.
_STT_FIELDS = ("language", "response_format", "temperature", "prompt")
_TTS_FIELDS = ("response_format", "speed")
MAX_TTS_CHARS = 4096
MAX_STT_BYTES = 25 * 1024 * 1024


def _wav_seconds(data: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return round(w.getnframes() / float(w.getframerate()), 3)
    except Exception:  # noqa: BLE001 - not a WAV; the byte count still goes in the audit
        return None


def _upstream_error(resp: httpx.Response, what: str) -> HTTPException:
    detail = resp.text[:300]
    return HTTPException(502, f"the gateway refused {what}: {resp.status_code} {detail}")


@router.post("/voice/v1/audio/transcriptions")
async def transcriptions(request: Request, s: Session = Depends(session_from)):
    _, entry = resolve(_pinned(s.deployment))
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(400, "file is required")
    data = await upload.read()
    if not data or len(data) > MAX_STT_BYTES:
        raise HTTPException(400, "audio is empty or too large")
    fields = {k: str(form[k]) for k in _STT_FIELDS if k in form}
    fields["model"] = entry["stt_model"]  # pinned: the body's model is ignored
    key = await _raven_key(s)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{audio_base()}/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"}, data=fields,
                files={"file": (upload.filename or "audio.wav", data,
                                upload.content_type or "audio/wav")})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"the gateway is unreachable for speech: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise _upstream_error(resp, "transcription")
    await db.audit(s.user, "voice.audio", f"{s.user}/{s.raven}", kind="stt",
                   seconds=_wav_seconds(data), bytes=len(data), model=entry["stt_model"])
    return Response(resp.content, media_type=resp.headers.get("content-type", "application/json"))


@router.post("/voice/v1/audio/speech")
async def speech(request: Request, s: Session = Depends(session_from)):
    _, entry = resolve(_pinned(s.deployment))
    try:
        body = await request.json()
        text = body["input"]
        assert isinstance(text, str) and text.strip()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "input (text) is required") from None
    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(400, f"input exceeds {MAX_TTS_CHARS} characters")
    payload = {k: body[k] for k in _TTS_FIELDS if k in body and isinstance(body[k], (str, int, float))}
    payload.update({"model": entry["tts_model"], "voice": entry["voice"], "input": text})
    key = await _raven_key(s)
    client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        upstream = await client.send(
            client.build_request("POST", f"{audio_base()}/audio/speech", json=payload,
                                 headers={"Authorization": f"Bearer {key}"}),
            stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"the gateway is unreachable for speech: {type(exc).__name__}") from exc
    if upstream.status_code >= 400:
        detail = (await upstream.aread()).decode(errors="replace")[:300]
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, f"the gateway refused speech: {upstream.status_code} {detail}")
    await db.audit(s.user, "voice.audio", f"{s.user}/{s.raven}", kind="tts",
                   characters=len(text), model=entry["tts_model"], voice=entry["voice"])

    async def relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:  # a worker that disconnects (barge-in) cancels the upstream call
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(relay(), media_type=upstream.headers.get("content-type", "audio/mpeg"))


# ----------------------------------------------------------------- the Raven as the LLM node

# A voice turn is spoken aloud. Prepended to the turn so the reply is speakable; the Raven's
# own persona and memory are otherwise untouched.
TURN_HINT = os.environ.get(
    "VOICE_TURN_HINT",
    "(This is a spoken conversation. Reply in one to three short plain sentences, "
    "with no markdown, lists, emoji or code.)")
TURN_TIMEOUT = float(os.environ.get("VOICE_TURN_TIMEOUT", "120"))


def _last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            if isinstance(c, str) and c.strip():
                return c.strip()
    return ""


async def raven_turn(target: dict, session_key: str, text: str):
    """One Raven turn over its JSON-RPC socket, yielding each `token.delta` text. The
    protocol is the one tests-live/test_raven_agent.py drives (turn.subscribe, turn.send,
    token.delta ..., message.complete), the same the console proxy carries for the WebUI."""
    import websockets

    header_kw = ("additional_headers"
                 if "additional_headers" in inspect.signature(websockets.connect).parameters
                 else "extra_headers")
    url = f"ws://{target['host']}:{target['port']}/rpc"
    async with websockets.connect(url, max_size=None, open_timeout=15,
                                  **{header_kw: {"x-raven-token": target["token"]}}) as ws:
        async def call(i: int, method: str, params: dict) -> dict:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if m.get("id") == i:
                    return m

        # A Raven keeps every subscription ever opened on a session and (measured against the real
        # image, 82e) delivers each event on all of them to whichever socket is talking, so the Nth
        # turn of a conversation arrived N times over ("Blue.Blue.Blue."). Each event carries the
        # subscription_id it belongs to: keep only ours, and close it when the turn ends.
        subscribed = await call(1, "turn.subscribe", {"session_key": session_key})
        mine = (subscribed.get("result") or {}).get("subscription_id")
        try:
            sent = await call(2, "turn.send", {"session_key": session_key, "content": text})
            if not (sent.get("result") or {}).get("accepted"):
                raise RuntimeError(f"raven refused the turn: {sent}")
            async for piece in _turn_events(ws, mine):
                yield piece
        finally:
            if mine:
                try:
                    await asyncio.wait_for(call(3, "turn.unsubscribe", {"subscription_id": mine}), 5)
                except Exception:  # noqa: BLE001 - best effort; the turn's outcome is already decided
                    pass


async def _turn_events(ws, mine):
    """The `token.delta` texts of one turn, from the frames of subscription `mine` only."""
    deadline = time.monotonic() + TURN_TIMEOUT
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the raven did not finish its turn in time")
        m = json.loads(await asyncio.wait_for(ws.recv(), left))
        if mine and (m.get("params") or {}).get("subscription_id") not in (None, mine):
            continue
        ev = (m.get("params") or {}).get("event") or {}
        if ev.get("type") == "token.delta":
            yield ev["payload"]["text"]
        elif ev.get("type") == "message.complete":
            return
        elif ev.get("type") == "error":
            # The turn failed inside the Raven (its model call, most often). Without this the
            # socket stays open and the caller waits out the whole turn timeout in silence.
            p = ev.get("payload") or {}
            raise RuntimeError(f"raven turn_failed: {p.get('message')}: {str(p.get('detail'))[:200]}")


def _chunk(cid: str, model: str, delta: dict, finish: str | None = None) -> str:
    return "data: " + json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }) + "\n\n"


@router.post("/voice/v1/chat/completions")
async def chat_completions(request: Request, s: Session = Depends(session_from)):
    """OpenAI-shaped, so the worker's stock LLM plugin can use the Raven as its LLM node.

    Only the LAST user message is sent: the Raven keeps its own session memory keyed by the
    room, so replaying the plugin's transcript would double it."""
    body = await request.json()
    text = _last_user_text(body.get("messages"))
    if not text:
        raise HTTPException(400, "no user message to answer")
    target = await agents.console_target(s.user, s.raven)
    if target.get("type") != "raven":
        raise HTTPException(400, f"agent {s.raven!r} is not a raven")
    session_key = f"voice:{s.room}"
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    model = body.get("model") or "raven"
    await db.audit(s.user, "voice.turn", f"{s.user}/{s.raven}", characters=len(text))
    prompt = f"{TURN_HINT}\n\n{text}" if TURN_HINT else text

    if not body.get("stream"):
        out = ""
        try:
            async for piece in raven_turn(target, session_key, prompt):
                out += piece
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"the raven turn failed: {type(exc).__name__}: {exc}") from exc
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": out},
                         "finish_reason": "stop"}]})

    async def events():
        yield _chunk(cid, model, {"role": "assistant", "content": ""})
        try:
            async for piece in raven_turn(target, session_key, prompt):
                yield _chunk(cid, model, {"content": piece})
        except Exception as exc:  # noqa: BLE001 - surfaced to the worker, never swallowed
            _log.warning("raven turn for %s/%s failed: %s: %s", s.user, s.raven, type(exc).__name__, exc)
            yield "data: " + json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}}) + "\n\n"
            return
        yield _chunk(cid, model, {}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
