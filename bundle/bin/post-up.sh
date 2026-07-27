#!/usr/bin/env bash
# First-run reconcile, then print where everything is.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

if [[ -n "${BOOTSTRAP_USER:-}" ]]; then
    echo
    echo "ensuring bootstrap user ${BOOTSTRAP_USER}"
    ./bin/ensure-user.sh "${BOOTSTRAP_USER}" "${BOOTSTRAP_PASSWORD}" \
        "${BOOTSTRAP_EMAIL:-${BOOTSTRAP_USER}@example.invalid}" >/dev/null
fi

echo
echo "reconciling identity into virtual keys"
./bin/api.sh POST /admin/sync >/dev/null || {
    echo "sync failed — run 'make sync' once identity finishes its first-boot import" >&2
}

cat <<EOF

  Chat surface        http://localhost:${CHAT_PORT:-3080}
  Gateway             http://localhost:${GATEWAY_PORT:-4000}
  Control plane       http://localhost:${CONTROL_PLANE_PORT:-8081}/docs
  Identity admin      http://localhost:${IDP_PORT:-8082}  (user: ${IDP_ADMIN_USER:-admin})

  Sign in to chat as   ${BOOTSTRAP_USER:-<none>} / ${BOOTSTRAP_PASSWORD:-<unset>}

  make sync    reconcile identity -> virtual keys
  make spend   the one bill, by user and surface
  make audit   verify the audit hash chain
  make test    prove scope items 1-9

EOF
