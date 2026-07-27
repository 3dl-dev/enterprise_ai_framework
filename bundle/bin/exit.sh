#!/usr/bin/env bash
# The exit path (scope item 9).
#
# Leaving must be a procedure the operator can run, not a support ticket. This is the
# anti-lock-in mechanism and it answers the question a licence cannot: what happens when
# you want us gone.
#
#   exit.sh export            export the ledger and verify it (safe, non-destructive)
#   exit.sh direct            write the direct-provider configuration for each surface
#   exit.sh revoke --confirm  revoke every virtual key at the gateway (destructive)
#   exit.sh full --confirm    all three, in the order that keeps surfaces working
#
# Order matters. Export first, because after revocation the surfaces stop working and
# panic is a bad time to discover the archive was incomplete. Revoke last, because the
# direct configuration must be in the operator's hands before their keys stop working.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

CP="http://localhost:${CONTROL_PLANE_PORT:-8081}"
GATEWAY="http://localhost:${GATEWAY_PORT:-4000}"
AUTH=(-H "Authorization: Bearer ${CONTROL_PLANE_ADMIN_TOKEN}")

MODE="${1:-export}"
CONFIRM="${2:-}"

# A fixed name would silently overwrite the previous archive; a timestamp keeps every one.
EXPORT_ROOT="${EXPORT_DIR:-exports}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${EXPORT_ROOT}/${STAMP}"

do_export() {
    mkdir -p "$OUT"
    echo "exporting to ${OUT}"

    curl -sS -f "${AUTH[@]}" "${CP}/admin/export/manifest" > "${OUT}/manifest.json"
    curl -sS -f "${AUTH[@]}" "${CP}/admin/export/audit"    > "${OUT}/audit.jsonl"
    curl -sS -f "${AUTH[@]}" "${CP}/admin/export/spend"    > "${OUT}/spend.csv"
    curl -sS -f "${AUTH[@]}" "${CP}/admin/export/keys"     > "${OUT}/keys.csv"

    echo
    ./bin/verify-export.py "$OUT"

    # Point at the newest export without needing to know the timestamp.
    ln -sfn "$STAMP" "${EXPORT_ROOT}/latest"
}

do_direct() {
    local dir="${OUT}/direct"
    [[ -d "$OUT" ]] || dir="${EXPORT_ROOT}/latest/direct"
    mkdir -p "$dir"
    echo
    echo "writing direct-provider configuration to ${dir}"

    # The upstreams the gateway was actually pointed at — not a guess, and not a template
    # the operator has to reconstruct from memory after the layer is gone.
    curl -sS -f -G "${GATEWAY}/model/info" \
        -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" \
        | python3 -c '
import json, sys
d = json.load(sys.stdin)
rows = []
for m in d.get("data", []):
    lp = m.get("litellm_params", {}) or {}
    rows.append({
        "model": m.get("model_name"),
        "upstream_model": lp.get("model"),
        "api_base": lp.get("api_base"),
    })
json.dump(rows, sys.stdout, indent=2)
print()
' > "${dir}/upstreams.json"

    cat > "${dir}/README.md" <<'MD'
# Direct-provider configuration

The layer is being removed. Each surface below talks straight to the provider afterwards.

`upstreams.json` lists every model the gateway was routing, with the upstream model name
and base URL it was routed to. Those are the values to restore, using your own provider
API keys — the ones the virtual keys were minted against. The layer never held anything
else, so there is nothing else to retrieve.

## Chat surface

In `bundle/.env`, replace the virtual key with a provider key and point the endpoint at
the provider:

    CHAT_VIRTUAL_KEY=<your provider API key>

and in `bundle/librechat/librechat.yaml`, set `baseURL` to the provider's URL from
`upstreams.json` instead of `http://gateway:4000/v1`.

## IDE coding agent

Set the OpenAI-compatible base URL to the provider's URL and supply your provider key.
Nothing else about the tool changes — only the pipe.

## Terminal coding agent

Unset `ANTHROPIC_BASE_URL` (or point it at the provider directly) and set
`ANTHROPIC_API_KEY` to your own key.

## What you keep

- `spend.csv`  — every metered request
- `audit.jsonl` — the full audit trail, hash-chained and independently verifiable
- `keys.csv`   — which virtual keys existed, for which user and surface

Verify the archive at any time, with this platform deleted:

    ./verify-export.py <this directory's parent>

## Not covered here

Egress restrictions, if you configured any, are outside this bundle's scope and must be
removed separately. Until they are, surfaces pointed directly at providers will be
blocked by your network rather than by this software.
MD
    echo "wrote ${dir}/README.md and ${dir}/upstreams.json"
}

do_revoke() {
    if [[ "$CONFIRM" != "--confirm" ]]; then
        echo "refusing to revoke without --confirm" >&2
        echo "every virtual key will stop working immediately; surfaces break until they" >&2
        echo "hold direct provider keys. Run: exit.sh revoke --confirm" >&2
        exit 2
    fi
    echo
    echo "revoking every virtual key"
    curl -sS -f -X POST "${AUTH[@]}" "${CP}/admin/exit/revoke-all" \
        | python3 -m json.tool
}

case "$MODE" in
    export) do_export ;;
    direct) do_direct ;;
    revoke) do_revoke ;;
    full)
        if [[ "$CONFIRM" != "--confirm" ]]; then
            echo "refusing to run the full exit without --confirm" >&2
            exit 2
        fi
        do_export
        do_direct
        do_revoke
        echo
        echo "exit complete. The archive in ${OUT} stands on its own."
        ;;
    *)
        echo "usage: exit.sh {export|direct|revoke --confirm|full --confirm}" >&2
        exit 2
        ;;
esac
