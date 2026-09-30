# The operated-instance source, shared by deploy.sh and watch-and-deploy.sh (enterpriseaiframework-e7f).
#
# Two files, layered, and NOTHING else (never the caller's environment):
#   1. bundle/.env          shared secrets; ALSO read by the local compose stack
#   2. the OVERLAY          operated-instance values (PUBLIC_BASE_URL, GATEWAY_PROVIDER, ...);
#                           read ONLY by deploy/; overlay wins over bundle/.env
#
# The overlay lives at ~/.config/enterprise-ai/operated.env (override: DEPLOY_OVERLAY_FILE). It is
# outside the repo on purpose: compose's --env-file, every bundle/bin script and every
# `git clean`/worktree/checkout the watcher does stop at the repo, so the local test stack can never
# pick the production values up (they once flipped the local control plane to freerouter).
# tests/test_deploy_instance_safety.py pins that nothing under bundle/, Makefile or tests reads it.
#
# Sourced, not executed. Sets INSTANCE_ENV_FILE and INSTANCE_OVERLAY_FILE.

instance_source_paths() {
    INSTANCE_ENV_FILE="${DEPLOY_ENV_FILE:-bundle/.env}"
    case "$INSTANCE_ENV_FILE" in /*) ;; *) INSTANCE_ENV_FILE="./$INSTANCE_ENV_FILE" ;; esac
    INSTANCE_OVERLAY_FILE="${DEPLOY_OVERLAY_FILE:-${HOME:-/root}/.config/enterprise-ai/operated.env}"
    case "$INSTANCE_OVERLAY_FILE" in /*) ;; *) INSTANCE_OVERLAY_FILE="./$INSTANCE_OVERLAY_FILE" ;; esac
}

# Fail-closed check on the overlay. Prints the reason to stderr, returns 1 on any defect.
# The OVERLAY ALONE must declare both keys (keys unset first): values in bundle/.env do not count.
instance_overlay_check() {
    local f="$INSTANCE_OVERLAY_FILE" k v
    [[ -f "$f" ]] || { echo "operated-instance overlay $f is missing; refusing to deploy an instance I cannot identify (see deploy/README.md)" >&2; return 1; }
    for k in PUBLIC_BASE_URL GATEWAY_PROVIDER; do
        v=$( ( set +u; unset PUBLIC_BASE_URL GATEWAY_PROVIDER; set -a; . "$f" >/dev/null 2>&1; printf '%s' "${!k:-}" ) )
        [[ -n "$v" ]] || { echo "$k is not set in the operated-instance overlay $f; there is no default (a placeholder overwrites the live instance)" >&2; return 1; }
    done
}
