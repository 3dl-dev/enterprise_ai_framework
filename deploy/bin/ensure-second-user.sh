#!/usr/bin/env bash
# Create a second realm user, so two-user isolation can be demonstrated rather than
# asserted. One user proves nothing about isolation.
#
#   deploy/bin/ensure-second-user.sh [username] [email]
#
# The password is generated here and stored only in a Secret inside the namespace
# (`workspace-test-user`), which is where tests-live reads it from. It is never written to
# the repo and never printed.
set -euo pipefail

NS=enterprise-ai
REALM="${IDP_REALM:-enterprise-ai}"
USER_NAME="${1:-student}"
EMAIL="${2:-${USER_NAME}@example.invalid}"

secret() { kubectl -n "$NS" get secret enterprise-ai-secrets -o jsonpath="{.data.$1}" | base64 -d; }

kubectl -n "$NS" port-forward svc/identity 18080:8080 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
IDP=http://localhost:18080
for _ in $(seq 1 40); do curl -sS -o /dev/null -m 2 "${IDP}/realms/${REALM}" && break || sleep 1; done

token=$(curl -sS -X POST "${IDP}/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli \
    -d "username=$(secret IDP_ADMIN_USER)" -d "password=$(secret IDP_ADMIN_PASSWORD)" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
kc() { curl -sS -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" "$@"; }

# Reuse the stored password if this user already exists, so reruns do not invalidate a
# credential the live tests are holding.
PASS=$(kubectl -n "$NS" get secret workspace-test-user -o jsonpath='{.data.PASSWORD}' 2>/dev/null | base64 -d || true)
[[ -n "$PASS" ]] || PASS=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')

# firstName/lastName are required or Keycloak's Verify Profile action halts the login flow
# at a "complete your account" form, which reads as broken SSO rather than a missing field.
payload=$(USER_NAME="$USER_NAME" EMAIL="$EMAIL" python3 -c '
import json, os
u = os.environ["USER_NAME"]
print(json.dumps({"username": u, "email": os.environ["EMAIL"],
                  "firstName": u.capitalize(), "lastName": "User",
                  "enabled": True, "emailVerified": True, "requiredActions": []}))')

existing=$(kc -G "${IDP}/admin/realms/${REALM}/users" \
    --data-urlencode "username=${USER_NAME}" --data-urlencode "exact=true" \
    | python3 -c 'import sys,json; u=json.load(sys.stdin); print(u[0]["id"] if u else "")')
if [[ -z "$existing" ]]; then
    kc -X POST "${IDP}/admin/realms/${REALM}/users" -d "$payload" >/dev/null
    existing=$(kc -G "${IDP}/admin/realms/${REALM}/users" \
        --data-urlencode "username=${USER_NAME}" --data-urlencode "exact=true" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])')
else
    kc -X PUT "${IDP}/admin/realms/${REALM}/users/${existing}" -d "$payload" >/dev/null
fi
kc -X PUT "${IDP}/admin/realms/${REALM}/users/${existing}/reset-password" \
    -d "$(PASS="$PASS" python3 -c '
import json, os
print(json.dumps({"type": "password", "value": os.environ["PASS"], "temporary": False}))')" >/dev/null

kubectl -n "$NS" create secret generic workspace-test-user \
    --from-literal=USERNAME="$USER_NAME" \
    --from-literal=EMAIL="$EMAIL" \
    --from-literal=PASSWORD="$PASS" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "${USER_NAME} <${EMAIL}> ready; password in secret/workspace-test-user"
