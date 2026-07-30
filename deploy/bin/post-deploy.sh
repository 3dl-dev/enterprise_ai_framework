#!/usr/bin/env bash
# Post-deploy reconcile for the cluster — the k8s equivalent of bundle/bin/post-up.sh.
#
# Keycloak imports a realm only against an empty database, so once the realm exists these
# changes cannot be made by editing the realm Secret and redeploying. They go through the
# admin API instead, which works on a running cluster and is idempotent.
#
#   PUBLIC_BASE_URL=https://host:8443 deploy/bin/post-deploy.sh
set -euo pipefail

cd "$(dirname "$0")/../.."
NS=enterprise-ai

PUBLIC_BASE_URL="${PUBLIC_BASE_URL:?PUBLIC_BASE_URL is required}"
REALM="${IDP_REALM:-enterprise-ai}"

secret() { kubectl -n "$NS" get secret enterprise-ai-secrets -o jsonpath="{.data.$1}" | base64 -d; }

ADMIN_USER="$(secret IDP_ADMIN_USER)"
ADMIN_PASS="$(secret IDP_ADMIN_PASSWORD)"

# Port-forward rather than exposing Keycloak's admin API. It has no business being
# reachable from anywhere but an operator's kubectl session.
kubectl -n "$NS" port-forward svc/identity 18080:8080 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
    curl -sS -o /dev/null -m 2 "http://localhost:18080/realms/${REALM}" && break || sleep 1
done

