"""Voice (Contract H, item enterpriseaiframework-82e): the registry, the session token, the audio
relay and the Raven turn adapter.

SECURITY FIRST. The relay spends money on a Raven's key and the room token is the whole of
room authorisation, so the cases below are people trying to cross a boundary:

  * a session token minted for one (owner, raven, room) reaching another's key;
  * a worker choosing its own voice/model (the registry pins them);
  * the portal door (loopback) being used as the worker door and the other way round;
  * a forged / expired / other-secret token; a Raven deleted mid-session.

WHAT IS REAL AND WHAT IS A NAMED DOUBLE
Real: app.voice, app.livekit_tokens, app.agents, app.portal.require_user, FastAPI routing,
PyJWT (the independent verifier of every token), the websocket client, httpx multipart.
Doubles, each with its recorded ground truth:
  * the Kubernetes API (FakeCluster, real HTTP; shared with the agent suites);
  * the Raven's /rpc socket: the frame protocol (turn.subscribe, turn.send, token.delta,
    message.complete, X-Raven-Token) is the one tests-live/test_raven_agent.py drives against the
    real hosted image;
  * the gateway's audio routes: the wire shape (Bearer key, multipart file + model, JSON text
    back; JSON model/voice/input, audio bytes back, 401 on a bad key) is what
    tests/test_speech_route.py measures against the real LiteLLM + Speaches.
The claim that none of this drifts from the real things is tests-live/test_voice_live.py, which
runs the real LiveKit server, gateway, speech server and Raven.
"""
import asyncio
import base64
import io
import json
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_agent_raven import OWNER, TOKEN, _serve_ws, world  # noqa: E402,F401
from test_portal_agents import AUDIT  # noqa: E402,F401

from app import db, livekit_tokens, voice  # noqa: E402

LK_KEY, LK_SECRET, LK_URL = "APIvoice82e", "livekit-secret-that-is-32-characters-long", "ws://192.168.2.50:30780"
SESSION_SECRET = "session-secret-only-the-control-plane-holds"
WORKER_IP = ("10.42.0.77", 40100)


def _wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


# ------------------------------------------------------------------ the gateway double

