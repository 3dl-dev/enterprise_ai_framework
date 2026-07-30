#!/usr/bin/env bash
# Mint rag-api's virtual key and write it into .env.
#
# enterpriseaiframework-c7c: file search's embedding call goes THROUGH the gateway
# (RAG_OPENAI_BASEURL points at it) rather than at a provider directly, so it needs the
# same kind of credential every other surface holds — a virtual key, never a provider
# key, minted directly against the gateway (the control plane deliberately never stores
# raw virtual keys). Idempotent: an existing, still-valid key is kept.
#
# This key is shared by the WHOLE rag-api process, not minted per uploading user — see
# the comment on `rag-api` in bundle/docker-compose.yml and tests/test_file_search.py's
# TestEmbeddingIsBilledThroughTheGateway for what that does and does not prove about
# per-user attribution.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

GATEWAY="http://localhost:${GATEWAY_PORT:-4000}"
ALIAS="rag-api::file-search"

valid_key() {
    local key="$1"
    [[ -n "$key" ]] || return 1
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' -G "${GATEWAY}/key/info" \
        -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" --data-urlencode "key=${key}")
    [[ "$code" == "200" ]]
}

if valid_key "${RAG_VIRTUAL_KEY:-}"; then
    echo "rag-api virtual key already provisioned and valid"
    exit 0
fi

echo "minting rag-api virtual key (${ALIAS})"

# Drop any prior key under this alias so re-provisioning cannot accumulate orphans that
# still authorize traffic.
curl -sS -X POST "${GATEWAY}/key/delete" \
    -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"key_aliases\":[\"${ALIAS}\"]}" >/dev/null 2>&1 || true

key=$(curl -sS -f -X POST "${GATEWAY}/key/generate" \
    -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"key_alias\":\"${ALIAS}\",\"metadata\":{\"surface\":\"file-search\",\"issuer\":\"provision-rag-key\"}}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["key"])')

if [[ -z "$key" ]]; then
    echo "error: gateway returned no key" >&2
    exit 1
fi

if grep -qE '^RAG_VIRTUAL_KEY=' .env; then
    sed "s|^RAG_VIRTUAL_KEY=.*|RAG_VIRTUAL_KEY=${key}|" .env > .env.tmp && mv .env.tmp .env
else
    printf 'RAG_VIRTUAL_KEY=%s\n' "$key" >> .env
fi
chmod 600 .env

echo "rag-api virtual key written to .env"
