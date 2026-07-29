#!/usr/bin/env bash
# assert_catalogue_not_downgraded <ns> <configmap-name> <local-config-path>
#
# enterpriseaiframework-dc0: bundle/litellm/config.generated.yaml is gitignored local
# state, rendered by bundle/bin/render-gateway-config.py, and deploy.sh ships that same
# file straight into the cluster's gateway-config ConfigMap. Whether the renderer emits
# real, priced Forge models or the three hardcoded fakes depends only on whether
# FORGE_API_KEY was visible in the ambient environment at render time — nothing about
# `make up` or `deploy.sh` signals which one just happened.
#
# ONE artifact, TWO deployments with incompatible requirements: compose must be
# fakes-only to be testable with no spend; the cluster is the dogfood and must be
# Forge-backed. A deploy from a shell that happens not to have Forge creds loaded (a
# fresh shell, a CI runner, direnv not yet allowed) silently pushes a fakes-only
# catalogue over a cluster that was Forge-backed, and every real user on it loses every
# real model with no error at deploy time — it just fails at chat time as an odd 400 on
# whatever they were using.
#
# This function reads the CURRENT cluster ConfigMap (a plain `kubectl get`, no mutation)
# and compares it against the LOCAL file about to be pushed. It refuses the deploy when
# the cluster is presently Forge-backed and the local file is not — a downgrade — because
# that direction is the one that fails silently. The opposite direction (fakes -> real,
# or real -> a newer real render) is not a downgrade and is always allowed.
#
# Detection is the same textual signal render-gateway-config.py and deploy.sh's sibling
# check already use: the literal substring `forge`, lowercase. Real entries embed it via
# `api_base: https://forge.3dl.dev/v1` (FORGE_BASE_URL's default) and nowhere else in
# either file does lowercase `forge` appear — config.base.yaml's own prose capitalizes it
# ("Forge's live catalog"). Verified: `grep -n 'forge' bundle/litellm/config.base.yaml`
# returns nothing, so the signal does not fire on template comments alone.
#
# Escape hatch: DEPLOY_CONFIRM_CATALOGUE_DOWNGRADE=1 bypasses the check, for the rare
# case an operator genuinely wants to revert a live cluster to the no-spend demo
# catalogue on purpose.
#
# Fails CLOSED, not open: if the cluster's current ConfigMap cannot be read for any
# reason OTHER than "it does not exist yet" (network blip, RBAC, wrong context), this
# refuses rather than guessing — a guard that fails open on transient errors is not a
# guard, it is a guard-shaped comment. "Configmap does not exist" is the one error that
# means "first deploy to this namespace, nothing to protect" and is allowed through.
assert_catalogue_not_downgraded() {
    local ns="$1" cm="$2" local_cfg="$3"

    if [[ "${DEPLOY_CONFIRM_CATALOGUE_DOWNGRADE:-}" == "1" ]]; then
        echo "warning: DEPLOY_CONFIRM_CATALOGUE_DOWNGRADE=1 — catalogue downgrade guard skipped." >&2
        return 0
    fi

    # NOTE: the kubectl call is the condition of this `if`, not a bare assignment. Under
    # `set -e` (which deploy.sh runs with), `cluster_cfg="$(kubectl ...)"` as a plain
    # statement aborts the whole script the instant kubectl exits non-zero — before this
    # function ever gets to look at $rc or print anything. That silently killed the
    # "first deploy, no ConfigMap yet" case (which must be ALLOWED) with no message at
    # all, instead of running the handling below. Wrapping it as an `if` condition is
    # exempt from `set -e` by POSIX rule, which is the whole reason for the shape here.
    local stderr_file cluster_cfg rc err
    stderr_file="$(mktemp)"
    if cluster_cfg="$(kubectl -n "$ns" get configmap "$cm" -o "jsonpath={.data.config\.yaml}" 2>"$stderr_file")"; then
        rc=0
    else
        rc=$?
    fi
    err="$(cat "$stderr_file")"
    rm -f "$stderr_file"

    if [[ $rc -ne 0 ]]; then
        if grep -qi 'not found\|notfound' <<<"$err"; then
            # First deploy to this namespace/configmap — nothing on the cluster yet to
            # protect against downgrading.
            return 0
        fi
        echo "error: could not read the cluster's current '${cm}' ConfigMap to check for a" >&2
        echo "       catalogue downgrade: ${err}" >&2
        echo "       Refusing rather than deploying blind. Fix cluster access, or set" >&2
        echo "       DEPLOY_CONFIRM_CATALOGUE_DOWNGRADE=1 to deploy anyway." >&2
        return 1
    fi

    local cluster_has_forge=false local_has_forge=false
    if grep -q 'forge' <<<"$cluster_cfg"; then
        cluster_has_forge=true
    fi
    if [[ -f "$local_cfg" ]] && grep -q 'forge' "$local_cfg"; then
        local_has_forge=true
    fi

    if [[ "$cluster_has_forge" == true && "$local_has_forge" == false ]]; then
        echo "error: refusing to deploy — the cluster's '${cm}' ConfigMap currently serves" >&2
        echo "       real, priced Forge models, but ${local_cfg}" >&2
        echo "       is fakes-only (no FORGE_API_KEY was visible when it was last rendered)." >&2
        echo "       Deploying would silently downgrade every user on this cluster to three" >&2
        echo "       fake models with no error at deploy time." >&2
        echo "       Regenerate with Forge credentials visible, e.g.:" >&2
        echo "           direnv exec . bundle/bin/render-gateway-config.py" >&2
        echo "       Or, if this downgrade is genuinely intended, re-run deploy.sh with" >&2
        echo "       DEPLOY_CONFIRM_CATALOGUE_DOWNGRADE=1 set in the environment." >&2
        return 1
    fi

    return 0
}
