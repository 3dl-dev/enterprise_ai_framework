#!/usr/bin/env bash
# Provision one named, RESIDENT agent instance.
#
#   deploy/bin/provision-agent.sh <keycloak-username> <agent-name> [--model NAME]
#
# A PARALLEL script to deploy/bin/provision-workspace.sh, not a generalisation of it.
# Contract 6 of docs/design/records/agents-surface.md freezes the Code/workspace surface
# byte-for-byte — the camp runs on it — so this file, deploy/k8s/63-agent-common.yaml,
# deploy/k8s/64-agent.template.yaml and deploy/agent/entrypoint.sh sit BESIDE the frozen
# set and never edit it. tests/test_agents_code_untouched.py makes that mechanical.
#
# What it guarantees, in the order the guarantees matter:
#
#   1. RESIDENCY. The pod's own process is `opencode serve` — a headless daemon that holds
#      a session with NO console attached and keeps holding it across every connect and
#      disconnect. This is the entire difference from Code, where ttyd spawns a fresh
#      opencode per websocket and it dies with the connection (finding 43). An agent that
#      needed a browser open would be a workspace with a different tab.
#   2. Object names are `agent-<user>-<name>` (Contract 1): the PVC, the Deployment, the
#      Service and the Secret. One greppable family, mirroring `ws-<user>`.
#   3. The opencode server is not published. The Service is ClusterIP with no NodePort,
#      the NetworkPolicy admits 4096 only from the control-plane pod, and the daemon
#      itself demands HTTP Basic. The portal decides WHICH agent you reach from your
#      authenticated name (enterpriseaiframework-0e7), so a request cannot name someone
#      else's.
#   4. The pod cannot reach the Kubernetes API, a workspace, another agent, the control
#      plane, Postgres or identity. Its in-cluster egress is an allowlist of NAMED
#      services — kube-dns, the gateway, and the MCP tool servers — never the namespace
#      and never the pod CIDR. Read the NetworkPolicy in 63-agent-common.yaml; do not
#      paraphrase it here, that is how finding 37 happened.
#
# IDEMPOTENT, AND DELIBERATELY NON-DISRUPTIVE. Re-running this for a healthy agent must
# not restart it: restarting an agent ends the resident session that is the whole product.
# So, unlike provision-workspace.sh, this does NOT rotate a credential on every run, and
# the pod template's rollout annotation tracks the entrypoint rather than the key.
#
# SCOPE, STATED RATHER THAN IMPLIED. This item (enterpriseaiframework-055) provisions the
# pod and its isolation. It does NOT mint the agent's virtual key: Contract 1's
# `<user>::agents/<name>` alias needs an additive change to gateway.py and a parallel
# issuance path, which is enterpriseaiframework-39d. Until that lands, this script writes
# an obviously-unusable sentinel into the Secret and says so loudly on every run — the
# agent is resident and reachable, and its inference will fail with a 401 from the
# gateway. A sentinel that cannot be mistaken for a key is the honest version of "not
# implemented yet"; a plausible-looking placeholder is not.
set -euo pipefail

cd "$(dirname "$0")/../.."

NS=enterprise-ai
REGISTRY="${RAIL_REGISTRY:-192.168.2.43:30500}"
IMAGE_NAME="enterprise-ai-workspace"
# The SAME image the Code surface runs, derived the same way provision-workspace.sh
# derives it — including the WORKSPACE_TAG/WORKSPACE_IMAGE overrides, because the tag that
# is actually deployed on a cluster is frequently not this checkout's HEAD. Reusing the
# artefact rather than building an agent image is how this surface gets a pinned opencode
# without touching one byte of deploy/workspace/.
WORKSPACE_TAG="${WORKSPACE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
IMAGE="${AGENT_IMAGE:-${WORKSPACE_IMAGE:-${REGISTRY}/${IMAGE_NAME}:${WORKSPACE_TAG}}}"

USAGE="usage: provision-agent.sh <keycloak-username> <agent-name> [--model NAME]"
USER_NAME="${1:?${USAGE}}"
AGENT_NAME="${2:?${USAGE}}"
shift 2
MODEL="${AGENT_MODEL:-glm-5.2@deepinfra}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------- names, constrained
# The SAME pattern the workspace already enforces on project names
# (deploy/workspace/shell-server.py: SLUG_OK). Constrained, not sanitised, for the reason
# that file gives: a rejected name is easy to explain, a silently rewritten one is not.
# It is also load-bearing for Contract 1's alias grammar `<user>::agents/<name>` — it
# guarantees <name> contains no `::` and no `/`.
SLUG='^[a-z0-9][a-z0-9-]{0,38}$'
if [[ ! "$AGENT_NAME" =~ $SLUG ]]; then
    echo "refusing: agent name '${AGENT_NAME}' must match ${SLUG}" >&2
    exit 1
