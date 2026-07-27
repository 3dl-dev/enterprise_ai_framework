#!/usr/bin/env bash
# Thin wrapper for calling the control-plane admin API from the host.
# Usage: api.sh GET /admin/spend  |  api.sh POST /admin/sync '{"...":...}'
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

method="${1:?usage: api.sh METHOD PATH [json]}"
path="${2:?usage: api.sh METHOD PATH [json]}"
body="${3:-}"

args=(-sS -X "$method"
      -H "Authorization: Bearer ${CONTROL_PLANE_ADMIN_TOKEN}"
      "http://localhost:${CONTROL_PLANE_PORT:-8081}${path}")

if [[ -n "$body" ]]; then
    args+=(-H "Content-Type: application/json" -d "$body")
fi

curl "${args[@]}" | python3 -m json.tool 2>/dev/null || true
