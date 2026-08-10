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

# ---------------------------------------------------------------------- the mailbox
# The agent's email capability (enterpriseaiframework-a4e). /etc/agent is this same
# ConfigMap volume, mounted 0755, so `agent-email` is on PATH for opencode's shell tool
# and for anyone who `kubectl exec`s in. There is NO mail server anywhere in this
# deployment and there must never be one — Baron's ruling is that the agent USES an
# external provider (M365 / Gmail / any IMAP+SMTP host) with the tenant's own mailbox.
# See deploy/agent/agent-email for why this is a CLI and not an MCP server.
#
# APPENDED, not prepended. /etc/agent is a ConfigMap mount, so whoever edits that
# ConfigMap decides what is in it; at the front of PATH a key named `git` or `python3`
# would shadow the real binary for every command the agent runs. At the back, a new key
# can only add a command that did not exist.
export PATH="${PATH}:/etc/agent"

# Whether this agent HAS a mailbox is decided entirely by whether the per-agent Secret
# `agent-<user>-<name>-email` exists; the pod template mounts it `optional: true`, so an
# agent without one simply has no AGENT_EMAIL_* variables and `agent-email` says so.
if [[ -n "${AGENT_EMAIL_SMTP_HOST:-}" ]]; then
    # opencode has to be TOLD the tool exists, and the only channel for that is the
    # `instructions` list in its config — which lives in the workspace image, and Contract
    # 6 forbids editing deploy/workspace/ including that file. So the config is COMPOSED
    # here at boot: the image's config, plus one more instructions entry. Not a rewritten
    # copy of it — a copy would silently fork the provider block and the model catalogue
    # the moment either changed in the image, and nothing would notice until an agent was
    # pinned to a model that no longer exists.
    #
    # jq is in the image (see its Dockerfile). The rendered file goes on the PVC because
    # /etc/opencode is a read-only ConfigMap mount and /home/coder is an emptyDir.
    RENDERED="${XDG_DATA_HOME:-/workspace/.agent-state}/opencode.json"
    # Composed from whatever config is ACTUALLY in effect ($OPENCODE_CONFIG, set just
    # above), not from a second hard-coded copy of the same path: two places naming the
    # image's config is how one of them ends up pointing at a file the other replaced.
    if jq '.instructions += ["/etc/agent/EMAIL.md"]' \
           "$OPENCODE_CONFIG" > "${RENDERED}.tmp" 2>/dev/null \
       && jq -e . "${RENDERED}.tmp" >/dev/null 2>&1 \
       && OPENCODE_CONFIG="${RENDERED}.tmp" opencode debug config >/dev/null 2>&1; then
        # THE THIRD CHECK IS THE ONE THAT MATTERS, and valid JSON is not it. opencode
        # validates against a strict schema and exits non-zero on anything it does not
        # recognise (the Dockerfile records this: an unknown key, even a `_comment`, is a
        # hard error). So a composed file can be perfectly well-formed JSON and still be
        # a config this binary refuses to start on — and since `opencode serve` IS the
        # container's process, that is a CrashLoopBackOff for every agent in the
        # deployment, caused by a documentation file.
        #
        # `opencode debug config` is the binary's own config resolver, run against the
        # candidate before anything commits to it. It is the same command the Dockerfile
        # names for verifying config claims, and it costs about a second, once, at boot.
        mv "${RENDERED}.tmp" "$RENDERED"
        export OPENCODE_CONFIG="$RENDERED"
        echo "email: mailbox configured; opencode config composed at ${RENDERED}"
    else
        # FALL BACK TO THE IMAGE CONFIG AND KEEP GOING. opencode 1.18.7 hard-errors and
        # exits non-zero on a config it cannot parse, so a bad render here would turn a
        # missing tool doc into an agent that never boots — trading the whole surface for
        # a documentation file. The CLI is still on PATH and still works; only the
        # instructions entry is lost, and this line is what says so in `kubectl logs`.
        rm -f "${RENDERED}.tmp"
        echo "email: could not compose opencode config; falling back to the image's." >&2
        echo "  agent-email still works, but opencode was not told about it." >&2
    fi
fi

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
