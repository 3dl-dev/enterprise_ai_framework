#!/usr/bin/env bash
# Build the two images tests-live/test_voice_live.py runs (enterpriseaiframework-82e), on the
# cluster with kaniko (no local docker). Prints the two references to export.
#
#   tests-live/build_voice_images.sh
#
# The control-plane context is trimmed to what its Dockerfile copies (no tests), and the ConfigMap
# that carries it is applied SERVER-SIDE: `kubectl apply` records the whole object in a
# last-applied annotation, which passes the 256KiB annotation cap once the vendored
# livekit-client (~400KB) is in the tree. deploy.sh builds the control plane with docker and is
# unaffected.
set -euo pipefail
cd "$(dirname "$0")/.."

REG="${RAIL_REGISTRY:-192.168.2.43:30500}"
TAG="${VOICE_LIVE_TAG:-82e-live}"
export RAIL_REGISTRY="$REG" KANIKO_CPU_REQUEST="${KANIKO_CPU_REQUEST:-50m}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/cp"
cp control-plane/Dockerfile control-plane/requirements.txt "$work/cp/"
cp -r control-plane/app "$work/cp/app"
find "$work/cp" -name __pycache__ -prune -exec rm -rf {} +

sed '0,/| kubectl apply -f - >\/dev\/null/s//| kubectl apply --server-side -f - >\/dev\/null/' \
    deploy/bin/kaniko-build.sh > "$work/kaniko-build.sh"
chmod +x "$work/kaniko-build.sh"

"$work/kaniko-build.sh" "$work/cp" "$REG/enterprise-ai-control-plane:$TAG" >&2
deploy/bin/kaniko-build.sh voice-worker "$REG/eaf-voice-worker:$TAG" >&2

echo "export VOICE_LIVE_CP_IMAGE=$REG/enterprise-ai-control-plane:$TAG"
echo "export VOICE_LIVE_WORKER_IMAGE=$REG/eaf-voice-worker:$TAG"
