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

# --- Compose isolation (enterpriseaiframework-0e3) ---
#
# A linked git worktree shares this host with the primary checkout. docker-compose.yml's
# `name:` is fixed ("enterprise-ai") and every published port is a fixed default, so a
# worktree's `docker compose up/down` used to operate on the SAME containers as whatever
# is already running — this is what let the wave-1 veracity adversary tear down the
# primary bundle's mcp-echo. Fix: give a worktree its own project name and its own port
# block; leave the primary checkout exactly as it was.
#
# Detection: a linked worktree's `git rev-parse --git-dir` (its own
# .git/worktrees/<name>) differs from `--git-common-dir` (the shared .git of the primary
# checkout); for the primary checkout the two are equal.
#
# Ports are a deterministic offset derived from the worktree's absolute path, not a
# Docker-assigned dynamic port. Dynamic ports are only known AFTER the container starts,
# but Keycloak bakes its externally-visible URL (KC_HOSTNAME, built from IDP_HTTPS_PORT)
# into itself at boot, and the chat surface does the same for DOMAIN_CLIENT/OPENID_ISSUER
# from CHAT_PORT/IDP_HTTPS_PORT — both need the real host port BEFORE `up`, which a
# deterministic pre-computed offset supplies and a Docker-assigned ephemeral port cannot
# (short of a second restart once the port becomes known). Distinct port ranges was the
# only one of the two options in the item description compatible with that constraint.
#
# The offset is a multiple of 100 in [100, 20000]: no pairwise difference between the six
# base ports below (3080/4000/8081/8082/8090/8443) is a multiple of 100, so no worktree's
# port can ever collide with the primary checkout's or with another worktree's whose hash
# lands on a different bucket (200 buckets; a same-bucket collision between two worktrees
# is possible but astronomically unlikely, and would be visibly loud — `make up` fails to
# bind — not a silent teardown of someone else's containers). The ceiling of 20000 keeps
# every derived port under 30000, clear of the k3s NodePort range (30380/30400/30500)
# this host also uses.
# Unlike ensure(), this always overwrites — the six port variables below carry non-empty
# defaults in .env.example (so a hand-copied .env is usable out of the box), which means
# ensure()'s fill-if-empty rule would never touch them once .env exists. A worktree's
# ports must track its path-derived offset unconditionally, or a bundle copied/rehomed
# between paths, or a stale .env left over from before this fix, would keep colliding
# ports. This intentionally means a worktree's ports cannot be hand-overridden in .env —
# isolation correctness wins over that customization.
set_var() {
    local var="$1" value="$2"
    if grep -qE "^${var}=" .env; then
        sed "s|^${var}=.*|${var}=${value}|" .env > .env.tmp && mv .env.tmp .env
    else
        printf '%s=%s\n' "$var" "$value" >> .env
    fi
    echo "  set ${var}=${value}"
}

# Both are resolved to absolute physical paths before comparing. git reports these
# INCONSISTENTLY when the working directory is a subdirectory of the checkout, which this
# script always is (it cd's to bundle/ above): --git-dir comes back absolute
# (/path/to/repo/.git) while --git-common-dir comes back relative (../.git). Comparing the
# raw strings therefore reports "different" for the PRIMARY checkout, which sent it down
# the worktree branch and renamed the main bundle's compose project out from under a
# running stack. Caught by the integration gate, not by the worktree tests — the worktree
# case is unequal either way, so only the primary checkout could expose it.
_abs_git_path() {
    local p
    p="$(git rev-parse "$1" 2>/dev/null)" || return 0
    [[ -z "$p" ]] && return 0
    ( cd "$p" 2>/dev/null && pwd -P ) || printf '%s' "$p"
}
GIT_DIR="$(_abs_git_path --git-dir)"
GIT_COMMON_DIR="$(_abs_git_path --git-common-dir)"

hex_hash() { printf '%s' "$1" | cksum | cut -d' ' -f1; }

if [[ -n "$GIT_DIR" && "$GIT_DIR" != "$GIT_COMMON_DIR" ]]; then
    WORKTREE_PATH="$(git rev-parse --show-toplevel)"
    HASH="$(hex_hash "$WORKTREE_PATH")"
    OFFSET=$(( (HASH % 200 + 1) * 100 ))
    SHORT_HASH="$(hex_hash "${WORKTREE_PATH}:name")"
    echo "linked worktree detected (${WORKTREE_PATH}); isolating compose project (offset +${OFFSET})"
    set_var COMPOSE_PROJECT_NAME "enterprise-ai-${SHORT_HASH:0:8}"
    set_var GATEWAY_PORT         "$(( 4000 + OFFSET ))"
    set_var CONTROL_PLANE_PORT   "$(( 8081 + OFFSET ))"
    set_var IDP_PORT             "$(( 8082 + OFFSET ))"
    set_var IDP_HTTPS_PORT       "$(( 8443 + OFFSET ))"
    set_var FAKEPROVIDER_PORT    "$(( 8090 + OFFSET ))"
    set_var CHAT_PORT            "$(( 3080 + OFFSET ))"
else
    # Self-heal a .env written while the detection above was broken. That bug rendered the
    # PRIMARY checkout as a worktree, so this file can already carry a derived project name
    # and a full offset port block. ensure() would not repair it — the values are non-empty,
    # so it leaves them alone, and the primary bundle would keep coming up under the wrong
    # identity on the wrong ports for as long as the file survived. Only the derived shape
    # (enterprise-ai-<digits>, written by set_var above) is reset, so a deliberately
    # customised project name or port is left alone.
    if grep -qE '^COMPOSE_PROJECT_NAME=enterprise-ai-[0-9]+$' .env; then
        echo "primary checkout carries a worktree-derived project name; resetting to defaults"
        set_var COMPOSE_PROJECT_NAME "enterprise-ai"
        set_var GATEWAY_PORT       "4000"
        set_var CONTROL_PLANE_PORT "8081"
        set_var IDP_PORT           "8082"
        set_var IDP_HTTPS_PORT     "8443"
        set_var FAKEPROVIDER_PORT  "8090"
        set_var CHAT_PORT          "3080"
    else
        ensure COMPOSE_PROJECT_NAME "enterprise-ai"
    fi
fi

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
    # CHAT_PORT is baked into the chat client's redirectUris/webOrigins (enterpriseaiframework-0e3):
    # Keycloak validates redirect_uri by exact host+port match, so a worktree bundle whose
    # CHAT_PORT differs from the template's fixed default got a 400 on every login test —
    # found by actually running two concurrent bundles, not by inspection.
    sed -e "0,/REPLACED_AT_BUNDLE_UP/s||${IDP_CLIENT_SECRET}|" \
        -e "s|REPLACED_AT_BUNDLE_UP|${CHAT_CLIENT_SECRET}|" \
        -e "s|REPLACED_CHAT_PORT_AT_BUNDLE_UP|${CHAT_PORT:-3080}|g" \
        keycloak/realm-export.template.json > keycloak/realm-export.json

    if grep -qE 'REPLACED_AT_BUNDLE_UP|REPLACED_CHAT_PORT_AT_BUNDLE_UP' keycloak/realm-export.json; then
        echo "error: realm template still has unreplaced placeholders" >&2
        exit 1
    fi
else
    echo "keycloak/realm-export.json exists, leaving it alone"
fi

echo "ok"
