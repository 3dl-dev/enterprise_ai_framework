#!/usr/bin/env bash
# Generate a self-signed TLS certificate for the identity provider.
#
# Required, not cosmetic: the chat surface's OIDC client (openid-client v6) refuses
# discovery over plaintext HTTP and exposes no override, so identity must speak TLS.
# That is the correct posture regardless — tokens should not cross a wire in the clear.
#
# The certificate carries both names it is reached by, because the browser and the chat
# container do not use the same one:
#   localhost  — the browser, for the authorization redirect
#   identity   — the chat container, for backchannel token exchange
#
# Idempotent: an existing certificate is left alone.
set -euo pipefail

cd "$(dirname "$0")/.."

CERT_DIR=certs
CRT="${CERT_DIR}/identity.crt"
KEY="${CERT_DIR}/identity.key"

# The OIDC client validates that the issuer in the discovery document equals the issuer
# it asked for. That means the browser and the chat container must reach identity at the
# *same* URL — "localhost for the browser, identity for the container" fails validation
# no matter how the discovery document is configured.
#
# The host's own routable IP is the one address that satisfies both: the browser reaches
# it directly, and containers reach it because published ports bind on the host.
host_ip() {
    ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}'
}

IDP_PUBLIC_IP="$(host_ip)"
if [[ -z "$IDP_PUBLIC_IP" ]]; then
    echo "error: could not determine a host IP reachable from containers" >&2
    exit 1
fi

# Record it so compose and the tests use the same address the certificate covers.
# A missing .env used to fall through all three branches in silence, leaving
# IDP_PUBLIC_HOST unset for compose. Nothing downstream noticed until Keycloak died on
# "https://:8443", ten containers later. It is render-env.sh's job to create .env and
# `make up` now runs it first, so reaching here without one is a broken invocation and
# says so rather than producing a bundle that cannot start.
if [[ ! -f .env ]]; then
    echo "error: bundle/.env does not exist — run bundle/bin/render-env.sh first" >&2
    exit 1
fi
if grep -qE '^IDP_PUBLIC_HOST=' .env; then
    sed "s|^IDP_PUBLIC_HOST=.*|IDP_PUBLIC_HOST=${IDP_PUBLIC_IP}|" .env > .env.tmp && mv .env.tmp .env
else
    printf 'IDP_PUBLIC_HOST=%s\n' "$IDP_PUBLIC_IP" >> .env
fi

if [[ -f "$CRT" && -f "$KEY" ]]; then
    if openssl x509 -in "$CRT" -noout -text | grep -q "IP Address:${IDP_PUBLIC_IP}"; then
        echo "identity certificate exists and covers ${IDP_PUBLIC_IP}, leaving it alone"
        exit 0
    fi
    echo "host IP changed to ${IDP_PUBLIC_IP}; regenerating certificate"
fi

mkdir -p "$CERT_DIR"
echo "generating self-signed certificate for identity (${IDP_PUBLIC_IP})"

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$KEY" -out "$CRT" -days 825 -sha256 \
    -subj "/CN=identity" \
    -addext "subjectAltName=DNS:localhost,DNS:identity,IP:127.0.0.1,IP:${IDP_PUBLIC_IP}" \
    -addext "basicConstraints=critical,CA:TRUE" \
    2>/dev/null

# Keycloak runs as a non-root user and must be able to read the key.
chmod 644 "$CRT"
chmod 644 "$KEY"

echo "wrote ${CRT}"