class FakeGateway:
    """The gateway's two audio routes. Refuses any Bearer that is not a key it was given."""

    def __init__(self, keys):
        self.keys = set(keys)
        self.requests: list[dict] = []
        gw = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                auth = self.headers.get("Authorization", "")
                gw.requests.append({"path": self.path, "auth": auth, "body": body,
                                    "ctype": self.headers.get("Content-Type", "")})
                if auth.removeprefix("Bearer ") not in gw.keys:
                    return self._send(401, b'{"error":"invalid key"}', "application/json")
                if self.path == "/v1/audio/transcriptions":
                    return self._send(200, b'{"text":"what is the weather"}', "application/json")
                if self.path == "/v1/audio/speech":
                    return self._send(200, _wav(0.3), "audio/wav")
                self._send(404, b"{}", "application/json")

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def vw(world, monkeypatch):
    """A world with two ravens on distinct keys, a gateway that knows only those keys."""
    monkeypatch.setenv("LIVEKIT_API_KEY", LK_KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", LK_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", LK_URL)
    monkeypatch.setenv("VOICE_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.delenv("AGENT_VOICES", raising=False)
    world.add_agent("alice", "rv")
    world.add_agent("bob", "rv")
    for user, key in (("alice", "sk-alice-rv"), ("bob", "sk-bob-rv")):
        sec = world.cluster.get("secrets", f"agent-{user}-rv-key")
        sec["data"]["OPENAI_API_KEY"] = base64.b64encode(key.encode()).decode()
    gw = FakeGateway({"sk-alice-rv", "sk-bob-rv"})
    monkeypatch.setenv("VOICE_GATEWAY_BASE", gw.base)
    audits: list[tuple] = []

    async def audit(actor, action, target=None, **detail):
        audits.append((actor, action, target, detail))
        return "h"

    monkeypatch.setattr(db, "audit", audit)
    world.gw, world.audits = gw, audits
    try:
        yield world
    finally:
        gw.stop()


def _api():
    api = FastAPI()
    api.include_router(livekit_tokens.router)
    api.include_router(voice.router)
    return api


def portal_as(user):
    c = TestClient(_api(), client=("127.0.0.1", 41000), raise_server_exceptions=False)
    c.headers.update({"X-Auth-Request-Preferred-Username": user})
    return c


def worker(session_token=None, peer=WORKER_IP):
    c = TestClient(_api(), client=peer, raise_server_exceptions=False)
    if session_token:
        c.headers.update({"Authorization": f"Bearer {session_token}"})
    return c


def session_for(user="alice", raven="rv", **kw):
    return voice.mint_session(user, raven, f"voice-{user}.{raven}", **kw)


# ------------------------------------------------------------------ registry

def test_an_unpinned_raven_speaks_in_the_default_voice_and_says_so(vw):
    r = portal_as("alice").get("/portal/api/agents/rv/voice")
    assert r.status_code == 200
    body = r.json()
    assert body["pinned"] is False and body["voice"] == "kokoro-heart"
    assert body["entry"] == {"provider": "local-speech", "stt_model": "speech-stt",
                             "tts_model": "speech-tts", "voice": "af_heart"}


def test_pinning_writes_the_annotation_on_the_object_and_the_token_carries_it(vw):
    r = portal_as("alice").post("/portal/api/agents/rv/voice", json={"voice": "kokoro-emma"})
    assert r.status_code == 200, r.text
    dep = vw.cluster.get("deployments", "agent-alice-rv")
    assert dep["metadata"]["annotations"]["agent.enterprise-ai/voice"] == "kokoro-emma"
    assert dep["metadata"]["labels"]["agent.enterprise-ai/user"] == "alice", "labels survive the patch"
    assert ("alice", "agent.voice", "alice/rv", {"voice": "kokoro-emma"}) in vw.audits
    tok = portal_as("alice").post("/portal/api/voice/token", json={"raven": "rv"}).json()
    assert tok["voice"] == "kokoro-emma"
    meta = json.loads(jwt.decode(tok["token"], LK_SECRET, algorithms=["HS256"],
                                 options={"verify_aud": False})["roomConfig"]["agents"][0]["metadata"])
    assert meta["voice"] == "bf_emma"


def test_a_voice_outside_the_catalogue_never_reaches_the_object(vw):
    for bad in ("../x", "kokoro-heart\nx: y", "af_heart", "", "KOKORO-HEART"):
        r = portal_as("alice").post("/portal/api/agents/rv/voice", json={"voice": bad})
        assert r.status_code == 400, bad
    dep = vw.cluster.get("deployments", "agent-alice-rv")
    assert "annotations" not in dep["metadata"] or "agent.enterprise-ai/voice" not in dep["metadata"]["annotations"]


def test_another_user_can_neither_read_nor_pin_my_ravens_voice(vw):
    for method in ("get", "post"):
        r = getattr(portal_as("mallory"), method)("/portal/api/agents/rv/voice",
                                                   **({"json": {"voice": "kokoro-emma"}} if method == "post" else {}))
        assert r.status_code == 404
    assert "annotations" not in vw.cluster.get("deployments", "agent-alice-rv")["metadata"]


def test_a_catalogue_from_the_environment_replaces_the_default_and_a_bad_one_is_loud(vw, monkeypatch):
    cat = {"studio": {"provider": "hosted", "stt_model": "s", "tts_model": "t", "voice": "v9"}}
    monkeypatch.setenv("AGENT_VOICES", json.dumps(cat))
    assert portal_as("alice").get("/portal/api/agents/rv/voice").json()["voices"] == ["studio"]
    # poison the env the way an operator typo would: one entry missing its tts_model
    monkeypatch.setenv("AGENT_VOICES", json.dumps({"studio": {"provider": "h", "stt_model": "s", "voice": "v"}}))
    assert portal_as("alice").get("/portal/api/agents/rv/voice").status_code == 500


def test_a_pin_the_catalogue_no_longer_offers_falls_back_rather_than_crashing(vw, monkeypatch):
    portal_as("alice").post("/portal/api/agents/rv/voice", json={"voice": "kokoro-emma"})
    monkeypatch.setenv("AGENT_VOICES", json.dumps(
        {"only": {"provider": "p", "stt_model": "s", "tts_model": "t", "voice": "v"}}))
    body = portal_as("alice").get("/portal/api/agents/rv/voice").json()
    assert body["voice"] == "only" and body["pinned"] is False


# ------------------------------------------------------------------ the room token

def _room_config(tok):
    return jwt.decode(tok, LK_SECRET, algorithms=["HS256"], options={"verify_aud": False})["roomConfig"]


def test_the_room_token_dispatches_the_worker_with_a_session_bound_to_this_room(vw):
    r = portal_as("alice").post("/portal/api/voice/token", json={"raven": "rv"})
    assert r.status_code == 200, r.text
    cfg = _room_config(r.json()["token"])
    assert cfg["emptyTimeout"] == livekit_tokens.EMPTY_TIMEOUT_SECONDS
    (dispatch,) = cfg["agents"]
    assert dispatch["agentName"] == "eaf-voice"
    meta = json.loads(dispatch["metadata"])
    claims = jwt.decode(meta["session"], SESSION_SECRET, algorithms=["HS256"], audience="eaf-voice-session")
    assert (claims["sub"], claims["raven"], claims["room"]) == ("alice", "rv", "voice-alice.rv")
    assert meta["stt_model"] == "speech-stt" and meta["tts_model"] == "speech-tts"
    assert "sk-alice-rv" not in json.dumps(r.json()) and "sk-alice-rv" not in json.dumps(meta), \
        "the Raven's gateway key must never leave the control plane"


def test_a_room_token_is_refused_for_a_raven_that_is_not_mine_or_not_a_raven(vw):
    assert portal_as("mallory").post("/portal/api/voice/token", json={"raven": "rv"}).status_code == 404
    assert portal_as("alice").post("/portal/api/voice/token", json={"raven": "ghost"}).status_code == 404
    vw.cluster.add_agent("alice", "hm", agent_type="hermes")
    r = portal_as("alice").post("/portal/api/voice/token", json={"raven": "hm"})
    assert r.status_code == 400 and "token" not in r.json()


# ------------------------------------------------------------------ session gate

def test_the_relay_refuses_the_portal_door_and_anonymous_and_forged_callers(vw):
    body = {"input": "hi"}
    good = session_for()
    # loopback (the sidecar door) is refused even with a perfect token
    assert worker(good, peer=("127.0.0.1", 1)).post("/voice/v1/audio/speech", json=body).status_code == 403
    assert worker().post("/voice/v1/audio/speech", json=body).status_code == 401
    forged = jwt.encode({"aud": "eaf-voice-session", "sub": "alice", "raven": "rv",
                         "room": "voice-alice.rv", "exp": 9999999999}, "another-secret-32-characters-xxxxxxxx",
                        algorithm="HS256")
    assert worker(forged).post("/voice/v1/audio/speech", json=body).status_code == 401
    wrong_aud = jwt.encode({"aud": "someone-else", "sub": "alice", "raven": "rv", "room": "voice-alice.rv",
                            "exp": 9999999999}, SESSION_SECRET, algorithm="HS256")
    assert worker(wrong_aud).post("/voice/v1/audio/speech", json=body).status_code == 401
    assert vw.gw.requests == [], "nothing reached the gateway"


def test_an_expired_session_and_a_room_mismatched_session_are_refused(vw):
    expired = session_for(now=1_000_000)
    assert worker(expired).post("/voice/v1/audio/speech", json={"input": "hi"}).status_code == 401
    other_room = voice.mint_session("alice", "rv", "voice-bob.rv")
    assert worker(other_room).post("/voice/v1/audio/speech", json={"input": "hi"}).status_code == 401
    assert vw.gw.requests == []


def test_a_raven_deleted_mid_session_stops_the_relay(vw):
    tok = session_for()
    vw.cluster.store.pop(("deployments", "agent-alice-rv"))
    assert worker(tok).post("/voice/v1/audio/speech", json={"input": "hi"}).status_code == 401
    assert vw.gw.requests == []


# ------------------------------------------------------------------ audio relay

def test_stt_is_forwarded_on_the_ravens_own_key_with_the_registry_model(vw):
    r = worker(session_for()).post(
        "/voice/v1/audio/transcriptions",
        data={"model": "whisper-1-EXPENSIVE", "language": "en", "response_format": "json",
              "evil_field": "x"},
        files={"file": ("file.wav", _wav(0.5), "audio/wav")})
    assert r.status_code == 200 and r.json() == {"text": "what is the weather"}
    (up,) = vw.gw.requests
    assert up["path"] == "/v1/audio/transcriptions"
    assert up["auth"] == "Bearer sk-alice-rv", "billed to alice's raven, not the session token"
    assert b'name="model"\r\n\r\nspeech-stt' in up["body"], "the registry's model, not the caller's"
    assert b"EXPENSIVE" not in up["body"] and b"evil_field" not in up["body"]
    assert b'name="language"\r\n\r\nen' in up["body"]
    (audit,) = [a for a in vw.audits if a[1] == "voice.audio"]
    assert audit[0] == "alice" and audit[2] == "alice/rv"
    assert audit[3]["kind"] == "stt" and audit[3]["seconds"] == 0.5


def test_tts_is_pinned_to_the_registered_voice_whatever_the_caller_asks_for(vw):
    portal_as("alice").post("/portal/api/agents/rv/voice", json={"voice": "kokoro-michael"})
    r = worker(session_for()).post("/voice/v1/audio/speech", json={
        "model": "tts-1-hd", "voice": "af_heart", "input": "Hello there.", "response_format": "wav",
        "speed": 1.0, "sneaky": "x"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    (up,) = vw.gw.requests
    sent = json.loads(up["body"])
    assert sent == {"model": "speech-tts", "voice": "am_michael", "input": "Hello there.",
                    "response_format": "wav", "speed": 1.0}
    assert up["auth"] == "Bearer sk-alice-rv"
    (audit,) = [a for a in vw.audits if a[1] == "voice.audio"]
    assert audit[3] == {"kind": "tts", "characters": 12, "model": "speech-tts", "voice": "am_michael"}


def test_one_users_session_can_only_ever_spend_that_users_ravens_key(vw):
    worker(session_for("bob")).post("/voice/v1/audio/speech", json={"input": "hi"})
    worker(session_for("alice")).post("/voice/v1/audio/speech", json={"input": "hi"})
    assert [r["auth"] for r in vw.gw.requests] == ["Bearer sk-bob-rv", "Bearer sk-alice-rv"]


def test_tts_input_limits_and_empty_input_are_refused_before_the_gateway(vw):
    tok = session_for()
    assert worker(tok).post("/voice/v1/audio/speech", json={"input": "x" * 5000}).status_code == 400
    assert worker(tok).post("/voice/v1/audio/speech", json={"input": "  "}).status_code == 400
    assert worker(tok).post("/voice/v1/audio/speech", json={}).status_code == 400
    assert worker(tok).post("/voice/v1/audio/transcriptions", data={"model": "m"}).status_code == 400
    assert vw.gw.requests == []


def test_a_gateway_refusal_is_a_502_naming_the_cause_and_no_audit_row(vw):
    sec = vw.cluster.get("secrets", "agent-alice-rv-key")
    sec["data"]["OPENAI_API_KEY"] = base64.b64encode(b"sk-revoked").decode()
    r = worker(session_for()).post("/voice/v1/audio/speech", json={"input": "hi"})
    assert r.status_code == 502 and "401" in r.json()["detail"]
    assert not [a for a in vw.audits if a[1] == "voice.audio"], "a refused call is not billed audio"
    assert "sk-revoked" not in r.text


def test_a_raven_with_no_gateway_key_is_a_503_not_an_unauthenticated_call(vw):
    del vw.cluster.get("secrets", "agent-alice-rv-key")["data"]["OPENAI_API_KEY"]
    assert worker(session_for()).post("/voice/v1/audio/speech", json={"input": "hi"}).status_code == 503
    assert vw.gw.requests == []


# ------------------------------------------------------------------ the Raven as the LLM node

def _raven_ws(world, replies, seen):
    async def handler(conn):
        seen["headers"] = {k.lower(): v for k, v in conn.request.headers.items()}
        seen["path"] = conn.request.path
        async for message in conn:
            call = json.loads(message)
            seen.setdefault("calls", []).append(call)
            await conn.send(json.dumps({"jsonrpc": "2.0", "id": call["id"],
                                        "result": {"accepted": True}}))
            if call["method"] == "turn.send":
                for text in replies:
                    await conn.send(json.dumps({"jsonrpc": "2.0", "method": "turn.event", "params": {
                        "event": {"type": "token.delta", "payload": {"text": text}}}}))
                await conn.send(json.dumps({"jsonrpc": "2.0", "method": "turn.event", "params": {
                    "event": {"type": "message.complete", "payload": {}}}}))

    loop = _serve_ws(world, handler)
    world.hosts["agent-alice-rv"] = "127.0.0.2"
    return loop


def _sse(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


def test_the_raven_answers_the_turn_over_its_rpc_and_streams_openai_chunks(vw):
    seen: dict = {}
    loop = _raven_ws(vw, ["It is ", "sunny."], seen)
    try:
        r = worker(session_for()).post("/voice/v1/chat/completions", json={
            "model": "raven", "stream": True, "messages": [
                {"role": "system", "content": "ignored persona"},
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "what is the weather"}]})
        assert r.status_code == 200, r.text
        chunks = _sse(r.text)
        assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "It is sunny."
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop" and r.text.rstrip().endswith("[DONE]")
        assert seen["headers"]["x-raven-token"] == TOKEN and seen["path"] == "/rpc"
        send = [c for c in seen["calls"] if c["method"] == "turn.send"][0]["params"]
        assert send["session_key"] == "voice:voice-alice.rv"
        assert send["content"].endswith("what is the weather") and "earlier" not in send["content"]
        assert "spoken conversation" in send["content"]
        assert ("alice", "voice.turn", "alice/rv") == vw.audits[-1][:3]
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_the_non_streaming_shape_carries_the_same_answer(vw):
    seen: dict = {}
    loop = _raven_ws(vw, ["Hello", " world"], seen)
    try:
        r = worker(session_for()).post("/voice/v1/chat/completions", json={
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "Hello world"
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_a_turn_with_no_user_message_or_an_unreachable_raven_is_a_clean_error(vw):
    tok = session_for()
    assert worker(tok).post("/voice/v1/chat/completions", json={"messages": []}).status_code == 400
    # nothing listening for this raven's host: the SSE stream reports the failure, not silence
    vw.hosts["agent-alice-rv"] = "127.0.0.9"
    r = worker(tok).post("/voice/v1/chat/completions", json={
        "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and "error" in r.text and "[DONE]" not in r.text
    r = worker(tok).post("/voice/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502


def test_a_turn_the_raven_fails_is_reported_at_once_not_after_the_timeout(vw):
    """The failure event shape is RECORDED from the real hosted image (a Raven whose model route
    was unreachable emitted, after llm_retry notices, an `error` event with code -32099,
    message `turn_failed` and a detail string), NOT invented here."""
    async def handler(conn):
        async for message in conn:
            call = json.loads(message)
            await conn.send(json.dumps({"jsonrpc": "2.0", "id": call["id"], "result": {"accepted": True}}))
            if call["method"] == "turn.send":
                for ev in ({"type": "notice", "payload": {"kind": "llm_retry", "detail": "network"}},
                           {"type": "error", "payload": {"code": -32099, "message": "turn_failed",
                                                         "reason": "internal",
                                                         "detail": "Error calling LLM (network@custom)"}}):
                    await conn.send(json.dumps({"jsonrpc": "2.0", "method": "event",
                                                "params": {"subscription_id": "s", "event": ev}}))

    loop = _serve_ws(vw, handler)
    vw.hosts["agent-alice-rv"] = "127.0.0.2"
    try:
        r = worker(session_for()).post("/voice/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 502 and "turn_failed" in r.json()["detail"]
        assert "Error calling LLM" in r.json()["detail"]
        s = worker(session_for()).post("/voice/v1/chat/completions", json={
            "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        assert "turn_failed" in s.text and "[DONE]" not in s.text
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_a_hermes_agent_is_not_a_voice_llm(vw):
    vw.cluster.add_agent("alice", "hm", agent_type="hermes")
    r = worker(session_for("alice", "hm")).post("/voice/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code in (400, 503)


def test_the_router_is_mounted_on_the_real_control_plane_app():
    from pathlib import Path
    assert "voice.router" in (Path(voice.__file__).parent / "main.py").read_text()
