#!/bin/bash
# Agent entrypoint: bring up a RESIDENT opencode daemon and then get out of the way.
#
# THIS IS THE WHOLE SURFACE, AND IT IS DEFINED BY WHAT IT IS NOT.
#
# The Code/workspace surface (deploy/workspace/entrypoint.sh) ends in `exec ttyd ...
# workspace-shell`: ttyd spawns a FRESH `opencode` for EVERY websocket connection, and
# that process dies when the browser disconnects (finding 43 — a 55%-CPU, 712-MB cold
# boot on every single reconnect). That is correct for Code, where the agent is a tool a
# person drives while looking at it.
#
# An Agent is the opposite. Its whole value is being AWAY from it. So here `opencode
# serve` — opencode's headless HTTP server mode, which hosts a session independently of
# any client — is the container's own long-lived process, the one whose liveness IS the
# pod's liveness. No console spawns it. Consoles ATTACH to it (enterpriseaiframework-0e7)
# and detaching does not end the session. Contract 2 of
# docs/design/records/agents-surface.md is this file.
#
# If you ever find yourself wrapping this in ttyd, or making the daemon start on demand,
# you have turned an Agent back into a workspace with a different tab.
#
# Delivered as a ConfigMap (`agent-entrypoint`, created by deploy/bin/provision-agent.sh)
# rather than baked into an image, because the image is the workspace image byte-for-byte
# and Contract 6 forbids touching deploy/workspace/ — including its Dockerfile.
set -euo pipefail

AGENT_USER="${AGENT_USER:?AGENT_USER is not set}"
AGENT_NAME="${AGENT_NAME:?AGENT_NAME is not set}"

# The daemon's own credential. HTTP Basic on every request to the opencode server —
# verified against this exact image (opencode 1.18.7): with the variable unset the server
# logs "server is unsecured" and answers /app with 200 to anyone; with it set the same
# request is 401 and `-u opencode:<password>` is 200.
#
# Same reasoning as WS_INTERNAL_TOKEN on the workspace, and the same refusal: a resident
# agent that silently comes up unauthenticated is exactly the failure nobody notices,
# because nobody is looking at it. The NetworkPolicy (deploy/k8s/63-agent-common.yaml)
# admits only the control-plane pod; this is the second, pod-local lock that holds even
# if the policy does not.
if [[ -z "${OPENCODE_SERVER_PASSWORD:-}" ]]; then
    echo "refusing to start: OPENCODE_SERVER_PASSWORD is not set." >&2
    echo "  It is the credential the console presents to this agent's opencode server." >&2
    echo "  Without it the daemon answers anything that reaches the port." >&2
    exit 1
fi

# Everything durable lives on the PVC, and only on the PVC.
AGENT_WORKDIR="${AGENT_WORKDIR:-/workspace/work}"
mkdir -p "${AGENT_WORKDIR}"

# opencode keeps its sessions in an sqlite db under XDG_DATA_HOME. The pod template points
# that INTO the PVC — the identical fix finding 30 applied to the workspace. For a
# workspace that made "resume my last session" survive a restart; for an agent it is what
# makes the stopped -> running transition resume THE SAME AGENT rather than a new one,
# which is the entire meaning of `replicas: 0` being a pause and not a delete.
mkdir -p "${XDG_DATA_HOME:-/workspace/.agent-state}"

git config --global --add safe.directory '*' 2>/dev/null || true
git config --global user.email "${AGENT_USER}+${AGENT_NAME}@agent.local" 2>/dev/null || true
git config --global user.name "agent ${AGENT_NAME}" 2>/dev/null || true
git config --global init.defaultBranch main 2>/dev/null || true

cd "${AGENT_WORKDIR}"
# A repo, so every change the agent makes unattended has a `git log` and a `git revert`.
# An unattended agent is precisely the one whose edits nobody watched happen.
[[ -d .git ]] || git init -q -b main 2>/dev/null || true

# Named explicitly for the same reason the workspace names it: it covers the paths that
# reach opencode without going through this script, `kubectl exec` above all. It cannot
# live in $HOME/.config — /home/coder is an emptyDir and anything baked there is masked.
export OPENCODE_CONFIG="${OPENCODE_CONFIG:-/etc/opencode/opencode.json}"

# --hostname 0.0.0.0, not loopback: unlike ttyd on the workspace there is no sidecar
# sharing this network namespace, so the console reaches the daemon over the ClusterIP
# Service from another pod. The two controls that replace loopback are the NetworkPolicy
# `from` list and OPENCODE_SERVER_PASSWORD above — neither of which is optional.
#
# --print-logs sends the server's own log to stderr, i.e. to `kubectl logs`. A resident
# process nobody is watching must at least be readable after the fact.
exec opencode serve \
    --hostname 0.0.0.0 \
    --port "${AGENT_SERVE_PORT:-4096}" \
    --print-logs
