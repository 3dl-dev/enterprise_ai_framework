#!/usr/bin/env bash
# Deploy the bundle to k3s.
#
# Reuses bundle/.env as the source of secrets so the cluster and the local compose bundle
# cannot drift into different credentials. Nothing secret is written to the repo — Secrets
# are created directly against the API.
#
#   PUBLIC_BASE_URL=https://ai.example.org deploy/bin/deploy.sh
#
# PUBLIC_BASE_URL must be the URL a *browser* will use. The chat surface's OIDC client
# refuses plaintext discovery and validates that the issuer matches what it requested, so
# an http:// value here will deploy cleanly and then fail at login. That is not a bug in
# this script — see dogfood-findings.md finding 5.
set -euo pipefail

cd "$(dirname "$0")/../.."

NS=enterprise-ai
IMAGE_NAME="enterprise-ai-control-plane"
TAG="$(git rev-parse --short HEAD 2>/dev/null || echo latest)"

[[ -f bundle/.env ]] || { echo "bundle/.env missing — run 'make up' locally first" >&2; exit 1; }
set -a; . ./bundle/.env; set +a

# Instance profile — operator-agnostic DEFAULTS; the instance overrides these in bundle/.env
# (see docs/design/hoistable-and-operated.md). Defined AFTER sourcing bundle/.env so the
# instance's values win. The manifests carry __GATEWAY_LAN_IP__/__GATEWAY_TAILNET_HOST__
# placeholders (the OIDC-backchannel hostAlias), substituted in the apply loop below.
REGISTRY="${RAIL_REGISTRY:-localhost:5000}"
IMAGE="${REGISTRY}/${IMAGE_NAME}:${TAG}"
GATEWAY_LAN_IP="${GATEWAY_LAN_IP:-127.0.0.1}"
GATEWAY_TAILNET_HOST="${GATEWAY_TAILNET_HOST:-gateway.local}"
LAN_CIDR="${LAN_CIDR:-127.0.0.0/8}"

PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
if [[ -z "$PUBLIC_BASE_URL" ]]; then
    echo "error: PUBLIC_BASE_URL is required (e.g. https://ai.example.org)" >&2
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
    --from-literal=FREEROUTER_METER_DSN="postgresql://${PGUSER}:${POSTGRES_PASSWORD}@postgres:5432/freerouter" \
    --from-literal=GATEWAY_PROVIDER="${GATEWAY_PROVIDER:-}" \
    --from-literal=FREEROUTER_MASTER_KEY="${FREEROUTER_MASTER_KEY:-}" \
    --from-literal=FREEROUTER_OPERATOR_TAB_MICRO="${FREEROUTER_OPERATOR_TAB_MICRO:-}" \
    --from-literal=FREEROUTER_PEER_SERVE="${FREEROUTER_PEER_SERVE:-}" \
    --from-literal=FREEROUTER_PEER_TESTNET="${FREEROUTER_PEER_TESTNET:-}" \
    --from-literal=FREEROUTER_PEER_SETTLEMENT_CHAIN_ID="${FREEROUTER_PEER_SETTLEMENT_CHAIN_ID:-}" \
    --from-literal=FREEROUTER_PEER_ALLOWLIST="${FREEROUTER_PEER_ALLOWLIST:-}" \
    --from-literal=FREEROUTER_PEER_UPSTREAMS="${FREEROUTER_PEER_UPSTREAMS:-}" \
    --from-literal=FREEROUTER_PEER_RELAY_URL="${FREEROUTER_PEER_RELAY_URL:-}" \
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

# The chat surface's inference profile is an INSTANCE choice, not a bundled default. The
# bundle ships operator-agnostic (a shared key against the LiteLLM gateway); an instance
# overrides these two via bundle/.env to select a different backend / key model — e.g. a
# key-only gateway with per-user keys. Defaults reproduce the shipped bundle verbatim, so a
# forker who sets nothing gets a working chat surface. See librechat.yaml's endpoint note and
# docs/design/hoistable-and-operated.md.
CHAT_INFERENCE_BASE="${CHAT_INFERENCE_BASE:-http://gateway:4000/v1}"
CHAT_ENDPOINT_APIKEY="${CHAT_ENDPOINT_APIKEY:-\${CHAT_VIRTUAL_KEY}}"
sed -e "s|baseURL: \"http://gateway:4000/v1\"|baseURL: \"${CHAT_INFERENCE_BASE}\"|" \
    -e "s|apiKey: \"\${CHAT_VIRTUAL_KEY}\"|apiKey: \"${CHAT_ENDPOINT_APIKEY}\"|" \
    bundle/librechat/librechat.yaml > /tmp/librechat-k8s.yaml
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

