#!/usr/bin/env bash
# Generate .env with random secrets if it does not exist, then render any config
# template that needs a secret baked in.
#
# Idempotent: an existing .env is never overwritten, so `make up` is safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

secret() { openssl rand -hex 24; }

if [[ ! -f .env ]]; then
    echo "generating .env with random secrets"
    cp .env.example .env
    for var in POSTGRES_PASSWORD IDP_ADMIN_PASSWORD IDP_CLIENT_SECRET \
               CONTROL_PLANE_ADMIN_TOKEN CHAT_CLIENT_SECRET; do
        # macOS and GNU sed disagree on -i; write through a temp file instead.
        sed "s|^${var}=.*|${var}=$(secret)|" .env > .env.tmp && mv .env.tmp .env
    done
    sed "s|^GATEWAY_MASTER_KEY=.*|GATEWAY_MASTER_KEY=sk-$(secret)|" .env > .env.tmp && mv .env.tmp .env
    sed "s|^GATEWAY_SALT_KEY=.*|GATEWAY_SALT_KEY=sk-$(secret)|" .env > .env.tmp && mv .env.tmp .env
    chmod 600 .env
else
    echo ".env exists, leaving it alone"
fi

set -a; . ./.env; set +a

# Keycloak imports the realm once, on first start against an empty database. The client
# secrets must therefore be present in the JSON at that moment — they cannot be injected
# as environment variables the way the other services take theirs.
echo "rendering keycloak/realm-export.json"
sed -e "0,/REPLACED_AT_BUNDLE_UP/s||${IDP_CLIENT_SECRET}|" \
    -e "s|REPLACED_AT_BUNDLE_UP|${CHAT_CLIENT_SECRET}|" \
    keycloak/realm-export.template.json > keycloak/realm-export.json

if grep -q REPLACED_AT_BUNDLE_UP keycloak/realm-export.json; then
    echo "error: realm template still has unreplaced placeholders" >&2
    exit 1
fi

echo "ok"