IDP=http://localhost:18080
token=$(curl -sS -X POST "${IDP}/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli \
    -d "username=${ADMIN_USER}" -d "password=${ADMIN_PASS}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

echo "==> pointing the librechat client at ${PUBLIC_BASE_URL}"
CID=$(curl -sS -G "${IDP}/admin/realms/${REALM}/clients" \
    -H "Authorization: Bearer ${token}" --data-urlencode "clientId=librechat" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')
[[ -n "$CID" ]] || { echo "librechat client not found in realm ${REALM}" >&2; exit 1; }

curl -sS -f -X PUT "${IDP}/admin/realms/${REALM}/clients/${CID}" \
    -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
    -d "$(PUBLIC_BASE_URL="$PUBLIC_BASE_URL" python3 -c '
import json, os
pub = os.environ["PUBLIC_BASE_URL"].rstrip("/")
print(json.dumps({
    "redirectUris": [f"{pub}/oauth/openid/callback"],
    "webOrigins": [pub],
}))')" >/dev/null
echo "    redirect URIs updated"

echo "==> bootstrap user"
BU="$(secret BOOTSTRAP_USER 2>/dev/null || echo '')"
if [[ -n "$BU" ]]; then
    BP="$(secret BOOTSTRAP_PASSWORD)"
    BE="$(secret BOOTSTRAP_EMAIL 2>/dev/null || echo "${BU}@example.invalid")"
    existing=$(curl -sS -G "${IDP}/admin/realms/${REALM}/users" \
        -H "Authorization: Bearer ${token}" \
        --data-urlencode "username=${BU}" --data-urlencode "exact=true" \
        | python3 -c 'import sys,json; u=json.load(sys.stdin); print(u[0]["id"] if u else "")')

    # firstName/lastName are required: without them Keycloak's Verify Profile action
    # halts the login flow at a "complete your account" form, which reads as broken SSO.
    payload=$(BU="$BU" BE="$BE" python3 -c '
import json, os
u = os.environ["BU"]
print(json.dumps({"username": u, "email": os.environ["BE"], "firstName": u.capitalize(),
                  "lastName": "User", "enabled": True, "emailVerified": True,
                  "requiredActions": []}))')

    if [[ -z "$existing" ]]; then
        curl -sS -f -X POST "${IDP}/admin/realms/${REALM}/users" \
            -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
            -d "$payload" >/dev/null
        existing=$(curl -sS -G "${IDP}/admin/realms/${REALM}/users" \
            -H "Authorization: Bearer ${token}" \
            --data-urlencode "username=${BU}" --data-urlencode "exact=true" \
            | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])')
    else
        curl -sS -f -X PUT "${IDP}/admin/realms/${REALM}/users/${existing}" \
            -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
            -d "$payload" >/dev/null
    fi
    curl -sS -f -X PUT "${IDP}/admin/realms/${REALM}/users/${existing}/reset-password" \
        -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
        -d "$(BP="$BP" python3 -c 'import json,os; print(json.dumps({"type":"password","value":os.environ["BP"],"temporary":False}))')" >/dev/null
    echo "    ${BU} ready"
fi

echo "==> chat surface virtual key"
# Must be minted against THIS gateway. A key from the local compose bundle lives in a
# different database and is rejected here — and LibreChat does not surface that: it
# silently falls back to the hardcoded model list in librechat.yaml, so the surface looks
# healthy while offering models it cannot reach.
kubectl -n "$NS" port-forward svc/gateway 14000:4000 >/dev/null 2>&1 &
GPF=$!
trap 'kill $PF $GPF 2>/dev/null || true' EXIT
sleep 3
GW=http://localhost:14000
MK="$(secret GATEWAY_MASTER_KEY)"
CHAT_KEY=$(curl -sS -o /dev/null -w '%{http_code}' -G "${GW}/key/info" \
    -H "Authorization: Bearer ${MK}" --data-urlencode "key=$(secret CHAT_VIRTUAL_KEY 2>/dev/null || echo x)")
if [[ "$CHAT_KEY" != "200" ]]; then
    curl -sS -X POST "${GW}/key/delete" -H "Authorization: Bearer ${MK}" \
        -H "Content-Type: application/json" -d '{"key_aliases":["chat-surface::chat"]}' >/dev/null 2>&1 || true
    NEW=$(curl -sS -X POST "${GW}/key/generate" -H "Authorization: Bearer ${MK}" \
        -H "Content-Type: application/json" \
        -d '{"key_alias":"chat-surface::chat","metadata":{"surface":"chat","issuer":"post-deploy"}}' \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["key"])')
    kubectl -n "$NS" patch secret enterprise-ai-secrets --type=json \
        -p "[{\"op\":\"replace\",\"path\":\"/data/CHAT_VIRTUAL_KEY\",\"value\":\"$(printf '%s' "$NEW" | base64 -w0)\"}]" >/dev/null
    kubectl -n "$NS" rollout restart deployment/chat >/dev/null
    kubectl -n "$NS" rollout status deployment/chat --timeout=300s >/dev/null
    echo "    minted and chat restarted"
else
    echo "    existing key is valid"
fi

echo "==> rag-api (file search, enterpriseaiframework-c7c) virtual key"
# Same reasoning as the chat surface key above: minted against THIS gateway, not carried
# over from the local compose bundle. Without a valid key here rag-api's embedding calls
# 401 against the gateway and every file upload fails at the embed step.
RAG_KEY=$(curl -sS -o /dev/null -w '%{http_code}' -G "${GW}/key/info" \
    -H "Authorization: Bearer ${MK}" --data-urlencode "key=$(secret RAG_VIRTUAL_KEY 2>/dev/null || echo x)")
if [[ "$RAG_KEY" != "200" ]]; then
    curl -sS -X POST "${GW}/key/delete" -H "Authorization: Bearer ${MK}" \
        -H "Content-Type: application/json" -d '{"key_aliases":["rag-api::file-search"]}' >/dev/null 2>&1 || true
    NEW_RAG=$(curl -sS -X POST "${GW}/key/generate" -H "Authorization: Bearer ${MK}" \
        -H "Content-Type: application/json" \
        -d '{"key_alias":"rag-api::file-search","metadata":{"surface":"file-search","issuer":"post-deploy"}}' \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["key"])')
    kubectl -n "$NS" patch secret enterprise-ai-secrets --type=json \
        -p "[{\"op\":\"replace\",\"path\":\"/data/RAG_VIRTUAL_KEY\",\"value\":\"$(printf '%s' "$NEW_RAG" | base64 -w0)\"}]" >/dev/null
    kubectl -n "$NS" rollout restart deployment/rag-api >/dev/null
    kubectl -n "$NS" rollout status deployment/rag-api --timeout=300s >/dev/null
    echo "    minted and rag-api restarted"
else
    echo "    existing key is valid"
fi

echo "==> reconciling identity into virtual keys"
kubectl -n "$NS" port-forward svc/control-plane 18081:8000 >/dev/null 2>&1 &
CPF=$!
trap 'kill $PF $CPF 2>/dev/null || true' EXIT
sleep 3
curl -sS -X POST "http://localhost:18081/admin/sync" \
    -H "Authorization: Bearer $(secret CONTROL_PLANE_ADMIN_TOKEN)" | python3 -m json.tool | head -8

echo
echo "  Sign in at ${PUBLIC_BASE_URL}"
