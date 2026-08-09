#!/usr/bin/env bash
# Prove the deployment can actually serve a prompt. Run at the end of every deploy.
#
# WHY THIS EXISTS, and why "the pods are Running" is not the same claim.
#
# enterpriseaiframework-0e97: a deploy completed, every pod reported Running and healthy, the
# login page worked — and the founder's first prompt returned 401. The chat surface held a
# virtual key the gateway had no record of. Every signal a deploy emits said success while the
# product was unusable. enterpriseaiframework-00c is the same shape. A deploy that cannot
# serve a prompt must not exit 0, and the only way to know is to send one.
#
# LibreChat also fails this SILENTLY in a second way: given a key the gateway rejects it does
# not error at startup, it quietly falls back to the hardcoded model list in librechat.yaml.
# So the surface looks healthy while offering models it cannot reach.
#
# WHAT THIS DOES AND DOES NOT PROVE. It exercises the credential path that actually broke:
# the key the chat Deployment holds, against this gateway, completing a real inference call.
# It deliberately does NOT drive a browser — enterpriseaiframework-c31 has hung Playwright
# three times this week on healthy products, and a flaky gate that blocks deploys is worse
# than no gate. The full browser journey is tests-live/test_first_conversation.py, which the
# continuous-deploy watcher runs separately.
set -euo pipefail

cd "$(dirname "$0")/../.."
NS="${NS:-enterprise-ai}"

secret() { kubectl -n "$NS" get secret enterprise-ai-secrets -o jsonpath="{.data.$1}" | base64 -d; }
fail()   { echo "SMOKE FAILED: $*" >&2; exit 1; }

echo "==> smoke: the capabilities this deploy is supposed to have delivered"
# The other half of what silently drifted. A chat surface that comes up without these is the
# 'merged but not deployed' state that made search, file upload and sharing invisible for
# days while every item that shipped them was closed.
chat_env=$(kubectl -n "$NS" get deploy chat -o json)
for var in SEARCH MEILI_HOST RAG_API_URL ALLOW_SHARED_LINKS_PUBLIC; do
    if ! grep -q "\"name\":\"$var\"" <<<"$(tr -d ' \n' <<<"$chat_env")"; then
        fail "chat is running without $var. The manifest sets it, so the cluster is behind main."
    fi
done
echo "    chat carries SEARCH, MEILI_HOST, RAG_API_URL, ALLOW_SHARED_LINKS_PUBLIC"

# A service chat advertises but that is not running is enterpriseaiframework-c8b: the user
# gets an error on their first prompt, and only if their browser happened to persist a toggle.
for svc in meilisearch rag-api; do
    kubectl -n "$NS" get pods -l "app=$svc" --no-headers 2>/dev/null | grep -q " Running " \
        || echo "    WARNING: $svc is not Running; the capability chat advertises is absent" >&2
done

echo "==> smoke: the chat surface's OpenID login strategy is actually registered"
# enterpriseaiframework-6c9: LibreChat registers its OpenID passport strategy exactly once, at
# boot, and never retries. A boot before Keycloak/Caddy were serving the issuer left it
# unregistered, so /oauth/openid 500'd ("Unknown authentication strategy openid") and login
# was dead for four days while /health stayed 200 and every pod was Running. "The login page
# loaded" is a different claim from "login works" — this sends the request that actually broke.
# 302 -> registered (it redirects to Keycloak); 500 -> not. The wait-for-oidc initContainer in
# deploy/k8s/50-chat.yaml is what should keep this green; this proves it did.
kubectl -n "$NS" port-forward svc/chat 13080:3080 >/dev/null 2>&1 &
CPF=$!
for _ in $(seq 1 20); do
    curl -sf -o /dev/null "http://localhost:13080/health" && break
    sleep 1
