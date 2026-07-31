#!/usr/bin/env bash
# Deploy the bundle to k3s.
#
# Reuses bundle/.env as the source of secrets so the cluster and the local compose bundle
# cannot drift into different credentials. Nothing secret is written to the repo — Secrets
# are created directly against the API.
#
#   PUBLIC_BASE_URL=https://ai.3dl.network deploy/bin/deploy.sh
#
# PUBLIC_BASE_URL must be the URL a *browser* will use. The chat surface's OIDC client
# refuses plaintext discovery and validates that the issuer matches what it requested, so
# an http:// value here will deploy cleanly and then fail at login. That is not a bug in
# this script — see dogfood-findings.md finding 5.
set -euo pipefail

cd "$(dirname "$0")/../.."

NS=enterprise-ai
REGISTRY="${RAIL_REGISTRY:-192.168.2.43:30500}"
IMAGE_NAME="enterprise-ai-control-plane"
TAG="$(git rev-parse --short HEAD 2>/dev/null || echo latest)"
IMAGE="${REGISTRY}/${IMAGE_NAME}:${TAG}"

[[ -f bundle/.env ]] || { echo "bundle/.env missing — run 'make up' locally first" >&2; exit 1; }
set -a; . ./bundle/.env; set +a

PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
if [[ -z "$PUBLIC_BASE_URL" ]]; then
    echo "error: PUBLIC_BASE_URL is required (e.g. https://ai.3dl.network)" >&2
    exit 1
