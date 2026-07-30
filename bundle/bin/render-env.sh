#!/usr/bin/env bash
# Ensure .env exists and that every required secret has a value, then render config
# templates that need a secret baked in.
#
# Fills gaps rather than regenerating: an existing value is never overwritten, and a
# variable added to the bundle later gets generated on the next `make up` instead of
# forcing a teardown.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

hex() { openssl rand -hex "$1"; }

if [[ ! -f .env ]]; then
    echo "creating .env from .env.example"
    cp .env.example .env
    chmod 600 .env
fi

# name:generator — generator is a command whose stdout becomes the value.
#
# The fill-in-place branch used to be `sed "s|^${var}=$|${var}=${value}|"`
# (enterpriseaiframework-082 wave 18's defect #1): GNU sed's replacement string
# interprets a literal `\n` in ${value} as an actual newline, so any generated value
# containing that two-character sequence — a PEM collapsed to one line, which is
# exactly the form render-codeapi-keys.py produces — came out split across three lines
# in .env. `--env-file` (and every `set -a; . ./.env` sourcing) then kept only the
# first line, the surface booted fine, and it failed later at signing time with a
# truncated key. python does the substitution with no shell/sed string interpolation
# of ${value} at all, so this class of corruption cannot recur.
ensure() {
    local var="$1" value="$2"
    if grep -qE "^${var}=.+$" .env; then
        return 0
    fi
    if grep -qE "^${var}=$" .env; then
        # Present but empty: fill it in place, preserving position in the file.
        VAR="$var" VALUE="$value" python3 - <<'PYEOF'
import os
env_path = ".env"
var = os.environ["VAR"]
value = os.environ["VALUE"]
with open(env_path) as f:
    lines = f.readlines()
target = f"{var}=\n"
for i, line in enumerate(lines):
    if line == target:
        lines[i] = f"{var}={value}\n"
        break
with open(env_path, "w") as f:
    f.writelines(lines)
PYEOF
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

# The bare name `enterprise-ai` belongs to the PRIMARY checkout and to nothing else. A
# checkout only earns it by positively proving it is the primary: inside a git repository,
# with --git-dir and --git-common-dir agreeing. Everything else — a linked worktree, and
# equally a plain copy, an extracted tarball, or an agent's scratchpad checkout with no
# .git at all — gets a derived name.
#
# The first version keyed on "is this a LINKED WORKTREE" and let everything else fall
# through to the primary name. A non-git checkout has no --git-dir, so GIT_DIR came back
# empty, the condition was false, and it took `enterprise-ai` — then `docker compose up`
# there RECREATED the primary's containers with the copy's own bind-mount paths. When that
# directory was later cleaned up, the mounts pointed at nothing and identity crash-looped
# on a missing /certs/identity.crt. Observed, not hypothesised (enterpriseaiframework-35a).
#
# So the test is now "prove you are the primary", not "prove you are a worktree". Absence
# of evidence resolves to isolation, which is the safe direction: a needlessly isolated
# bundle costs some ports, while a needlessly shared one destroys somebody's running stack.
IS_PRIMARY=0
if [[ -n "$GIT_DIR" && "$GIT_DIR" == "$GIT_COMMON_DIR" ]]; then
    IS_PRIMARY=1
fi

if [[ "$IS_PRIMARY" -eq 0 ]]; then
    # `git rev-parse --show-toplevel` is unavailable outside a repository, so fall back to
    # the checkout's own absolute path — which is what the offset is derived from anyway.
    WORKTREE_PATH="$(git rev-parse --show-toplevel 2>/dev/null || pwd -P)"
    HASH="$(hex_hash "$WORKTREE_PATH")"
    OFFSET=$(( (HASH % 200 + 1) * 100 ))
    SHORT_HASH="$(hex_hash "${WORKTREE_PATH}:name")"
    echo "not the primary checkout (${WORKTREE_PATH}); isolating compose project (offset +${OFFSET})"
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

# Web search (enterpriseaiframework-0be).
#
# WEBFETCH_TOKEN is a real credential, not a formality: it is what LibreChat presents to
# the fetch service as `firecrawlApiKey`, and the fetch service fails closed without it.
# It must also be NON-EMPTY for a reason that has nothing to do with authentication —
# LibreChat's Firecrawl client returns success:false without issuing any HTTP request when
# its key is empty, so an empty token turns web search into "cite pages nobody fetched".
ensure WEBFETCH_TOKEN              "wf-$(hex 24)"
# RERANK_TOKEN gates rerank/, the reranker of ours that closes the grounding gap
# `rerankerType: none` leaves open (enterpriseaiframework-0be). Same non-empty
# requirement as WEBFETCH_TOKEN, same reason: an empty value here would silently
# reproduce LibreChat's own no-ranking fallback instead of failing loudly.
ensure RERANK_TOKEN                "rr-$(hex 24)"
# SearXNG refuses to start on its upstream placeholder secret.
ensure SEARXNG_SECRET              "$(hex 24)"

# Conversation search (enterpriseaiframework-2a6). This is LibreChat's own native
# search feature (packages/data-schemas/src/models/plugins/mongoMeili.ts) — MEILI_HOST
# + MEILI_MASTER_KEY + SEARCH=true turn it on. MEILI_MASTER_KEY authenticates
# LibreChat's Meilisearch client against the self-hosted meilisearch service below;
# it is never sent anywhere outside the bundle.
ensure MEILI_MASTER_KEY            "ms-$(hex 24)"
# File search (enterpriseaiframework-c7c) — rag_api's dedicated pgvector instance.
ensure RAGVECTOR_PASSWORD          "$(hex 24)"

# Bootstrap realm user, so a fresh bundle can be signed in to without manual steps.
# The first two are not secrets, but they must exist in .env for post-up to provision
# the account — an .env written before these were added would otherwise skip it silently.
ensure BOOTSTRAP_USER              "baron"
ensure BOOTSTRAP_EMAIL             "baron@3dl.dev"
ensure BOOTSTRAP_PASSWORD          "$(hex 16)"

# --- codeapi (enterpriseaiframework-082) ---
# The code-execution sandbox's own Valkey, never the bundle's shared one — the shared
# instance has no password and every codeapi component requires REDIS_PASSWORD.
ensure CODEAPI_REDIS_PASSWORD        "$(hex 24)"
# Shared secret between codeapi's own components (api/worker/file-server/tool-call-
# server/egress-gateway) — internal service-to-service auth, never seen by LibreChat
# or by the sandbox container itself (secure-startup.ts's hardened-mode boot check
# refuses to start a sandbox that can see anything matching SECRET|TOKEN|PASSWORD).
ensure CODEAPI_INTERNAL_SERVICE_TOKEN "$(hex 32)"
# Egress gateway's own handle-encryption secret. Sandbox-runner, service-api and
# service-worker never receive this one either — only the egress gateway does.
ensure CODEAPI_EGRESS_GRANT_SECRET   "$(hex 32)"
ensure MINIO_ROOT_USER                "codeapi"
ensure MINIO_ROOT_PASSWORD            "$(hex 24)"

# The two Ed25519 keypairs codeapi needs — the LibreChat-auth JWT pair and the
# execution-manifest pair — are generated together, atomically, by a python helper,
# never by ensure() above: ensure() fills ONE variable from ONE generator command,
# but a keypair is two variables that must come from the SAME key or nothing verifies.
python3 "$SCRIPT_DIR/render-codeapi-keys.py"

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

# SearXNG's secret has to be inside the settings file before the container starts: this
# SearXNG has no SEARXNG_SECRET environment override, and the image only substitutes its
# own placeholder when it is CREATING settings.yml — a mounted file is read verbatim.
#
# Unlike the realm export this is re-rendered every time rather than left alone once it
# exists. The realm can only be imported once (Keycloak ignores the file afterwards), so
# rewriting it would desynchronise the file from the database; SearXNG re-reads its
# settings on every start, so re-rendering is how a change to the template — a new engine,
# a changed timeout — actually reaches the running instance instead of being silently
# ignored on every subsequent `make up`.
echo "rendering searxng/settings.yml"
sed -e "s|SEARXNG_SECRET_REPLACED_AT_BUNDLE_UP|${SEARXNG_SECRET}|" \
    searxng/settings.template.yml > searxng/settings.yml
if grep -q 'SEARXNG_SECRET_REPLACED_AT_BUNDLE_UP' searxng/settings.yml; then
    echo "error: searxng settings template still has an unreplaced placeholder" >&2
    exit 1
fi
chmod 644 searxng/settings.yml

echo "ok"
