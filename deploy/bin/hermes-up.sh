#!/usr/bin/env bash
# ONE COMMAND. A resident agent, on the metered Forge path, talking in the company's chat.
#
#   deploy/bin/hermes-up.sh <keycloak-username> <agent-name> \
#       [--chat slack|discord] [--slack-config-file FILE] [--discord-config-file FILE]
#       [--model NAME]
#
# Worked examples — the whole point of this file is that these are the whole command:
#
#   # Slack (the default) — a resident agent metered on the one bill, in your workspace
#   deploy/bin/hermes-up.sh alice hermes --slack-config-file ~/.secrets/hermes-slack.env
#
#   # Discord instead
#   deploy/bin/hermes-up.sh alice hermes --chat discord \
#       --discord-config-file ~/.secrets/hermes-discord.env
#
#   # Re-run it. Nothing restarts, nothing rotates, the credential file is not needed again.
#   deploy/bin/hermes-up.sh alice hermes
#
# THIS SCRIPT COMPOSES. IT IMPLEMENTS NOTHING.
# ===========================================
# Every mechanism it uses already exists and is already tested on its own:
#
#   * deploy/bin/provision-agent.sh — residency, object naming, the NetworkPolicy, the
#     integrated key mint through /admin/keys/issue, and the connector credential
#     handling (set-once, from a FILE, never argv, never read back).
#   * deploy/agent/agent-slack, agent-discord — the chat tools, including their own
#     `config` subcommand, which is what this script asks the pod rather than inventing a
#     second opinion about whether chat is wired.
#   * deploy/agent/entrypoint.sh — putting those tools on PATH and composing SLACK.md /
#     DISCORD.md into opencode's `instructions` so the model knows they exist.
#
# What is genuinely NEW here is only two things: a default (integrated Forge + Slack), and
# a REFUSAL TO CLAIM SUCCESS THAT HAS NOT BEEN OBSERVED.
#
# WHY THE VALIDATION IS THE PRODUCT
# =================================
# A turnkey command's failure mode is not that it errors. It is that it prints a
# reassuring summary over a half-built thing, and the operator finds out days later that
# the agent has been 401ing into a channel nobody reads. `kubectl rollout status` returning
# 0 does not mean the agent can infer, and a Secret existing does not mean the tool inside
# the pod can see it. So READY is printed only after all four of these were OBSERVED, each
# from inside the running pod where the claim actually has to be true:
#
#   1. the Deployment reports Available=True and the pod's phase is Running;
#   2. `agent-<chat> config` inside the pod reports its tokens present — the TOOL's own
#      report, not our inference from the Secret we applied;
#   3. the composed opencode config in the pod carries the connector's instructions doc,
#      because entrypoint.sh falls back to the image config on a compose failure and an
#      agent that has Slack but was never told so is a silently mute agent;
#   4. a REAL POST to /chat/completions from inside the pod returns 200 — through
#      $OPENAI_API_BASE with $OPENAI_API_KEY, both read from the pod's own environment.
#      That one call proves the egress allowlist, the minted virtual key, the gateway, the
#      upstream (Forge) and the model name all at once, and nothing short of it does.
#
# Step 4 also asserts the base is OUR gateway. That is what makes the word "Forge" in the
# summary true rather than assumed: a BYO agent (Contract 4) would answer with the user's
# own provider here, produce no ledger row, and must not be reported as metered.
#
# INTEGRATED IS NOT A DEFAULT THIS SCRIPT WILL LET YOU SLIP OUT OF. provision-agent.sh
# derives BYO mode from the environment as well as from flags, so `AGENT_BYO_API_BASE` set
# in a shell would silently turn this into an unmetered agent while the summary below still
# said "Forge". It is refused instead; use provision-agent.sh directly for BYO.
#
# IDEMPOTENT AND NON-DISRUPTIVE, inherited rather than re-implemented. provision-agent.sh
# does not rotate a live key and does not touch a connector credential it was not handed,
# and the pod template's annotations are hashes that come out identical on a re-run — so a
# second `hermes-up.sh` re-validates and re-prints, and does not end the resident session
# that is the entire product. The only thing this script does that costs anything is step
# 4's one-token inference, and that is the price of not lying.
#
# LIVE, AGAINST THE DEPLOYED CLUSTER, THIS NEEDS THE AGENTS-SURFACE DEPLOY.
# The control plane currently deployed predates the agents surface, so /admin/keys/issue
# will not mint `<user>::agents/<name>` and step 4 cannot pass against it. That is tracked
# on the ship checklist, enterpriseaiframework-a39. Until it lands, the live path is a
# locally-run control-plane app for the mint (enterpriseaiframework-ede) or an
# AGENT_OPENAI_API_KEY supplied by the operator.
set -euo pipefail

