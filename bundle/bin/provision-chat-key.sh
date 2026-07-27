#!/usr/bin/env bash
# Mint the chat surface's virtual key and write it into .env.
#
# The chat surface is a shared client: many people sign in to one deployment, so it holds
# one surface key rather than a key each. Per-person attribution still works because the
# surface forwards the authenticated user, which the gateway records as end_user.
#
# The control plane deliberately never stores raw virtual keys, so the operator mints this
# one directly against the gateway. Idempotent: an existing, still-valid key is kept.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

GATEWAY="http://localhost:${GATEWAY_PORT:-4000}"
ALIAS="chat-surface::chat"

valid_key() {
    local key="$1"
    [[ -n "$key" ]] || return 1
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' -G "${GATEWAY}/key/info" \
        -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" --data-urlencode "key=${key}")
    [[ "$code" == "200" ]]
}

if valid_key "${CHAT_VIRTUAL_KEY:-}"; then
    echo "chat virtual key already provisioned and valid"
    exit 0
fi

echo "minting chat surface virtual key (${ALIAS})"

# Drop any prior key under this alias so re-provisioning cannot accumulate orphans that
# still authorize traffic.
curl -sS -X POST "${GATEWAY}/key/delete" \
    -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"key_aliases\":[\"${ALIAS}\"]}" >/dev/null 2>&1 || true

key=$(curl -sS -f -X POST "${GATEWAY}/key/generate" \
    -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"key_alias\":\"${ALIAS}\",\"metadata\":{\"surface\":\"chat\",\"issuer\":\"provision-chat-key\"}}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["key"])')

if [[ -z "$key" ]]; then
    echo "error: gateway returned no key" >&2
    exit 1
fi

if grep -qE '^CHAT_VIRTUAL_KEY=' .env; then
    sed "s|^CHAT_VIRTUAL_KEY=.*|CHAT_VIRTUAL_KEY=${key}|" .env > .env.tmp && mv .env.tmp .env
else
    printf 'CHAT_VIRTUAL_KEY=%s\n' "$key" >> .env
fi
chmod 600 .env

echo "chat virtual key written to .env"
