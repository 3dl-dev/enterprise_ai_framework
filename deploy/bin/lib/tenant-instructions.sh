#!/usr/bin/env bash
# ensure_tenant_instructions <ns> <configmap-name> <default-seed-file> [<instructions-file>]
#
# Manages the one ConfigMap that carries an operator's house rules for the terminal
# agent, deployment-wide (enterpriseaiframework-cbf). Extracted from
# deploy/bin/provision-workspace.sh into its own file so tests-live/test_workspace_
# instructions.py can call the exact same function against a real API server, with a
# throwaway ConfigMap name, instead of re-implementing or simulating it — a prior version
# of that test called `kubectl create configmap --dry-run=client -o json` on its own,
# which exercises kubectl's client-side generator and zero lines of this logic.
#
# Three branches, same three as before extraction:
#
#   instructions given        an operator is deliberately setting (or resetting) the
#                              deployment's house rules. Overwrite unconditionally — this
#                              call is the whole point of the argument.
#   instructions omitted,
#   configmap already exists  do not touch it: a second provision-workspace.sh run for a
#                              user this deployment already configured must not silently
#                              reset an operator's customisation back to the default seed.
#   instructions omitted,
#   configmap missing         first run for this deployment. Seed from default_seed
#                              (deploy/workspace/AGENTS.md, unedited) so the tenant that
#                              already exists sees no regression.
#
# `--dry-run=client -o yaml | kubectl apply -f -` is the standard idiom for "create or
# replace this object, whichever applies" without a separate exists-check-then-create race
# for the overwrite case; it still sends a real request to the real API server on the
# `kubectl apply` half — the dry-run only scopes the local YAML generation on the `create`
# half. It is not a stand-in for exercising this code; a test that shells out to `kubectl
# create configmap --dry-run=client` ON ITS OWN, without calling this function, DOES NOT
# exercise this file.
ensure_tenant_instructions() {
    local ns="$1" cm="$2" default_seed="$3" instructions="${4:-}"

    if [[ -n "$instructions" ]]; then
        [[ -f "$instructions" ]] || { echo "no such file: ${instructions}" >&2; return 1; }
        kubectl -n "$ns" create configmap "$cm" \
            --from-file=TENANT.md="$instructions" \
            --dry-run=client -o yaml | kubectl apply -f - >/dev/null
        echo "    instructions ${instructions} (deployment-wide, updated)"
    elif ! kubectl -n "$ns" get configmap "$cm" >/dev/null 2>&1; then
        kubectl -n "$ns" create configmap "$cm" \
            --from-file=TENANT.md="$default_seed" \
            --dry-run=client -o yaml | kubectl apply -f - >/dev/null
        echo "    instructions ${default_seed} (deployment-wide, seeded default)"
    else
        echo "    instructions unchanged (deployment already configured)"
    fi
}
