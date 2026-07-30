#!/usr/bin/env bash
# Provision one user's browser-terminal workspace.
#
#   deploy/bin/provision-workspace.sh <keycloak-username> [--model NAME] [--instructions FILE]
#
# Repeatable and idempotent: run it twice and you get the same workspace with a freshly
# rotated virtual key. This is the mechanism the on-click provisioning API will call; it
# is a script today because the API is a separate item, not because the steps are
# provisional.
#
# What it guarantees, in the order the guarantees matter:
#
#   1. The pod holds THAT user's own `<username>::ide` virtual key, minted through the
#      control plane so the ledger's token hash stays correct. Never the gateway master
#      key. Never a key shared between users.
#   2. ttyd is not published. The Service is ClusterIP with no NodePort, the
#      NetworkPolicy admits 7681/7682 only from the control-plane pod, and ttyd itself
#      demands a credential that only the portal holds. The portal decides WHICH pod you
#      reach from your authenticated name, so a request cannot name somebody else's.
#   3. The pod cannot reach the Kubernetes API, another workspace, the control plane,
#      Postgres or identity. Its in-cluster egress is an allowlist of NAMED services —
#      kube-dns, the gateway, and the MCP tool servers the chat surface uses
#      (enterpriseaiframework-784) — never the namespace and never the pod CIDR. Read the
#      NetworkPolicy in 60-workspace-common.yaml; the list is short on purpose. Do not
#      paraphrase it here: this line said "nothing else we run except the gateway" for one
#      commit after the tool servers were added, which is the same defect as finding 37.
#
# --instructions FILE (enterpriseaiframework-cbf): the terminal agent's standing
# instructions for THIS DEPLOYMENT, not this user. Every workspace pod in the namespace
# mounts one shared ConfigMap (`workspace-tenant-instructions`) as a directory (see
# deploy/k8s/61-workspace.template.yaml) — matching "one control plane" rather than making
# agent behaviour a per-user setting; per-user was considered and rejected because it
# invites every user editing the agent's rules, which is a different product than an
# operator configuring one. First run with no --instructions seeds that ConfigMap from
# deploy/workspace/AGENTS.md — the camp's current rules, unedited — so nothing regresses
# for the tenant that already exists. A later run WITH --instructions overwrites it for
# every workspace in the namespace, present and future; because the mount is a directory
# and not a subPath, a running pod picks up the change on the kubelet's own ConfigMap
# sync, no restart, no image build (subPath mounts do NOT get that sync — see
# deploy/bin/lib/tenant-instructions.sh and deploy/workspace/Dockerfile). Platform facts
# that must hold regardless of what any operator writes here live separately, baked into
# the image at /etc/opencode/PLATFORM.md.
#
# The Agent Skills corpus (enterpriseaiframework-6ff, `chat-skill-*` ConfigMaps,
# deploy/k8s/61-workspace.template.yaml) is NOT created here — deploy/bin/deploy.sh
# creates it once from bundle/skills/ and both the chat surface and every workspace this
# script provisions mount the same ConfigMaps. Nothing for this script to do beyond
# templating the volume/volumeMount pairs already baked into 61-workspace.template.yaml.
set -euo pipefail

cd "$(dirname "$0")/../.."
source deploy/bin/lib/tenant-instructions.sh

NS=enterprise-ai
REGISTRY="${RAIL_REGISTRY:-192.168.2.43:30500}"
IMAGE_NAME="enterprise-ai-workspace"
WORKSPACE_TAG="${WORKSPACE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
IMAGE="${WORKSPACE_IMAGE:-${REGISTRY}/${IMAGE_NAME}:${WORKSPACE_TAG}}"
REALM="${IDP_REALM:-enterprise-ai}"
CLIENT_ID=workspace

USER_NAME="${1:?usage: provision-workspace.sh <keycloak-username> [--model NAME] [--instructions FILE]}"
shift
MODEL="${WORKSPACE_MODEL:-glm-5.2@deepinfra}"
INSTRUCTIONS="${WORKSPACE_INSTRUCTIONS:-}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)        MODEL="$2"; shift 2 ;;
        --instructions) INSTRUCTIONS="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

secret() { kubectl -n "$NS" get secret enterprise-ai-secrets -o jsonpath="{.data.$1}" | base64 -d; }

PUBLIC_BASE_URL="$(secret PUBLIC_BASE_URL)"
ISSUER="$(secret OPENID_ISSUER)"

# The address a browser will use to reach this workspace. NodePorts answer on every node;
# one is named so the OIDC redirect URI is a single fixed string.
WORKSPACE_HOST="${WORKSPACE_HOST:-192.168.2.44}"

echo "==> workspace for ${USER_NAME}"
echo "    image    ${IMAGE}"
echo "    model    ${MODEL}"

