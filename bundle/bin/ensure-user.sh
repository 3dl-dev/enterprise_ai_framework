#!/usr/bin/env bash
# Create or update a realm user. Idempotent.
#
# Usage: ensure-user.sh <username> [password] [email]
#
# Realm import only runs against an empty database, so users cannot be added by editing
# the template once the bundle has started. This goes through the admin API instead and
# therefore works on a running stack.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

USERNAME="${1:?usage: ensure-user.sh <username> [password] [email]}"
PASSWORD="${2:-}"
EMAIL="${3:-${USERNAME}@example.invalid}"
IDP="http://localhost:${IDP_PORT:-8082}"
REALM="${IDP_REALM:-enterprise-ai}"

token=$(curl -sS -X POST "${IDP}/realms/master/protocol/openid-connect/token" \
    -d "grant_type=password" \
    -d "client_id=admin-cli" \
    -d "username=${IDP_ADMIN_USER:-admin}" \
    -d "password=${IDP_ADMIN_PASSWORD}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

existing=$(curl -sS -G "${IDP}/admin/realms/${REALM}/users" \
    -H "Authorization: Bearer ${token}" \
    --data-urlencode "username=${USERNAME}" --data-urlencode "exact=true" \
    | python3 -c 'import sys,json; u=json.load(sys.stdin); print(u[0]["id"] if u else "")')

if [[ -z "$existing" ]]; then
    echo "creating user ${USERNAME}"
    curl -sS -f -X POST "${IDP}/admin/realms/${REALM}/users" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c '
import json, sys
print(json.dumps({
    "username": sys.argv[1],
    "email": sys.argv[2],
    "enabled": True,
    "emailVerified": True,
}))' "$USERNAME" "$EMAIL")" >/dev/null
    existing=$(curl -sS -G "${IDP}/admin/realms/${REALM}/users" \
        -H "Authorization: Bearer ${token}" \
        --data-urlencode "username=${USERNAME}" --data-urlencode "exact=true" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])')
else
    echo "user ${USERNAME} already exists"
fi

if [[ -n "$PASSWORD" ]]; then
    curl -sS -f -X PUT "${IDP}/admin/realms/${REALM}/users/${existing}/reset-password" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c '
import json, sys
print(json.dumps({"type": "password", "value": sys.argv[1], "temporary": False}))' "$PASSWORD")"
    echo "password set for ${USERNAME}"
fi

echo "${existing}"