cd "$(dirname "$0")/../.."

NS=enterprise-ai

USAGE="usage: hermes-up.sh <keycloak-username> <agent-name>
                           [--chat slack|discord]
                           [--slack-config-file FILE] [--discord-config-file FILE]
                           [--model NAME]"
USER_NAME="${1:?${USAGE}}"
AGENT_NAME="${2:?${USAGE}}"
shift 2

CHAT=""
SLACK_CONFIG_FILE=""
DISCORD_CONFIG_FILE=""
MODEL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --chat)                CHAT="$2"; shift 2 ;;
        # FILES, never the tokens themselves — argv is world-readable in `ps`, and a Slack
        # `xoxb-` posts as the whole organisation. Passed straight through to
        # provision-agent.sh, which is where that discipline is implemented and tested.
        --slack-config-file)   SLACK_CONFIG_FILE="$2"; shift 2 ;;
        --discord-config-file) DISCORD_CONFIG_FILE="$2"; shift 2 ;;
        --model)               MODEL="$2"; shift 2 ;;
        -h|--help)             echo "$USAGE"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; echo "$USAGE" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------- the chat choice
# SLACK IS THE DEFAULT, and it is derived here rather than being a default the operator has
# to know: `hermes-up.sh alice hermes --slack-config-file F` must work with no --chat, and
# `--discord-config-file F` must not silently provision a Slack agent that has no Slack.
if [[ -z "$CHAT" ]]; then
    if [[ -n "$DISCORD_CONFIG_FILE" && -z "$SLACK_CONFIG_FILE" ]]; then
        CHAT=discord
    else
        CHAT=slack
    fi
fi
case "$CHAT" in
    slack|discord) ;;
    *) echo "refusing: --chat must be 'slack' or 'discord', not '${CHAT}'" >&2; exit 1 ;;
esac

# A config file for the OTHER platform is refused rather than quietly ignored. Supplying
# Discord's bot token and getting a Slack agent is the kind of "it did something, just not
# that" that costs an afternoon.
if [[ "$CHAT" == slack && -n "$DISCORD_CONFIG_FILE" ]]; then
    echo "refusing: --discord-config-file given but --chat is slack." >&2
    echo "  Pass --chat discord, or use --slack-config-file." >&2
    exit 1
fi
if [[ "$CHAT" == discord && -n "$SLACK_CONFIG_FILE" ]]; then
    echo "refusing: --slack-config-file given but --chat is discord." >&2
    echo "  Pass --chat slack, or use --discord-config-file." >&2
    exit 1
fi

if [[ "$CHAT" == slack ]]; then
    CHAT_FLAG="--slack-config-file"; CHAT_FILE="$SLACK_CONFIG_FILE"
    CHAT_SUM_KEY=AGENT_SLACK_CONFIG_SUM; CHAT_DOC=/etc/agent/SLACK.md
    CHAT_NOUN="Slack workspace"
else
    CHAT_FLAG="--discord-config-file"; CHAT_FILE="$DISCORD_CONFIG_FILE"
    CHAT_SUM_KEY=AGENT_DISCORD_CONFIG_SUM; CHAT_DOC=/etc/agent/DISCORD.md
    CHAT_NOUN="Discord guild"
fi

OBJ="agent-${USER_NAME}-${AGENT_NAME}"