# ---------------------------------------------------------------- port-forwards
# Keycloak's admin API and the control plane are both ClusterIP on purpose. Reaching them
# from an operator's kubectl session is the whole access model.
kubectl -n "$NS" port-forward svc/identity 18080:8080 >/dev/null 2>&1 &
IPF=$!
kubectl -n "$NS" port-forward svc/control-plane 18081:8000 >/dev/null 2>&1 &
CPF=$!
trap 'kill $IPF $CPF 2>/dev/null || true' EXIT
IDP=http://localhost:18080
CP=http://localhost:18081
for _ in $(seq 1 40); do
    curl -sS -o /dev/null -m 2 "${IDP}/realms/${REALM}" && break || sleep 1
done
for _ in $(seq 1 40); do
    curl -sS -o /dev/null -m 2 "${CP}/health" && break || sleep 1
done

token=$(curl -sS -X POST "${IDP}/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli \
    -d "username=$(secret IDP_ADMIN_USER)" -d "password=$(secret IDP_ADMIN_PASSWORD)" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
kc() { curl -sS -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" "$@"; }

# ---------------------------------------------------------------- the user must exist
UID_=$(kc -G "${IDP}/admin/realms/${REALM}/users" \
        --data-urlencode "username=${USER_NAME}" --data-urlencode "exact=true" \
        | python3 -c 'import sys,json; u=json.load(sys.stdin); print(u[0]["id"] if u else "")')
[[ -n "$UID_" ]] || { echo "no such user in realm ${REALM}: ${USER_NAME}" >&2; exit 1; }
EMAIL=$(kc "${IDP}/admin/realms/${REALM}/users/${UID_}" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("email") or "")')
# Not used to authorize anything here any more, but a realm account with no email is
# broken in the chat surface and the account console too, so it is still worth refusing
# loudly at the point somebody is being set up.
[[ -n "$EMAIL" ]] || { echo "user ${USER_NAME} has no email; fix the realm account first" >&2; exit 1; }
echo "    identity ${USER_NAME} <${EMAIL}>"

# ---------------------------------------------------------------- the virtual key
# Through the control plane, never straight at the gateway: minting at the gateway would
# leave the ledger's recorded token hash pointing at a key that no longer exists, and
# budget changes would then fail against a deleted key.
curl -sS -o /dev/null -X POST "${CP}/admin/sync" \
    -H "Authorization: Bearer $(secret CONTROL_PLANE_ADMIN_TOKEN)"
ISSUED=$(curl -sS -X POST "${CP}/admin/keys/issue" \
    -H "Authorization: Bearer $(secret CONTROL_PLANE_ADMIN_TOKEN)" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1], "surface": "ide"}))' "$USER_NAME")")
VKEY=$(printf '%s' "$ISSUED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("key",""))')
VALIAS=$(printf '%s' "$ISSUED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("key_alias",""))')
if [[ -z "$VKEY" ]]; then
    echo "control plane did not issue a key: ${ISSUED}" >&2
    exit 1
fi
if [[ "$VALIAS" != "${USER_NAME}::ide" ]]; then
    echo "refusing to provision: expected alias ${USER_NAME}::ide, got '${VALIAS}'" >&2
    exit 1
fi
MASTER="$(secret GATEWAY_MASTER_KEY)"
if [[ "$VKEY" == "$MASTER" ]]; then
    echo "refusing to provision: the issued key is the gateway master key" >&2
    exit 1
fi
echo "    key      ${VALIAS} (rotated)"

# ---------------------------------------------------------------- the pod's secret
# Only the spendable key. The pod no longer authenticates anybody itself — the portal
# does, and reaches this workspace over a ClusterIP the NetworkPolicy admits only from
# the control plane — so there is no client secret, no cookie secret and no email
# allow-list to keep in sync here.
kubectl -n "$NS" create secret generic "ws-${USER_NAME}-key" \
    --from-literal=OPENAI_API_KEY="$VKEY" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

KEYSUM=$(printf '%s' "$VKEY" | sha256sum | cut -c1-16)

# Where a published page can be opened from. Deliberately NOT behind oauth2-proxy: the
# audience is parents, who have no account. Override to a funnel-fronted URL when one
# exists; the LAN NodePort is the honest default.
PUBLISH_URL="${PUBLISH_URL:-https://gateway.tailcb6ef9.ts.net:8443}"

# ---------------------------------------------------------------- apply
kubectl apply -f deploy/k8s/60-workspace-common.yaml >/dev/null

# One ConfigMap for the whole deployment, applied BEFORE the pod template below so a pod
# created by this run always finds it already there — see
# deploy/bin/lib/tenant-instructions.sh for the three cases this covers.
ensure_tenant_instructions "$NS" workspace-tenant-instructions \
    deploy/workspace/AGENTS.md "$INSTRUCTIONS"

sed -e "s|__USER__|${USER_NAME}|g" \
    -e "s|__IMAGE__|${IMAGE}|g" \
    -e "s|__MODEL__|${MODEL}|g" \
    -e "s|__KEYSUM__|${KEYSUM}|g" \
    -e "s|__PUBLISH_URL__|${PUBLISH_URL}|g" \
    deploy/k8s/61-workspace.template.yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" rollout status "deployment/ws-${USER_NAME}" --timeout=600s

echo
echo "  ${USER_NAME}: ${PUBLIC_BASE_URL}/portal/  ->  the Code tab"
echo "  one login, same account as chat. The workshop has no URL of its own."