fi
if [[ "$PUBLIC_BASE_URL" != https://* ]]; then
    echo "WARNING: PUBLIC_BASE_URL is not https. Everything will deploy and come up," >&2
    echo "         but nobody will be able to log in: the OIDC client refuses plaintext." >&2
fi

# Same rule the catalogue generator enforces: never ship a model we cannot authenticate
# to. Without this the gateway advertises every Forge model and returns 500 on each one,
# which surfaces at request time instead of deploy time.
if grep -q 'forge' bundle/litellm/config.generated.yaml 2>/dev/null && [[ -z "${FORGE_API_KEY:-}" ]]; then
    echo "error: the generated catalogue contains Forge models but FORGE_API_KEY is empty." >&2
    echo "       Deploying would advertise models that 500 on every request." >&2
    echo "       Run 'op signin' then 'direnv reload', or regenerate a fakes-only" >&2
    echo "       catalogue with: bundle/bin/render-gateway-config.py" >&2
    exit 1
fi

echo "==> namespace"
kubectl apply -f deploy/k8s/00-namespace.yaml

echo "==> secrets"
PGUSER="${POSTGRES_USER:-eai}"
kubectl -n "$NS" create secret generic enterprise-ai-secrets \
    --from-literal=POSTGRES_USER="$PGUSER" \
    --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    --from-literal=CONTROL_PLANE_DATABASE_URL="postgresql://${PGUSER}:${POSTGRES_PASSWORD}@postgres:5432/controlplane" \
    --from-literal=GATEWAY_DATABASE_URL="postgresql://${PGUSER}:${POSTGRES_PASSWORD}@postgres:5432/gateway" \
    --from-literal=GATEWAY_MASTER_KEY="$GATEWAY_MASTER_KEY" \
    --from-literal=GATEWAY_SALT_KEY="$GATEWAY_SALT_KEY" \
    --from-literal=CONTROL_PLANE_ADMIN_TOKEN="$CONTROL_PLANE_ADMIN_TOKEN" \
    --from-literal=IDP_ADMIN_USER="${IDP_ADMIN_USER:-admin}" \
    --from-literal=IDP_ADMIN_PASSWORD="$IDP_ADMIN_PASSWORD" \
    --from-literal=IDP_CLIENT_SECRET="$IDP_CLIENT_SECRET" \
    --from-literal=CHAT_CLIENT_SECRET="$CHAT_CLIENT_SECRET" \
    --from-literal=CHAT_SESSION_SECRET="$CHAT_SESSION_SECRET" \
    --from-literal=CHAT_JWT_SECRET="$CHAT_JWT_SECRET" \
    --from-literal=CHAT_JWT_REFRESH_SECRET="$CHAT_JWT_REFRESH_SECRET" \
    --from-literal=CHAT_CREDS_KEY="$CHAT_CREDS_KEY" \
    --from-literal=CHAT_CREDS_IV="$CHAT_CREDS_IV" \
    --from-literal=CHAT_VIRTUAL_KEY="${CHAT_VIRTUAL_KEY:-}" \
    --from-literal=PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
    --from-literal=OPENID_ISSUER="${PUBLIC_BASE_URL}/realms/${IDP_REALM:-enterprise-ai}" \
    --from-literal=FORGE_API_KEY="${FORGE_API_KEY:-}" \
    --from-literal=CODEAPI_JWT_PRIVATE_KEY="$CODEAPI_JWT_PRIVATE_KEY" \
    --from-literal=CODEAPI_JWT_PUBLIC_KEY="$CODEAPI_JWT_PUBLIC_KEY" \
    --from-literal=CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY="$CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY" \
    --from-literal=SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY="$SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY" \
    --from-literal=CODEAPI_INTERNAL_SERVICE_TOKEN="$CODEAPI_INTERNAL_SERVICE_TOKEN" \
    --from-literal=CODEAPI_EGRESS_GRANT_SECRET="$CODEAPI_EGRESS_GRANT_SECRET" \
    --from-literal=CODEAPI_REDIS_PASSWORD="$CODEAPI_REDIS_PASSWORD" \
    --from-literal=MINIO_ROOT_USER="$MINIO_ROOT_USER" \
    --from-literal=MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
    --from-literal=WEBFETCH_TOKEN="${WEBFETCH_TOKEN:?WEBFETCH_TOKEN is unset — run bundle/bin/render-env.sh}" \
    --from-literal=RERANK_TOKEN="${RERANK_TOKEN:?RERANK_TOKEN is unset — run bundle/bin/render-env.sh}" \
    --from-literal=SEARXNG_SECRET="${SEARXNG_SECRET:?SEARXNG_SECRET is unset — run bundle/bin/render-env.sh}" \
    --from-literal=MEILI_MASTER_KEY="${MEILI_MASTER_KEY:?MEILI_MASTER_KEY is unset — run bundle/bin/render-env.sh}" \
    --from-literal=RAGVECTOR_USER="${RAGVECTOR_USER:-ragapi}" \
    --from-literal=RAGVECTOR_PASSWORD="${RAGVECTOR_PASSWORD:?RAGVECTOR_PASSWORD is unset — run bundle/bin/render-env.sh}" \
    --from-literal=RAG_VIRTUAL_KEY="${RAG_VIRTUAL_KEY:-}" \
    --dry-run=client -o yaml | kubectl apply -f -

# The realm JSON carries client secrets, hence a Secret. Rendered by the compose bundle;
# reused here so both deployments trust the same clients.
if [[ -f bundle/keycloak/realm-export.json ]]; then
    python3 - <<'PY' > /tmp/realm-public.json
import json, os, pathlib
realm = json.loads(pathlib.Path("bundle/keycloak/realm-export.json").read_text())
public = os.environ["PUBLIC_BASE_URL"].rstrip("/")
# Redirect URIs must point at the public origin; the compose bundle's localhost entries
# are kept so the same realm export works for local development too.
for c in realm.get("clients", []):
    if c.get("clientId") == "librechat":
        c["redirectUris"] = sorted(set(c.get("redirectUris", [])) | {
            f"{public}/oauth/openid/callback"})
        c["webOrigins"] = sorted(set(c.get("webOrigins", [])) | {public})
print(json.dumps(realm, indent=2))
PY
    kubectl -n "$NS" create secret generic enterprise-ai-realm \
        --from-file=realm.json=/tmp/realm-public.json \
        --dry-run=client -o yaml | kubectl apply -f -
    rm -f /tmp/realm-public.json
else
    echo "warning: bundle/keycloak/realm-export.json missing; run 'make up' locally first" >&2
fi

echo "==> config"
# The generated gateway catalogue: same artifact the compose bundle runs, so the cluster
# offers exactly the models that were priced and verified locally.
# The callback modules ride in the same configmap because config.yaml names them:
# litellm imports them at startup and the proxy will not boot without them.
kubectl -n "$NS" create configmap gateway-config \
    --from-file=config.yaml=bundle/litellm/config.generated.yaml \
    --from-file=strip_reasoning.py=deploy/gateway/strip_reasoning.py \
    --from-file=require_principal.py=deploy/gateway/require_principal.py \
    --from-file=flush_spend_on_shutdown.py=deploy/gateway/flush_spend_on_shutdown.py \
    --from-file=allow_reasoning_effort.py=deploy/gateway/allow_reasoning_effort.py \
    --dry-run=client -o yaml | kubectl apply -f -

# The chat surface's in-cluster endpoint differs from compose only in hostname.
sed 's|http://gateway:4000/v1|http://gateway:4000/v1|' bundle/librechat/librechat.yaml > /tmp/librechat-k8s.yaml
kubectl -n "$NS" create configmap chat-config \
    --from-file=librechat.yaml=/tmp/librechat-k8s.yaml \
    --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/librechat-k8s.yaml

# enterpriseaiframework-6ff: the tenant Agent Skills corpus, one ConfigMap per skill
# directory under bundle/skills/ — see deploy/bin/lib/tenant-skills.sh for why not one
# combined ConfigMap. Named `chat-skill-*` and shared verbatim by the workspace pods
# (deploy/k8s/61-workspace.template.yaml), so the chat surface and the terminal agent
# load the identical corpus through their own separate native loaders.
source deploy/bin/lib/tenant-skills.sh
ensure_tenant_skill_configmaps "$NS" chat-skill bundle/skills

CFG_SUM=$( { cat bundle/litellm/config.generated.yaml bundle/librechat/librechat.yaml deploy/gateway/strip_reasoning.py deploy/gateway/require_principal.py deploy/gateway/flush_spend_on_shutdown.py deploy/gateway/allow_reasoning_effort.py bundle/skills/*/SKILL.md; } | sha256sum | cut -c1-16)

echo "==> build and push control-plane image -> ${IMAGE}"
docker build -q -t "$IMAGE" ./control-plane
docker push -q "$IMAGE"

echo "==> apply"
for f in 10-postgres 11-data 20-identity 30-gateway 50-chat; do
    sed "s|REPLACED_BY_DEPLOY|${CFG_SUM}|" "deploy/k8s/${f}.yaml" | kubectl apply -f -
done
sed "s|image: REPLACED_BY_DEPLOY|image: ${IMAGE}|" deploy/k8s/40-control-plane.yaml | kubectl apply -f -

echo
echo "==> waiting for rollout"
for d in postgres chatdb; do kubectl -n "$NS" rollout status statefulset/"$d" --timeout=300s; done
for d in valkey identity gateway control-plane chat; do
    kubectl -n "$NS" rollout status deployment/"$d" --timeout=600s || true
done

echo
kubectl -n "$NS" get pods -o wide
cat <<EOF

  Chat (NodePort)      http://<k3s-worker>:30380     -> front with Caddy at ${PUBLIC_BASE_URL}
  Gateway (NodePort)   http://<k3s-worker>:30400     -> LAN/tailnet only, not public
  Control plane        kubectl -n ${NS} port-forward svc/control-plane 8081:8000

  Uninstall            kubectl delete namespace ${NS}
EOF
