"""The EAF voice worker (Contract H, agents-raven.md; item enterpriseaiframework-82e).

One LiveKit Agents job per room `voice-<user>.<raven>`: Silero VAD -> STT -> the Raven as the
LLM -> TTS, every leg through the control plane's /voice/v1 relay with the per-session token
the control plane put in this job's dispatch metadata. The relay spends the RAVEN's gateway
key, so voice lands on `<owner>::agents/<raven>`.

WHAT IS DELIBERATELY ABSENT (each is a LiveKit Cloud or non-OSI piece, agents-raven.md "LiveKit
traps"; tests/test_voice_worker.py fails the build if any appears in this file):
  * LiveKit Inference (STT/LLM/TTS by model string, the adaptive interruption model)
  * noise cancellation plugins (Krisp / ai-coustics)
  * the turn-detector model
End of turn is Silero VAD, and interruption handling is pinned to VAD so the adaptive
(cloud-backed) detector is never constructed.
"""
import asyncio
import logging
import os

from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    room_io,
)
from livekit.plugins import openai, silero

import settings

logger = logging.getLogger("eaf-voice")
AGENT_NAME = "eaf-voice"
# After the user leaves, the session waits this long for them to come back before it ends. A
# user who hangs up and presses Talk again at once must find the same agent still in the room:
# the control plane sends no second one to a room that has one, and the framework's own
# close-on-disconnect would end the session under a rejoining user.
LINGER_SECONDS = float(os.environ.get("VOICE_LINGER_SECONDS", "6"))

# Two idle warm processes by default rather than one per CPU: each holds a Silero VAD, and a
# session is one process, so this is a per-node concurrency knob, not a correctness one.
server = AgentServer(num_idle_processes=int(os.environ.get("VOICE_IDLE_PROCESSES", "2")))


def prewarm(proc: agents.JobProcess) -> None:
    # Silero ships its ONNX weights inside the wheel: nothing is fetched here or at runtime.
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    cfg = settings.load(ctx.job.metadata)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    auth = dict(base_url=cfg.base_url, api_key=cfg.session_token)
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=openai.STT(model=cfg.stt_model, use_realtime=False, **auth),
        llm=openai.LLM(model=cfg.llm_model, **auth),
        tts=openai.TTS(model=cfg.tts_model, voice=cfg.voice, response_format="wav", **auth),
        turn_handling={"turn_detection": "vad", "interruption": {"mode": "vad"}},
    )
    # The Raven has its own persona and memory; the voice layer adds no instructions of its own.
    await session.start(
        agent=Agent(instructions=""), room=ctx.room,
        room_options=room_io.RoomOptions(close_on_disconnect=False),
    )

    def user_present() -> bool:
        return any(p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                   for p in ctx.room.remote_participants.values())

    async def leave_unless_user_returns() -> None:
        await asyncio.sleep(LINGER_SECONDS)
        if not user_present():
            logger.info("user left room %s and did not return; ending the session", ctx.room.name)
            ctx.shutdown(reason="user left")

    def on_disconnect(participant: rtc.RemoteParticipant) -> None:
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT and not user_present():
            asyncio.ensure_future(leave_unless_user_returns())

    ctx.room.on("participant_disconnected", on_disconnect)


if __name__ == "__main__":
    agents.cli.run_app(server)
