#!/usr/bin/env bash
# Provision one user's browser-terminal workspace.
#
#   deploy/bin/provision-workspace.sh <keycloak-username> [--nodeport N] [--model NAME]
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
#   2. ttyd is not reachable. It binds loopback inside the pod; the only published port
#      is oauth2-proxy, which requires a Keycloak login AND an exact match against this
#      one user's email before it will proxy a single byte.
#   3. The pod cannot reach the Kubernetes API, another workspace, or anything else we
#      run except the gateway. See the NetworkPolicy in 60-workspace-common.yaml.
set -euo pipefail

cd "$(dirname "$0")/../.."

NS=enterprise-ai
REGISTRY="${RAIL_REGISTRY:-192.168.2.43:30500}"
IMAGE_NAME="enterprise-ai-workspace"
WORKSPACE_TAG="${WORKSPACE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
IMAGE="${WORKSPACE_IMAGE:-${REGISTRY}/${IMAGE_NAME}:${WORKSPACE_TAG}}"
REALM="${IDP_REALM:-enterprise-ai}"
CLIENT_ID=workspace

USER_NAME="${1:?usage: provision-workspace.sh <keycloak-username> [--nodeport N] [--model NAME]}"
shift
NODEPORT=""
MODEL="${WORKSPACE_MODEL:-glm-5.2@deepinfra}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nodeport) NODEPORT="$2"; shift 2 ;;
        --model)    MODEL="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

secret() { kubectl -n "$NS" get secret enterprise-ai-secrets -o jsonpath="{.data.$1}" | base64 -d; }

PUBLIC_BASE_URL="$(secret PUBLIC_BASE_URL)"
ISSUER="$(secret OPENID_ISSUER)"

# The address a browser will use to reach this workspace. NodePorts answer on every node;
# one is named so the OIDC redirect URI is a single fixed string.
WORKSPACE_HOST="${WORKSPACE_HOST:-192.168.2.44}"

# ---------------------------------------------------------------- port allocation
if [[ -z "$NODEPORT" ]]; then
    existing=$(kubectl -n "$NS" get svc "ws-${USER_NAME}" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)
    if [[ -n "$existing" ]]; then
        NODEPORT="$existing"
    else
        # Deterministic from the username so a reprovision keeps the same URL, with a
        # linear probe because a hash collision must not silently steal another
        # workspace's port. 30400 is the gateway's and is skipped by construction.
        base=$(( 30410 + $(printf '%s' "$USER_NAME" | cksum | cut -d' ' -f1) % 80 ))
        taken=$(kubectl get svc -A -o jsonpath='{range .items[*].spec.ports[*]}{.nodePort}{"\n"}{end}' | sort -u)
        for i in $(seq 0 79); do
            candidate=$(( 30410 + (base - 30410 + i) % 80 ))
            if ! grep -qx "$candidate" <<<"$taken"; then NODEPORT=$candidate; break; fi
        done
        [[ -n "$NODEPORT" ]] || { echo "no free NodePort in 30410-30489" >&2; exit 1; }
    fi
fi
WORKSPACE_URL="http://${WORKSPACE_HOST}:${NODEPORT}"
REDIRECT="${WORKSPACE_URL}/oauth2/callback"

echo "==> workspace for ${USER_NAME}"
echo "    url      ${WORKSPACE_URL}"
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
# oauth2-proxy authorizes on the email claim. A user with no email would be authenticated
# and then rejected by an empty allowlist, which is safe but reads as a broken login.
[[ -n "$EMAIL" ]] || { echo "user ${USER_NAME} has no email; oauth2-proxy authorizes on it" >&2; exit 1; }
echo "    identity ${USER_NAME} <${EMAIL}>"

# ---------------------------------------------------------------- the oauth2-proxy client
CID=$(kc -G "${IDP}/admin/realms/${REALM}/clients" --data-urlencode "clientId=${CLIENT_ID}" \
      | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')
if [[ -z "$CID" ]]; then
    echo "==> creating the ${CLIENT_ID} client"
    kc -X POST "${IDP}/admin/realms/${REALM}/clients" -d "$(python3 -c '
import json, sys
print(json.dumps({
    "clientId": sys.argv[1], "name": "Workspace (browser terminal)",
    "protocol": "openid-connect", "publicClient": False, "standardFlowEnabled": True,
    "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
    "redirectUris": [sys.argv[2]], "webOrigins": [sys.argv[3]],
}))' "$CLIENT_ID" "$REDIRECT" "$WORKSPACE_URL")" >/dev/null
    CID=$(kc -G "${IDP}/admin/realms/${REALM}/clients" --data-urlencode "clientId=${CLIENT_ID}" \
          | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])')
else
    # One client, many workspaces: each provision adds its own callback. The client is
    # shared, but it grants nothing on its own — authorization is the per-pod email
    # allowlist, not the client.
    kc "${IDP}/admin/realms/${REALM}/clients/${CID}" \
      | REDIRECT="$REDIRECT" ORIGIN="$WORKSPACE_URL" python3 -c '
import json, os, sys
c = json.load(sys.stdin)
r = sorted(set(c.get("redirectUris") or []) | {os.environ["REDIRECT"]})
w = sorted(set(c.get("webOrigins") or []) | {os.environ["ORIGIN"]})
print(json.dumps({"redirectUris": r, "webOrigins": w}))' > /tmp/ws-client-$$.json
    kc -X PUT "${IDP}/admin/realms/${REALM}/clients/${CID}" -d "@/tmp/ws-client-$$.json" >/dev/null
    rm -f "/tmp/ws-client-$$.json"
fi
CLIENT_SECRET=$(kc "${IDP}/admin/realms/${REALM}/clients/${CID}/client-secret" \
                | python3 -c 'import sys,json; print(json.load(sys.stdin)["value"])')
[[ -n "$CLIENT_SECRET" ]] || { echo "could not read the ${CLIENT_ID} client secret" >&2; exit 1; }
echo "    client   ${CLIENT_ID} callback registered"

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
kubectl -n "$NS" create secret generic "ws-${USER_NAME}-key" \
    --from-literal=OPENAI_API_KEY="$VKEY" \
    --from-literal=OAUTH2_PROXY_CLIENT_SECRET="$CLIENT_SECRET" \
    --from-literal=OAUTH2_PROXY_COOKIE_SECRET="$(python3 -c '
import base64, secrets
print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')" \
    --from-literal=AUTHORIZED_EMAILS="$EMAIL" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

KEYSUM=$(printf '%s' "$VKEY" | sha256sum | cut -c1-16)

# ---------------------------------------------------------------- apply
kubectl apply -f deploy/k8s/60-workspace-common.yaml >/dev/null

sed -e "s|__USER__|${USER_NAME}|g" \
    -e "s|__EMAIL__|${EMAIL}|g" \
    -e "s|__IMAGE__|${IMAGE}|g" \
    -e "s|__NODEPORT__|${NODEPORT}|g" \
    -e "s|__MODEL__|${MODEL}|g" \
    -e "s|__ISSUER__|${ISSUER}|g" \
    -e "s|__REDIRECT__|${REDIRECT}|g" \
    -e "s|__KEYSUM__|${KEYSUM}|g" \
    deploy/k8s/61-workspace.template.yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" rollout status "deployment/ws-${USER_NAME}" --timeout=600s

echo
echo "  ${USER_NAME}: ${WORKSPACE_URL}"
echo "  sign in with the same Keycloak account used at ${PUBLIC_BASE_URL}"