fi
if [[ ! "$USER_NAME" =~ $SLUG ]]; then
    echo "refusing: username '${USER_NAME}' must match ${SLUG} to be part of an object name" >&2
    exit 1
fi

OBJ="agent-${USER_NAME}-${AGENT_NAME}"
# RFC 1123 caps a k8s object name at 63 characters. REFUSE rather than truncate: a
# truncated name that collides with another user's agent is the one failure mode worth
# failing loudly on, because its symptom is two people sharing one agent's PVC.
if (( ${#OBJ} > 63 )); then
    echo "refusing: object name '${OBJ}' is ${#OBJ} characters, over the RFC 1123 limit of 63." >&2
    echo "  Shorten the agent name. This is not truncated on purpose: a truncated name can" >&2
    echo "  collide with another user's agent and silently share its volume." >&2
    exit 1
fi

echo "==> agent ${AGENT_NAME} for ${USER_NAME}"
echo "    objects  ${OBJ}"
echo "    image    ${IMAGE}"
echo "    model    ${MODEL}"

# ---------------------------------------------------------------- shared objects
# ServiceAccount + NetworkPolicy for the agents component. Applied here for the same
# reason provision-workspace.sh applies its own common file: an agent must never be able
# to exist before the policy that fences it does.
kubectl apply -f deploy/k8s/63-agent-common.yaml >/dev/null

# The resident entrypoint, deployment-wide (one control plane), delivered as a ConfigMap
# because the image is the workspace image and Contract 6 forbids rebuilding it.
kubectl -n "$NS" create configmap agent-entrypoint \
    --from-file=entrypoint.sh=deploy/agent/entrypoint.sh \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
CFGSUM=$(sha256sum deploy/agent/entrypoint.sh | cut -c1-16)

# ---------------------------------------------------------------- the pod's secret
# Read what is already there FIRST. Re-provisioning must not roll a credential out from
# under a running console, and must not silently replace a real key with the sentinel.
existing() {
    kubectl -n "$NS" get secret "${OBJ}-key" -o "jsonpath={.data.$1}" 2>/dev/null \
        | base64 -d 2>/dev/null || true
}
SERVER_PASSWORD="$(existing OPENCODE_SERVER_PASSWORD)"
if [[ -z "$SERVER_PASSWORD" ]]; then
    SERVER_PASSWORD="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-32)"
fi

# The sentinel is spelled so that anyone who greps a 401 finds the item that fixes it.
KEY_SENTINEL="unset-pending-enterpriseaiframework-39d"
API_KEY="${AGENT_OPENAI_API_KEY:-$(existing OPENAI_API_KEY)}"
if [[ -z "$API_KEY" ]]; then
    API_KEY="$KEY_SENTINEL"
fi

kubectl -n "$NS" create secret generic "${OBJ}-key" \
    --from-literal=OPENCODE_SERVER_PASSWORD="$SERVER_PASSWORD" \
    --from-literal=OPENAI_API_KEY="$API_KEY" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# ---------------------------------------------------------------- apply
sed -e "s|__USER__|${USER_NAME}|g" \
    -e "s|__NAME__|${AGENT_NAME}|g" \
    -e "s|__IMAGE__|${IMAGE}|g" \
    -e "s|__MODEL__|${MODEL}|g" \
    -e "s|__CFGSUM__|${CFGSUM}|g" \
    deploy/k8s/64-agent.template.yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" rollout status "deployment/${OBJ}" --timeout=600s

if [[ "$API_KEY" == "$KEY_SENTINEL" ]]; then
    echo
    echo "  NOTE: ${OBJ} holds no virtual key yet."
    echo "  The daemon is resident and the console can attach, but every model call will"
    echo "  fail at the gateway until enterpriseaiframework-39d mints ${USER_NAME}::agents/${AGENT_NAME}."
fi

echo
echo "  ${OBJ}: resident. \`opencode serve\` holds the session with nothing attached;"
echo "  stop it with \`kubectl -n ${NS} scale deploy/${OBJ} --replicas=0\` (PVC kept)."
