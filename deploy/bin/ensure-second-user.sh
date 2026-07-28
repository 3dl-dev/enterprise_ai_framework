#!/usr/bin/env bash
# Create a realm user, so multi-user isolation can be demonstrated rather than asserted.
# One user proves nothing about isolation.
#
#   deploy/bin/ensure-second-user.sh [username] [email]
#
# Each user's password is generated here and stored ONLY in that user's own Secret inside
# the namespace, `workspace-user-<username>`. It is never written to the repo and never
# printed.
#
# WHY THE SECRET IS PER USER
#
# This wrote every user into one shared Secret, `workspace-test-user`. With exactly one
# extra user that was indistinguishable from correct. With two it was neither: the reuse
# lookup below is keyed on the Secret, so creating a THIRD user read the SECOND user's
# password out of it and set it as the third user's own — handing two people one
# credential — and then overwrote the USERNAME field, pointing the live tests at an
# account whose password they no longer held. Found before it ran, not after.
#
# `workspace-test-user` still exists and still means "the account tests-live signs in as".
# It is only ever (re)pointed at the user this script was asked to make when it does not
# already name somebody else, so adding a camper cannot silently steal the test identity.
set -euo pipefail

NS=enterprise-ai
REALM="${IDP_REALM:-enterprise-ai}"
USER_NAME="${1:-student}"
EMAIL="${2:-${USER_NAME}@example.invalid}"

# Keycloak usernames are permissive; this becomes a Secret name and a Kubernetes object
# name, which are not. Reject rather than rewrite — see the same reasoning in
# shell-server.py about project names.
if [[ ! "$USER_NAME" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
    echo "refusing '${USER_NAME}': use lowercase letters, digits and dashes" >&2
    exit 1
fi
USER_SECRET="workspace-user-${USER_NAME}"

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

# Reuse THIS user's stored password if there is one, so reruns do not invalidate a
# credential somebody is already holding. Keyed on the user, never on a shared Secret.
PASS=$(kubectl -n "$NS" get secret "$USER_SECRET" -o jsonpath='{.data.PASSWORD}' 2>/dev/null | base64 -d || true)
# Migration for accounts made before the per-user split: adopt the shared Secret's
# password, but only when it is genuinely this user's.
if [[ -z "$PASS" ]]; then
    prior=$(kubectl -n "$NS" get secret workspace-test-user -o jsonpath='{.data.USERNAME}' 2>/dev/null | base64 -d || true)
    if [[ "$prior" == "$USER_NAME" ]]; then
        PASS=$(kubectl -n "$NS" get secret workspace-test-user -o jsonpath='{.data.PASSWORD}' 2>/dev/null | base64 -d || true)
    fi
fi
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

kubectl -n "$NS" create secret generic "$USER_SECRET" \
    --from-literal=USERNAME="$USER_NAME" \
    --from-literal=EMAIL="$EMAIL" \
    --from-literal=PASSWORD="$PASS" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Point the live tests at this account only if nobody else already holds that role.
# Claiming it unconditionally is what broke: a new camper would repoint the tests at an
# account whose password the shared Secret no longer matched.
holder=$(kubectl -n "$NS" get secret workspace-test-user -o jsonpath='{.data.USERNAME}' 2>/dev/null | base64 -d || true)
if [[ -z "$holder" || "$holder" == "$USER_NAME" ]]; then
    kubectl -n "$NS" create secret generic workspace-test-user \
        --from-literal=USERNAME="$USER_NAME" \
        --from-literal=EMAIL="$EMAIL" \
        --from-literal=PASSWORD="$PASS" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    echo "${USER_NAME} <${EMAIL}> ready; password in secret/${USER_SECRET} (also the live-test account)"
else
    echo "${USER_NAME} <${EMAIL}> ready; password in secret/${USER_SECRET}"
    echo "  live tests keep signing in as '${holder}' — untouched"
fi
