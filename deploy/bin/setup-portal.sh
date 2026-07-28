#!/usr/bin/env bash
# Register the portal's Keycloak client and put its secrets where the pod can read them.
#
#   deploy/bin/setup-portal.sh
#
# Idempotent: run it again after changing PUBLIC_BASE_URL and it re-registers the callback
# without disturbing anything else. Secrets that already exist are reused rather than
# regenerated, because rotating the cookie secret signs everybody out for no reason.
set -euo pipefail

cd "$(dirname "$0")/../.."

NS=enterprise-ai
REALM="${IDP_REALM:-enterprise-ai}"
CLIENT_ID=portal

secret() { kubectl -n "$NS" get secret enterprise-ai-secrets -o jsonpath="{.data.$1}" | base64 -d; }
have()   { kubectl -n "$NS" get secret enterprise-ai-secrets -o jsonpath="{.data.$1}" 2>/dev/null | grep -q .; }

PUBLIC_BASE_URL="$(secret PUBLIC_BASE_URL)"
[[ -n "$PUBLIC_BASE_URL" ]] || { echo "PUBLIC_BASE_URL is not set in the namespace secret" >&2; exit 1; }

# The portal lives under /portal on the same origin as chat and identity. Same origin
# matters for the same reason it does everywhere else here: the OIDC issuer string must
# be byte-identical between the browser and the backchannel or the token is rejected.
REDIRECT="${PUBLIC_BASE_URL}/portal/oauth2/callback"

echo "==> portal client"
echo "    realm     ${REALM}"
echo "    callback  ${REDIRECT}"

kubectl -n "$NS" port-forward svc/identity 18080:8080 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
IDP=http://localhost:18080
for _ in $(seq 1 40); do curl -sS -o /dev/null -m 2 "${IDP}/realms/${REALM}" && break || sleep 1; done

token=$(curl -sS -X POST "${IDP}/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli \
    -d "username=$(secret IDP_ADMIN_USER)" -d "password=$(secret IDP_ADMIN_PASSWORD)" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
kc() { curl -sS -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" "$@"; }

CID=$(kc -G "${IDP}/admin/realms/${REALM}/clients" --data-urlencode "clientId=${CLIENT_ID}" \
      | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')

if [[ -z "$CID" ]]; then
    kc -X POST "${IDP}/admin/realms/${REALM}/clients" -d "$(
        REDIRECT="$REDIRECT" ORIGIN="$PUBLIC_BASE_URL" CLIENT_ID="$CLIENT_ID" python3 -c '
import json, os
print(json.dumps({
    "clientId": os.environ["CLIENT_ID"], "name": "Portal",
    "protocol": "openid-connect", "publicClient": False, "standardFlowEnabled": True,
    "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
    "redirectUris": [os.environ["REDIRECT"]], "webOrigins": [os.environ["ORIGIN"]],
}))')" >/dev/null
    CID=$(kc -G "${IDP}/admin/realms/${REALM}/clients" --data-urlencode "clientId=${CLIENT_ID}" \
          | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])')
    echo "    created"
else
    # Merge rather than replace: a deployment may legitimately answer on more than one
    # origin (the funnel and the LAN), and clobbering the list breaks the other one.
    kc "${IDP}/admin/realms/${REALM}/clients/${CID}" \
      | REDIRECT="$REDIRECT" ORIGIN="$PUBLIC_BASE_URL" python3 -c '
import json, os, sys
c = json.load(sys.stdin)
print(json.dumps({
    "redirectUris": sorted(set(c.get("redirectUris") or []) | {os.environ["REDIRECT"]}),
    "webOrigins":   sorted(set(c.get("webOrigins")   or []) | {os.environ["ORIGIN"]}),
}))' > /tmp/portal-client-$$.json
    kc -X PUT "${IDP}/admin/realms/${REALM}/clients/${CID}" -d "@/tmp/portal-client-$$.json" >/dev/null
    rm -f "/tmp/portal-client-$$.json"
    echo "    updated"
fi

CLIENT_SECRET=$(kc "${IDP}/admin/realms/${REALM}/clients/${CID}/client-secret" \
                | python3 -c 'import sys,json; print(json.load(sys.stdin)["value"])')
[[ -n "$CLIENT_SECRET" ]] || { echo "could not read the ${CLIENT_ID} client secret" >&2; exit 1; }

# Reused if present. Regenerating it would invalidate every live portal session, which is
# a surprising thing for a config script to do.
if have PORTAL_COOKIE_SECRET; then
    COOKIE_SECRET="$(secret PORTAL_COOKIE_SECRET)"
else
    COOKIE_SECRET=$(python3 -c '
import base64, secrets
print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')
fi

kubectl -n "$NS" patch secret enterprise-ai-secrets --type merge -p "$(
    PORTAL_CLIENT_SECRET="$CLIENT_SECRET" PORTAL_COOKIE_SECRET="$COOKIE_SECRET" \
    PORTAL_REDIRECT_URL="$REDIRECT" python3 -c '
import base64, json, os
b = lambda s: base64.b64encode(s.encode()).decode()
print(json.dumps({"data": {
    "PORTAL_CLIENT_SECRET": b(os.environ["PORTAL_CLIENT_SECRET"]),
    "PORTAL_COOKIE_SECRET": b(os.environ["PORTAL_COOKIE_SECRET"]),
    "PORTAL_REDIRECT_URL":  b(os.environ["PORTAL_REDIRECT_URL"]),
}}))')" >/dev/null

echo "    secrets written"
echo
echo "  portal: ${PUBLIC_BASE_URL}/portal/"
