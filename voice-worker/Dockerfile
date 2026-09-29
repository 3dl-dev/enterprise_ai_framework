# The EAF voice worker image. Build on the cluster (no local docker):
#   deploy/bin/kaniko-build.sh voice-worker $RAIL_REGISTRY/eaf-voice-worker:<tag>
#
# AIR-GAP: Silero VAD's ONNX weights are package data of livekit-plugins-silero (MIT weights,
# Apache-2.0 plugin). The final RUN loads the VAD with HF_HUB_OFFLINE=1, so a build that needed
# the network to produce a working VAD fails here, not at runtime.
# No turn-detector, no noise-cancellation, no LiveKit Inference: see worker.py.
FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY settings.py worker.py ./

ENV HF_HUB_OFFLINE=1 PYTHONUNBUFFERED=1
RUN python -c "from livekit.plugins import silero; silero.VAD.load(); print('silero vad loaded offline')" \
    && ! python -c "import livekit.local_inference" 2>/dev/null \
    && echo "livekit-local-inference is absent (LiveKit Model License material not in the image)"

EXPOSE 8081
CMD ["python", "worker.py", "start"]