done
login_code=$(curl -sS -o /dev/null -w '%{http_code}' "http://localhost:13080/oauth/openid" || echo 000)
kill $CPF 2>/dev/null || true
if [[ "$login_code" != "302" ]]; then
    fail "the chat surface's OpenID login strategy is not registered (/oauth/openid -> HTTP $login_code, expected 302). This is enterpriseaiframework-6c9: chat booted before the issuer was reachable and never retried. Restart chat once Keycloak+Caddy are serving the public issuer."
fi
echo "    /oauth/openid -> 302: OpenID strategy registered, login will work"

echo "==> smoke: a real completion using the key the chat surface holds"
kubectl -n "$NS" port-forward svc/gateway 14100:4000 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do
    curl -sf -o /dev/null "http://localhost:14100/health/liveliness" && break
    sleep 1
done

GW=http://localhost:14100
CHAT_KEY="$(secret CHAT_VIRTUAL_KEY 2>/dev/null || true)"
[[ -n "$CHAT_KEY" ]] || fail "CHAT_VIRTUAL_KEY is empty in the cluster Secret; post-deploy.sh did not run or did not mint one"

# The model the surface actually opens on, read from the deployed config rather than assumed,
# so this cannot pass against a model no user will ever be given.
MODEL="$(kubectl -n "$NS" get configmap chat-config -o jsonpath='{.data.librechat\.yaml}' \
    | awk '/^modelSpecs:/{f=1} f && /^[[:space:]]+model:/{sub(/.*model:[[:space:]]*/,""); gsub(/"/,""); print; exit}')"
[[ -n "$MODEL" ]] || fail "could not read the default model out of the deployed chat-config"
echo "    default model: $MODEL"

# max_tokens must be generous even though we want one word back. The default models are
# REASONING models, and deploy/gateway/strip_reasoning.py deliberately removes the reasoning
# trace before it reaches any surface. With a small budget the whole allowance is spent on
# reasoning that is then stripped, and the smoke test sees a 200 with empty content and fails
# a healthy cluster. Measured here: max_tokens=16 -> content ''; max_tokens=256 -> 'ready',
# 115 completion tokens. A gate that cries wolf gets disabled, so it gets room.
body=$(printf '{"model":%s,"messages":[{"role":"user","content":"reply with the single word: ready"}],"max_tokens":256}' \
       "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$MODEL")")

code=$(curl -sS -o /tmp/smoke-reply.json -w '%{http_code}' -X POST "${GW}/v1/chat/completions" \
    -H "Authorization: Bearer ${CHAT_KEY}" -H "Content-Type: application/json" -d "$body" || echo 000)

if [[ "$code" != "200" ]]; then
    echo "--- gateway said ---" >&2
    head -c 800 /tmp/smoke-reply.json >&2 || true
    echo >&2
    fail "the chat surface's own key could not complete a turn (HTTP $code). This is exactly enterpriseaiframework-0e97: login will work and the first prompt will 401."
fi

python3 - <<'PY' || exit 1
import json, sys
d = json.load(open("/tmp/smoke-reply.json"))
choices = d.get("choices") or []
if not choices:
    print("SMOKE FAILED: gateway returned 200 with no choices — a reply that is not a reply", file=sys.stderr)
    sys.exit(1)
content = (choices[0].get("message") or {}).get("content") or ""
if not content.strip():
    print("SMOKE FAILED: the completion came back empty", file=sys.stderr)
    sys.exit(1)
usage = d.get("usage") or {}
# Zero tokens means it never reached an upstream, which also means it never metered — the
# under-reporting failure the unpriced-model detector exists for.
if not usage.get("total_tokens"):
    print(f"SMOKE FAILED: usage reports no tokens ({usage}); nothing was actually served or billed", file=sys.stderr)
    sys.exit(1)
print(f"    served {usage.get('total_tokens')} tokens: {content.strip()[:60]!r}")
PY

rm -f /tmp/smoke-reply.json
echo
echo "==> smoke passed: the deployment serves prompts on the key the surface holds"
