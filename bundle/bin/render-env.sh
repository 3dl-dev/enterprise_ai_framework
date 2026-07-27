#!/usr/bin/env bash
# Ensure .env exists and that every required secret has a value, then render config
# templates that need a secret baked in.
#
# Fills gaps rather than regenerating: an existing value is never overwritten, and a
# variable added to the bundle later gets generated on the next `make up` instead of
# forcing a teardown.
set -euo pipefail

cd "$(dirname "$0")/.."

hex() { openssl rand -hex "$1"; }

if [[ ! -f .env ]]; then
    echo "creating .env from .env.example"
    cp .env.example .env
    chmod 600 .env
fi

# name:generator — generator is a command whose stdout becomes the value.
ensure() {
    local var="$1" value="$2"
    if grep -qE "^${var}=.+$" .env; then
        return 0
    fi
    if grep -qE "^${var}=$" .env; then
        # Present but empty: fill it in place, preserving position in the file.
        sed "s|^${var}=$|${var}=${value}|" .env > .env.tmp && mv .env.tmp .env
    else
        printf '%s=%s\n' "$var" "$value" >> .env
    fi
    echo "  generated ${var}"
}

echo "checking secrets"
ensure POSTGRES_PASSWORD           "$(hex 24)"
ensure IDP_ADMIN_PASSWORD          "$(hex 24)"
ensure IDP_CLIENT_SECRET           "$(hex 24)"
ensure CONTROL_PLANE_ADMIN_TOKEN   "$(hex 24)"
ensure GATEWAY_MASTER_KEY          "sk-$(hex 24)"
ensure GATEWAY_SALT_KEY            "sk-$(hex 24)"
ensure CHAT_CLIENT_SECRET          "$(hex 24)"
ensure CHAT_SESSION_SECRET         "$(hex 24)"
ensure CHAT_JWT_SECRET             "$(hex 24)"
ensure CHAT_JWT_REFRESH_SECRET     "$(hex 24)"
# LibreChat requires these at exact lengths: AES-256 key (32 bytes) and IV (16 bytes).
ensure CHAT_CREDS_KEY              "$(hex 32)"
ensure CHAT_CREDS_IV               "$(hex 16)"

# Bootstrap realm user, so a fresh bundle can be signed in to without manual steps.
# The first two are not secrets, but they must exist in .env for post-up to provision
# the account — an .env written before these were added would otherwise skip it silently.
ensure BOOTSTRAP_USER              "baron"
ensure BOOTSTRAP_EMAIL             "baron@3dl.dev"
ensure BOOTSTRAP_PASSWORD          "$(hex 16)"

chmod 600 .env
set -a; . ./.env; set +a

# Keycloak imports the realm once, on first start against an empty database. The client
# secrets must therefore be present in the JSON at that moment — they cannot be injected
# as environment variables the way the other services take theirs.
if [[ ! -f keycloak/realm-export.json ]]; then
    echo "rendering keycloak/realm-export.json"
    sed -e "0,/REPLACED_AT_BUNDLE_UP/s||${IDP_CLIENT_SECRET}|" \
        -e "s|REPLACED_AT_BUNDLE_UP|${CHAT_CLIENT_SECRET}|" \
        keycloak/realm-export.template.json > keycloak/realm-export.json

    if grep -q REPLACED_AT_BUNDLE_UP keycloak/realm-export.json; then
        echo "error: realm template still has unreplaced placeholders" >&2
        exit 1
    fi
else
    echo "keycloak/realm-export.json exists, leaving it alone"
fi

echo "ok"
