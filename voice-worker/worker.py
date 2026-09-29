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
import logging

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, AutoSubscribe, JobContext
from livekit.plugins import openai, silero

import settings

logger = logging.getLogger("eaf-voice")
AGENT_NAME = "eaf-voice"

server = AgentServer()


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
    await session.start(agent=Agent(instructions=""), room=ctx.room)

    def on_disconnect(participant):
        if participant.identity == ctx.room.local_participant.identity:
            return
        logger.info("user left room %s; ending the session", ctx.room.name)
        ctx.shutdown(reason="user left")

    ctx.room.on("participant_disconnected", on_disconnect)


if __name__ == "__main__":
    agents.cli.run_app(server)