# ---------------------------------------------------------------- refuse to be BYO
# provision-agent.sh derives BYO from the ENVIRONMENT as well as from its flags. An
# operator with AGENT_BYO_API_BASE exported from an earlier session would get an agent
# whose inference never touches our gateway — no ledger row, no budget, no audit entry —
# while this script's summary said "Forge, metered". Refused, with the alternative named.
if [[ -n "${AGENT_BYO_API_KEY:-}" || -n "${AGENT_BYO_API_BASE:-}" ]]; then
    echo "refusing: AGENT_BYO_API_KEY/AGENT_BYO_API_BASE is set in this environment." >&2
    echo "  hermes-up.sh is the INTEGRATED path: the agent's inference goes through our" >&2
    echo "  gateway on a minted ${USER_NAME}::agents/${AGENT_NAME} key, so it is metered," >&2
    echo "  budgeted and audited on the one bill. With those set, provision-agent.sh would" >&2
    echo "  build a BYO agent instead and this summary would be a lie." >&2
    echo "  Unset them, or call deploy/bin/provision-agent.sh --byo-key-file directly." >&2
    exit 1
fi

# ---------------------------------------------------------------- pre-flight
# Fail BEFORE changing anything if this run cannot possibly end in a chat-wired agent.
# Validation would catch it afterwards too, but only after minting a key and rolling a pod
# — and "it failed, and it also changed things" is the worst shape a turnkey command has.
#
# Reading the SUM (a hash provision-agent.sh stores beside the credential), never the
# credential: there is no path in this repo that reads a bot token back out, and this is
# not going to be the first one.
existing_in() {
    kubectl -n "$NS" get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null \
        | base64 -d 2>/dev/null || true
}
if [[ -z "$CHAT_FILE" ]]; then
    if [[ -z "$(existing_in "${OBJ}-${CHAT}" "$CHAT_SUM_KEY")" ]]; then
        echo "refusing: ${OBJ} has no ${CHAT} credential and none was supplied." >&2
        echo "  A turnkey agent with no ${CHAT_NOUN} is not what this command promises." >&2
        echo "  Pass ${CHAT_FLAG} FILE — a KEY=value file; see deploy/bin/provision-agent.sh" >&2
        echo "  for the exact keys. Nothing has been changed." >&2
        exit 1
    fi
fi

echo "==> hermes-up ${AGENT_NAME} for ${USER_NAME}  (chat: ${CHAT}, inference: integrated/Forge)"
echo

# ---------------------------------------------------------------- 1. provision
# The composition. No --byo-* flags, so provision-agent.sh takes its INTEGRATED default:
# a virtual key minted through the control plane with the alias `<user>::agents/<name>`,
# and OPENAI_API_BASE pointed at our gateway. The chat flag is the one that already exists.
PROVISION_ARGS=("$USER_NAME" "$AGENT_NAME")
[[ -n "$MODEL" ]] && PROVISION_ARGS+=(--model "$MODEL")
[[ -n "$CHAT_FILE" ]] && PROVISION_ARGS+=("$CHAT_FLAG" "$CHAT_FILE")

bash deploy/bin/provision-agent.sh "${PROVISION_ARGS[@]}"

echo
echo "==> validating (nothing is READY until all four of these are observed)"

fail() { echo; echo "NOT READY: $*" >&2; exit 1; }

# ---------------------------------------------------------------- 2. it is actually up
# Asserted independently of provision-agent.sh's `rollout status`. Not redundancy for its
# own sake: rollout status is satisfied by a Deployment whose ReplicaSet reached its
# target, and the pod behind it can still be CrashLoopBackOff by the time anyone looks.
AVAILABLE="$(kubectl -n "$NS" get deployment "$OBJ" \
    -o 'jsonpath={.status.conditions[?(@.type=="Available")].status}' 2>/dev/null || true)"
if [[ "$AVAILABLE" != "True" ]]; then
    fail "deployment/${OBJ} is not Available (condition reports '${AVAILABLE:-<none>}').
  kubectl -n ${NS} describe deployment/${OBJ}
  kubectl -n ${NS} logs -l agent.enterprise-ai/name=${AGENT_NAME} --tail=50"
fi