# The two files the portal's Agents tab renders an agent from
# (enterpriseaiframework-627). They live under deploy/ and the control-plane image is built
# from control-plane/ alone, so they arrive as a ConfigMap — the same delivery mechanism
# provision-agent.sh already uses for the entrypoint, and for the same reason: the image
# must not carry a second copy of an object set that would drift from the template.
#
# Rebuilt from the repository on every deploy, so an edit to either file reaches the
# control plane the way an edit to a manifest does.
echo "==> agent assets -> configmap/agent-assets"
#
# EVERY file the agent pod mounts at /etc/agent, not the entrypoint alone. The control
# plane rebuilds the shared `agent-entrypoint` ConfigMap from these when a user creates an
# agent from the Agents tab, so anything missing here is a tool that disappears from every
# agent in the namespace the first time somebody presses Create. The list is the one
# `deploy/bin/provision-agent.sh` ships and `control-plane/app/agents.py` names;
# tests/test_agent_assets.py fails if the three disagree.
kubectl -n "$NS" create configmap agent-assets \
    --from-file=64-agent.template.yaml=deploy/k8s/64-agent.template.yaml \
    --from-file=65-agent-hermes.template.yaml=deploy/k8s/65-agent-hermes.template.yaml \
    --from-file=entrypoint.sh=deploy/agent/entrypoint.sh \
    --from-file=agent-email=deploy/agent/agent-email \
    --from-file=EMAIL.md=deploy/agent/EMAIL.md \
    --from-file=agent-slack=deploy/agent/agent-slack \
    --from-file=SLACK.md=deploy/agent/SLACK.md \
    --from-file=agent-discord=deploy/agent/agent-discord \
    --from-file=DISCORD.md=deploy/agent/DISCORD.md \
    --from-file=agentws.py=deploy/agent/agentws.py \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "==> build and push control-plane image -> ${IMAGE}"
docker build -q -t "$IMAGE" ./control-plane
docker push -q "$IMAGE"

echo "==> apply"
# Every manifest in deploy/k8s/, in the lexical order the NN- prefixes already encode.
#
# This used to be a hand-written list of five files, and it silently fell behind the
# directory: 51-file-search.yaml (rag-api) and 70-codeapi.yaml were merged, tested and
# closed, and then never deployed, because nobody thought to add them here. A feature that
# ships a manifest must not also have to ship an edit to this loop — so the loop reads the
# directory, and anything that must NOT be applied is excluded BY NAME below, with a reason.
# An exclusion is a decision; an omission was an accident.
#
# `image: REPLACED_BY_DEPLOY` is NOT one placeholder — it is the same spelling used by seven
# manifests for seven DIFFERENT images (mcp-echo, fakeprovider, the two web-search services,
# and six codeapi images), each built separately by deploy/bin/kaniko-build.sh. Only
# 40-control-plane.yaml's is the image this script builds.
#
# Substituting $IMAGE into all of them is not a hypothetical: it was done here on 2026-08-01
# and it put the control-plane image into fakeprovider, mcp-echo, rerank and webfetch, which
# then crash-looped on `KeyError: CONTROL_PLANE_DATABASE_URL` — three of them had been
# healthy for four days. So the substitution is scoped to the one manifest it belongs to, and
# any OTHER manifest still carrying an unresolved `image:` placeholder is a hard error: its
# image was never built and pushed, and applying it would replace a working workload with a
# wrong one. Refusing is the whole point — a deploy that cannot deploy something must say so
# rather than deploy something else.
needs_built_image() { grep -q '^\s*image: REPLACED_BY_DEPLOY' "$1"; }
skip_manifest() {
    case "$1" in
        # Applied above, before the Secrets that everything else depends on.
        00-namespace.yaml) return 0 ;;
        # Gated on the worker reboot window (enterpriseaiframework-feb). Applying these
        # before the virtiofs device is live binds PVCs to a path that is not there yet.
        01-tank-pvs.yaml) return 0 ;;
        # A template, not a manifest: carries per-user placeholders and is rendered one pod
        # at a time by deploy/bin/provision-workspace.sh.
        61-workspace.template.yaml) return 0 ;;
        # Same: a template, rendered one agent at a time by deploy/bin/provision-agent.sh.
        # Note that 63-agent-common.yaml is NOT skipped — the agents ServiceAccount and
        # NetworkPolicy are namespace-wide objects and must land on every deploy, so an
        # agent can never exist before the policy that fences it.
        64-agent.template.yaml) return 0 ;;
        # The hermes gateway agent template (agents-gateway-console.md), rendered one agent
        # at a time by control-plane/app/agents.py. 66-agent-console-common.yaml (the
        # namespace-wide :9119 policy that fences its dashboard) is NOT a template and IS
        # applied, for the same reason 63-agent-common.yaml is.
        65-agent-hermes.template.yaml) return 0 ;;
        *) return 1 ;;
    esac
}

