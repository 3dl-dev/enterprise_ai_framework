"""What one voice session is configured with. No livekit import on purpose: this is the part
that decides which endpoint every plugin talks to, so it is testable without the framework
(tests/test_voice_worker.py) and reviewable in one screen.

The single base URL (VOICE_AUDIO_BASE, the control plane's /voice/v1) serves all three legs:
STT, TTS and the LLM node. The bearer is the per-session token the control plane put in the
job's dispatch metadata; the worker holds no other credential for any of them.
"""
import json
import os
from dataclasses import dataclass

DEFAULT_BASE = "http://control-plane:8000/voice/v1"


@dataclass(frozen=True)
class SessionSettings:
    base_url: str
    session_token: str
    stt_model: str
    tts_model: str
    voice: str
    llm_model: str = "raven"


def load(metadata: str, env=os.environ) -> SessionSettings:
    try:
        meta = json.loads(metadata or "")
        got = {k: meta[k] for k in ("session", "stt_model", "tts_model", "voice")}
        assert all(isinstance(v, str) and v for v in got.values())
    except (ValueError, KeyError, AssertionError, TypeError) as exc:
        raise ValueError(f"the job's dispatch metadata is not a voice session: {exc!r}") from exc
    return SessionSettings(
        base_url=(env.get("VOICE_AUDIO_BASE") or DEFAULT_BASE).rstrip("/"),
        session_token=got["session"], stt_model=got["stt_model"],
        tts_model=got["tts_model"], voice=got["voice"])