POD="$(kubectl -n "$NS" get pods \
    -l "app.kubernetes.io/component=agent,agent.enterprise-ai/user=${USER_NAME},agent.enterprise-ai/name=${AGENT_NAME}" \
    -o 'jsonpath={.items[0].metadata.name}' 2>/dev/null || true)"
[[ -n "$POD" ]] || fail "no pod found for ${OBJ}."

PHASE="$(kubectl -n "$NS" get pod "$POD" -o 'jsonpath={.status.phase}' 2>/dev/null || true)"
if [[ "$PHASE" != "Running" ]]; then
    fail "pod ${POD} is '${PHASE:-<none>}', not Running.
  kubectl -n ${NS} describe pod/${POD}"
fi
echo "    pod      ${POD} Running, deployment Available"

in_pod() { kubectl -n "$NS" exec "$POD" -c agent -- bash -c "$1"; }

# ---------------------------------------------------------------- 3. chat, per the tool
# The TOOL's own `config` subcommand, from inside the pod. Deliberately not our own
# re-derivation from the Secret we just applied: what has to be true is that the process
# which will post to ${CHAT_NOUN} can see its tokens, and the only thing that can answer
# that is that process. `config` prints presence booleans and never a token — that is its
# entire contract, asserted by tests/test_agent_slack.py and test_agent_discord.py.
CHAT_JSON="$(in_pod "command -v agent-${CHAT} >/dev/null || { echo not-on-path >&2; exit 3; }; agent-${CHAT} config" 2>/dev/null || true)"
if [[ -z "$CHAT_JSON" ]]; then
    fail "agent-${CHAT} did not report a configuration inside ${POD}.
  The tool should be on PATH from the agent-entrypoint ConfigMap; check:
  kubectl -n ${NS} exec ${POD} -c agent -- bash -lc 'command -v agent-${CHAT}'"
fi
# The required presence flags per platform: Slack needs BOTH tokens (the bot token posts,
# the app token opens the Socket Mode websocket that RECEIVES); Discord's one bot token
# does both. An agent that can talk and cannot listen is the failure this catches.
CHAT_MISSING="$(printf '%s' "$CHAT_JSON" | python3 -c '
import json, sys
required = {"slack": ["bot_token_set", "app_token_set"], "discord": ["bot_token_set"]}
cfg = json.load(sys.stdin)
print(" ".join(k for k in required[sys.argv[1]] if not cfg.get(k)))
' "$CHAT" 2>/dev/null || echo "unparseable")"
if [[ -n "$CHAT_MISSING" ]]; then
    fail "agent-${CHAT} inside ${POD} reports its credentials are not present: ${CHAT_MISSING}.
  The Secret ${OBJ}-${CHAT} exists but the pod is not seeing it — most often a pod that
  predates the credential, since envFrom is injected at pod start and never updated."
fi
echo "    chat     agent-${CHAT} reports its tokens present in-pod"

# The instructions doc, composed into opencode's config by entrypoint.sh. This is a
# separate failure from the one above and has to be checked separately: entrypoint.sh
# deliberately FALLS BACK to the image config when the compose fails, rather than
# CrashLoopBackOff-ing every agent in the deployment over a documentation file. The tool
# still works — but the model was never told it exists, so it will never reach for it, and
# an agent that silently never uses its chat connector is exactly the false READY this
# whole section exists to prevent.
DOC_STATE="$(in_pod "$(printf 'f="${XDG_DATA_HOME:-/workspace/.agent-state}/opencode.json"; if [ -f "$f" ] && grep -qF %q "$f"; then echo DOC_WIRED; else echo DOC_MISSING; fi' "$CHAT_DOC")" 2>/dev/null || true)"
if [[ "$DOC_STATE" != "DOC_WIRED" ]]; then
    fail "opencode in ${POD} was never told about ${CHAT_DOC}.
  The tool is on PATH and works, but the model has no instructions for it, so it will
  never use it. entrypoint.sh says why in the pod's log:
  kubectl -n ${NS} logs ${POD} | grep -i '^tools:'"
fi
echo "    tools    ${CHAT_DOC} composed into opencode's instructions"