unbuilt=()
for path in deploy/k8s/*.yaml; do
    f="$(basename "$path")"
    if skip_manifest "$f"; then
        echo "    skip $f"
        continue
    fi
    if [[ "$f" == 40-control-plane.yaml ]]; then
        # The one image this script builds and pushes, a few lines above.
        rendered=$(sed -e "s|image: REPLACED_BY_DEPLOY|image: ${IMAGE}|" \
                       -e "s|REPLACED_BY_DEPLOY|${CFG_SUM}|" "$path")
    elif needs_built_image "$path"; then
        unbuilt+=("$f")
        echo "    HOLD  $f — image not built"
        continue
    else
        rendered=$(sed "s|REPLACED_BY_DEPLOY|${CFG_SUM}|" "$path")
    fi
    # Instance network shape: the OIDC-backchannel hostAlias (and its NetworkPolicy peer) is
    # operator-specific. Manifests ship placeholders; the instance sets the real LAN IP + tailnet
    # host in bundle/.env. Defaults keep a forker's cluster applying cleanly.
    rendered=$(printf '%s\n' "$rendered" | sed \
        -e "s|__GATEWAY_LAN_IP__|${GATEWAY_LAN_IP}|g" \
        -e "s|__GATEWAY_TAILNET_HOST__|${GATEWAY_TAILNET_HOST}|g" \
        -e "s|__LAN_CIDR__|${LAN_CIDR}|g" \
        -e "s|__PORTAL_ADMINS__|${PORTAL_ADMINS:-}|g")
    echo "    apply $f"
    printf '%s\n' "$rendered" | kubectl apply -f -
done

if (( ${#unbuilt[@]} )); then
    echo >&2
    echo "warning: held back ${#unbuilt[@]} manifest(s) whose images this script does not build:" >&2
    printf '           %s\n' "${unbuilt[@]}" >&2
    echo "         Their images come from deploy/bin/kaniko-build.sh and are substituted by" >&2
    echo "         hand today, so this script has no tag to put in. Holding them is" >&2
    echo "         deliberate: 05/06/07 are already running the right images, and applying" >&2
    echo "         them blind would replace working workloads with the wrong one." >&2
    echo "         70-codeapi.yaml has never been deployed, which is why chat advertises" >&2
    echo "         code execution against a service that is not there (enterpriseaiframework-c8b)." >&2
    echo "         Closing this properly means deploy.sh builds and pushes every image it" >&2
    echo "         deploys, the way it already does for the control plane:" >&2
    echo "         enterpriseaiframework-d5f." >&2
fi

echo
echo "==> waiting for rollout"
# Derived from the namespace rather than hand-listed, for the same reason the apply loop is.
# ws-* are per-user workspace pods provisioned by provision-workspace.sh on demand; one
# user's broken pod is not a reason to fail a platform deploy.
#
# No `|| true` here. It used to swallow failed rollouts, so a deploy whose chat pod never
# came up still exited 0 — the same "every signal says success over a broken product"
# failure that enterpriseaiframework-0e97 and -00c are both instances of.
rollout_failed=0
for kind in statefulset deployment; do
    for name in $(kubectl -n "$NS" get "$kind" -o name | sed 's|.*/||' | grep -v '^ws-' | sort); do
        if ! kubectl -n "$NS" rollout status "$kind/$name" --timeout=600s; then
            echo "ERROR: $kind/$name did not roll out" >&2
            rollout_failed=1
        fi
    done
done
if (( rollout_failed )); then
    echo >&2
    echo "error: at least one workload failed to roll out; the deploy is NOT complete." >&2
    kubectl -n "$NS" get pods -o wide >&2
    exit 1
fi

echo
kubectl -n "$NS" get pods -o wide

echo
echo "==> post-deploy reconciliation"
# enterpriseaiframework-0e97: this was never called, and the founder hit the consequence in
# production — login succeeded, the first prompt returned 401, because the chat surface held
# a virtual key the gateway had no record of. post-deploy.sh already contained the guard
# that detects and repairs exactly that (probe CHAT_VIRTUAL_KEY against /key/info, and on
# anything but 200 delete the stale alias, mint a fresh key, patch the Secret, restart chat).
# A deploy that cannot serve a prompt should not exit 0, so this is part of deploying, not
# an optional follow-up somebody has to remember.
PUBLIC_BASE_URL="$PUBLIC_BASE_URL" deploy/bin/post-deploy.sh

echo
echo "==> smoke"
# The deploy's own definition of success. Pod health is not product health: 0e97 shipped a
# cluster where every pod was Running and the first prompt returned 401. This sends one.
deploy/bin/smoke.sh

cat <<EOF

  Chat (NodePort)      http://<k3s-worker>:30380     -> front with Caddy at ${PUBLIC_BASE_URL}
  Gateway (NodePort)   http://<k3s-worker>:30400     -> LAN/tailnet only, not public
  Control plane        kubectl -n ${NS} port-forward svc/control-plane 8081:8000

  Uninstall            kubectl delete namespace ${NS}
EOF
