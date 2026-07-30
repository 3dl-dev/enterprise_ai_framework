#!/usr/bin/env bash
# sync_workspace_memory <ns> <configmap-name> <username> [<control-plane-deployment>]
#
# Refreshes one user's terminal-agent memory ConfigMap from LibreChat's chat memory
# (enterpriseaiframework-471, done-condition 3), the per-user counterpart to
# deploy/bin/lib/tenant-instructions.sh's deployment-wide TENANT.md.
#
# WHY THIS SHELLS OUT TO THE RUNNING CONTROL-PLANE POD INSTEAD OF READING MONGO DIRECTLY
# FROM WHEREVER THIS SCRIPT RUNS:
#
# The control-plane pod is the only thing in this deployment that already holds a network
# route to LibreChat's Mongo and the CHAT_MONGO_URL/CHAT_MONGO_DB credentials
# (deploy/k8s/40-control-plane.yaml) — the workspace pod's own NetworkPolicy deliberately
# does NOT include Mongo (deploy/k8s/60-workspace-common.yaml only allows DNS, the
# gateway, and the named MCP tool servers), and this script does not run inside any pod
# at all, it runs on an operator's machine with kubectl access. Rather than open a new
# route or invent a second Mongo credential, this reuses the one that already exists via
# `kubectl exec` into the already-running control-plane Deployment, the same "one control
# plane" principle chat_identity.py states for the spend ledger's own read of Mongo.
#
# ONE CONFIGMAP PER USER, not deployment-wide like tenant-instructions: a preference is
# personal, and the workspace template mounts `workspace-memory-<user>` (not a shared
# name) at /etc/opencode/memory for exactly that user's pod, `optional: true` so a pod
# started before this has ever run for that user still comes up (empty directory, and
# chat_memory.render_instructions_markdown's own "nothing stored yet" default covers the
# case where the ConfigMap exists but is stale/empty).
#
# `--dry-run=client -o yaml | kubectl apply -f -` is the same create-or-replace idiom
# tenant-instructions.sh uses, for the same reason: no separate exists-then-create race,
# while still sending a real request to the real API server on the apply half.
#
# THIS SCRIPT IS CLUSTER-SIDE ONLY. It is exercised here by static review and by the
# hermetic proof of the piece it calls (control-plane/app/chat_memory.py, tested against
# a real disposable Mongo in tests/test_workspace_memory_bridge.py) — the kubectl round
# trip itself is not applied or exercised against a live cluster by this change, per the
# operational constraint against touching the running deployment.
sync_workspace_memory() {
    local ns="$1" cm="$2" user="$3" cp_deploy="${4:-deploy/control-plane}"

    local content
    if ! content="$(kubectl -n "$ns" exec "$cp_deploy" -- \
        python3 -m app.render_workspace_memory "$user" 2>&1)"; then
        echo "could not render memory for ${user}: ${content}" >&2
        return 1
    fi

    kubectl -n "$ns" create configmap "$cm" \
        --from-literal="MEMORY.md=${content}" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    echo "    memory ${cm} refreshed for ${user}"
}