# ---------------------------------------------------------------- 4. real inference
# ONE REAL REQUEST. Everything else above is a statement about configuration; this is the
# only step that proves the agent can actually do its job, and it proves the whole chain in
# one call: the NetworkPolicy egress allowlist admits the gateway, the minted virtual key
# authenticates, the gateway routes to the upstream (Forge), and the model name resolves.
#
# FROM INSIDE THE POD, using the pod's OWN $OPENAI_API_KEY and $OPENAI_API_BASE. That is
# not a convenience: it means this script never reads the key, never holds it, and never
# puts it in argv on the node — the outer command is single-quoted, so what `ps` shows on
# the host is the literal text `${OPENAI_API_KEY}`. It also makes the test STRONGER, since
# it exercises the exact credential and route the agent itself will use rather than a
# separate one that happens to work.
#
# max_tokens 1 — a fraction of a cent, and it lands a real ledger row under
# `<user>::agents/<name>`, which is the point.
GW_SCRIPT='
set -u
base="${OPENAI_API_BASE:-}"
model="${OPENCODE_MODEL#enterprise-ai/}"
code=$(curl -sS -o /dev/null -w "%{http_code}" -m 60 \
    -X POST "${base}/chat/completions" \
    -H "Authorization: Bearer ${OPENAI_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${model}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}" \
    ) || code=000
printf "%s %s %s\n" "$code" "$base" "$model"
'
GW_OUT="$(in_pod "$GW_SCRIPT" 2>/dev/null || true)"
# `|| true` because `read` reports non-zero on a short or empty line and `set -e` would
# turn "the exec produced nothing" into a bare exit with no diagnosis — which is exactly
# the case the message below exists to explain.
read -r GW_CODE GW_BASE GW_MODEL <<<"${GW_OUT:-}" || true
if [[ "${GW_CODE:-}" != "200" ]]; then
    fail "inference through the gateway returned '${GW_CODE:-<nothing>}', not 200.
  base ${GW_BASE:-<unknown>}, model ${GW_MODEL:-<unknown>}.
  000 means the pod could not reach the gateway at all — check the NetworkPolicy in
  deploy/k8s/63-agent-common.yaml. 401 means the minted key is not live at the gateway;
  4xx on the model name means it is not in the catalogue."
fi
# The base, ASSERTED rather than assumed, and this is what makes the word "Forge" below
# honest. A BYO agent (Contract 4) answers 200 here too — from the user's own provider,
# producing no ledger row at all. Reporting that as metered would be finding 4's silent
# unmetered path with a friendly summary printed over it.
if [[ "$GW_BASE" != "http://gateway:4000/v1" ]]; then
    fail "the agent's inference goes to '${GW_BASE}', not our gateway.
  That is a BYO agent: its spend produces NO row on the ledger. hermes-up.sh only
  reports agents on the integrated, metered path."
fi
echo "    forge    200 from ${GW_BASE} for ${GW_MODEL} (metered as ${USER_NAME}::agents/${AGENT_NAME})"

# ---------------------------------------------------------------- READY
PUBLIC_BASE_URL="$(kubectl -n "$NS" get secret enterprise-ai-secrets \
    -o 'jsonpath={.data.PUBLIC_BASE_URL}' 2>/dev/null | base64 -d 2>/dev/null || true)"
CONSOLE="${PUBLIC_BASE_URL}/agents/${AGENT_NAME}/"

echo
echo "READY"
echo "  agent    ${AGENT_NAME}  (objects: ${OBJ})"
echo "  status   Running — \`opencode serve\` is resident, holding the session with no"
echo "           console attached; it survives every connect and disconnect."
echo "  chat     ${CHAT} — agent-${CHAT} configured in-pod, instructions loaded"
echo "  forge    integrated: ${GW_BASE} -> Forge, model ${GW_MODEL}, verified 200"
echo "           metered, budgeted and audited as ${USER_NAME}::agents/${AGENT_NAME}"
echo "  console  ${CONSOLE}"
echo
echo "  Re-run this command any time: it re-validates and does NOT restart the agent."
echo "  Stop it with \`kubectl -n ${NS} scale deploy/${OBJ} --replicas=0\` (PVC kept)."
