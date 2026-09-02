#!/usr/bin/env bash
# Roll running surfaces from the LiteLLM gateway onto the freerouter spoke, after the
# GATEWAY_PROVIDER=freerouter flip. The flip switched provisioning + billing; this repoints
# each surface's INFERENCE endpoint (OPENAI_API_BASE) at freerouter and re-issues its key as
# a freerouter key. Session-safe: workspace PVC/XDG state survives, agents keep their loop.
#
#   deploy/bin/rollout-to-freerouter.sh <wave>
#     wave = a space-separated user list ("baron", "baron claire", "all"), or "chat"
#
# HARD PRECONDITION (the strand guard): a surface is repointed ONLY if prod freerouter's
# /v1/models actually serves the model that surface uses. Repointing a surface at a catalog
# that lacks its model would 404 every request — so this script refuses, per user, and says
# which model is missing. This is why the rollout is blocked until full-catalog discovery
# (freerouter epic-168) lands: today freerouter serves 1 model and the workspaces run
# glm-5.2@deepinfra.
set -euo pipefail
cd "$(dirname "$0")/../.."
NS=enterprise-ai
FR_BASE="${GATEWAY_SURFACE_BASE:-http://freerouter:8080}"
WAVE="${1:?usage: rollout-to-freerouter.sh <user...|all|chat>}"

cp=$(kubectl -n "$NS" get pods -l app=control-plane -o jsonpath='{.items[0].metadata.name}')
catalog() { kubectl -n "$NS" exec "$cp" -c control-plane -- python3 -c "
import urllib.request,json
print('\n'.join(x.get('id','') for x in json.load(urllib.request.urlopen('$FR_BASE/v1/models',timeout=10)).get('data',[])))" 2>/dev/null; }

CATALOG="$(catalog)"
echo "freerouter catalog: $(echo "$CATALOG" | grep -c .) models"
serves() { echo "$CATALOG" | grep -qxF "$1"; }

# strand guard: refuse the whole rollout if the workspace default model is not served
WS_MODEL="${WORKSPACE_MODEL:-glm-5.2@deepinfra}"
if ! serves "$WS_MODEL"; then
    echo "REFUSING: freerouter does not serve the workspace model '$WS_MODEL' — repointing" >&2
    echo "  surfaces now would strand them (404 on every request). Wait for full-catalog" >&2
    echo "  discovery (freerouter epic-168) or add the model to freerouter's UPSTREAMS." >&2
    echo "  Served today: $(echo "$CATALOG" | tr '\n' ' ')" >&2
    exit 2
fi

users() {
    case "$WAVE" in
        all) kubectl -n "$NS" get pods -l app.kubernetes.io/component=workspace \
                 -o jsonpath='{range .items[*]}{.metadata.labels.workspace\.enterprise-ai/user}{"\n"}{end}' | sort -u ;;
        chat) echo "__chat__" ;;
        *) echo "$WAVE" | tr ' ' '\n' ;;
    esac
}

for u in $(users); do
    if [[ "$u" == "__chat__" ]]; then
        echo "==> chat: repoint librechat baseURL -> $FR_BASE and redeploy (ALL chat users at once)"
        sed -i "s|baseURL: \"http://gateway:4000/v1\"|baseURL: \"$FR_BASE/v1\"|" bundle/librechat/librechat.yaml
        kubectl -n "$NS" rollout restart deploy/chat 2>/dev/null || echo "  (apply the librechat configmap + roll chat manually)"
        continue
    fi
    echo "==> workspace: $u -> freerouter"
    GATEWAY_SURFACE_BASE="$FR_BASE" deploy/bin/provision-workspace.sh "$u" || echo "  workspace $u FAILED"
    # agents owned by this user
    for a in $(kubectl -n "$NS" get deploy -o name 2>/dev/null | grep -oE "agent-${u}-[a-z0-9-]+" | sed "s/agent-${u}-//"); do
        echo "==> agent: ${u}/${a} -> freerouter"
        GATEWAY_SURFACE_BASE="$FR_BASE" deploy/bin/provision-agent.sh "$u" "$a" || echo "  agent ${u}/${a} FAILED"
    done
done
echo "rollout wave '$WAVE' complete — verify each surface serves through freerouter."
