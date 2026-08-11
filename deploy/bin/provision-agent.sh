#!/usr/bin/env bash
# Provision one named, RESIDENT agent instance.
#
#   deploy/bin/provision-agent.sh <keycloak-username> <agent-name> [--model NAME]
#   deploy/bin/provision-agent.sh <user> <name> --byo-key-file FILE --byo-api-base URL
#
# A PARALLEL script to deploy/bin/provision-workspace.sh, not a generalisation of it.
# Contract 6 of docs/design/records/agents-surface.md freezes the Code/workspace surface
# byte-for-byte — the camp runs on it — so this file, deploy/k8s/63-agent-common.yaml and
# deploy/k8s/64-agent.template.yaml sit BESIDE the frozen set and never edit it.
# tests/test_agents_code_untouched.py makes that mechanical.
#
# THIS IS THE HERMES RETARGET (docs/design/records/agents-surface-hermes-retarget.md). An
# Agent is a long-lived autonomous Hermes Agent (NousResearch), not an opencode coding
# session — the two surfaces were conflated and are now separate. The resident process is
# `hermes gateway run`, the console attaches `hermes --tui` over the Kubernetes pods/exec
# subresource, and the pod runs the Hermes image, not the workspace artefact.
#
# What it guarantees, in the order the guarantees matter:
#
#   1. RESIDENCY. The pod's own process is `hermes gateway run` — the foreground messaging
#      gateway + cron scheduler, supervised by the image's s6-overlay, holding its session
#      with NO console attached and keeping it across every connect and disconnect. An
#      agent that needed a browser open would be a workspace with a different tab.
#   2. Object names are `agent-<user>-<name>` (Contract 1): the PVC, the Deployment, the
#      per-agent config ConfigMap and the Secret. One greppable family, mirroring `ws-<user>`.
#   3. The agent publishes NO inbound port. `hermes gateway run` is outbound-only; the
#      console attaches over the API server's pods/exec subresource, not a pod Service, so
#      there is no server to guard with a password. The portal decides WHICH agent you
#      reach from your authenticated name (enterpriseaiframework-0e7), re-checked against
#      the owner label, so a request cannot name someone else's.
#   4. The pod cannot reach the Kubernetes API, a workspace, another agent, the control
#      plane, Postgres or identity. Its in-cluster egress is an allowlist of NAMED
#      services — kube-dns, the gateway, and the MCP tool servers — never the namespace
#      and never the pod CIDR. Read the NetworkPolicy in 63-agent-common.yaml; do not
#      paraphrase it here, that is how finding 37 happened.
#
# IDEMPOTENT, AND DELIBERATELY NON-DISRUPTIVE. Re-running this for a healthy agent must
# not restart it: restarting an agent ends the resident session that is the whole product.
# So, unlike provision-workspace.sh, this does NOT rotate a credential on every run, and
# the pod template's rollout annotations track the config inputs and the credential hashes
# rather than the credential values.
#
# THE MODEL API IS CONFIGURABLE (Contract 4, enterpriseaiframework-39d). Two modes, and
# the difference between them is where the agent's inference goes and whose money it is:
#
#   INTEGRATED (the default). The control plane mints a per-agent virtual key with the
#     alias `<user>::agents/<name>` and this script writes it into the pod's Secret,
#     replacing -055's sentinel. OPENAI_API_BASE is our gateway, so the agent's spend is
#     metered, budgeted, audited, and lands on the one bill under the per-instance surface
#     `agents/<name>`. Minted through /admin/keys/issue, never straight at the gateway:
#     minting at the gateway leaves the ledger's recorded token hash pointing at a key
#     that no longer exists, and every later budget change fails silently.
#
#   BYO (--byo-key-file + --byo-api-base). The user's OWN provider credential, stored in a
#     separate Secret `agent-<user>-<name>-byo`, with OPENAI_API_BASE pointed at THEIR
#     provider. No virtual key is minted and the traffic never touches our gateway, so by
#     construction it produces ZERO gateway ledger rows. That is permitted — it is the
#     customer's own credential on their own per-user resident, which the standing
#     constraint explicitly allows — but only because it is DECLARED: the pod carries
#     `agent.enterprise-ai/model-source: byo`, so no spend view can render it as a silent
#     $0. Finding 4's leak was an unmetered path that looked healthy; this is an unmetered
#     path that says so.
#
# THE BYO KEY IS THE SENSITIVE SURFACE, and it is handled set-once, exactly like
# /portal/api/keys/rotate hands back a secret once and never again. It is read from a FILE
# (or AGENT_BYO_API_KEY), never from a command-line argument — argv is world-readable in
# `ps` — it is never echoed, never logged, and there is no path in this script or in the
# control plane that reads it back out. Re-supplying it is the only way to "rotate" it.
#
# STILL DELIBERATELY NON-DISRUPTIVE. An integrated agent that already holds a real key
# does NOT get it rotated on a re-run: rotation deletes the old key at the gateway, and
# because the pod template's rollout annotation tracks the credential's hash rather than
# the key itself (see 64-agent.template.yaml), the running daemon would keep presenting a
# credential that no longer exists and start 401ing with nothing on screen to say why.
# Minting happens when there is no usable key — first provision, or -055's sentinel.
set -euo pipefail

cd "$(dirname "$0")/../.."

NS=enterprise-ai
# The Hermes Agent image. Named configuration, NOT the workspace artefact: an Agent runs
# Hermes (a different product from opencode), so its image is a pinned date tag rather than
# something read off a workspace pod. `0.8.0` does not exist on Docker Hub; the tags are
# date-based (vYYYY.M.D). Overridable per deployment via AGENT_IMAGE, matching
# control-plane/app/agents.py's HERMES_IMAGE default so the two renderers agree.
IMAGE="${AGENT_IMAGE:-nousresearch/hermes-agent:v2026.8.3}"

USAGE="usage: provision-agent.sh <keycloak-username> <agent-name> [--model NAME]
                                 [--byo-key-file FILE] [--byo-api-base URL]
                                 [--email-config-file FILE]
                                 [--slack-config-file FILE] [--discord-config-file FILE]"
USER_NAME="${1:?${USAGE}}"
AGENT_NAME="${2:?${USAGE}}"
shift 2
MODEL="${AGENT_MODEL:-glm-5.2@deepinfra}"
BYO_KEY_FILE=""
BYO_API_BASE="${AGENT_BYO_API_BASE:-}"
EMAIL_CONFIG_FILE="${AGENT_EMAIL_CONFIG_FILE:-}"
SLACK_CONFIG_FILE="${AGENT_SLACK_CONFIG_FILE:-}"
DISCORD_CONFIG_FILE="${AGENT_DISCORD_CONFIG_FILE:-}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)         MODEL="$2"; shift 2 ;;
        # A FILE, never the key itself. `--byo-key sk-...` would put a live provider
        # credential into argv, where every process on the node can read it out of `ps`.
        --byo-key-file)  BYO_KEY_FILE="$2"; shift 2 ;;
        --byo-api-base)  BYO_API_BASE="$2"; shift 2 ;;
        # Same rule, same reason: the mailbox app-password never appears in argv.
        --email-config-file) EMAIL_CONFIG_FILE="$2"; shift 2 ;;
        # And the same again for the chat bot tokens (enterpriseaiframework-783). A Slack
        # `xoxb-` token posts as the whole organisation in every channel the app is in.
        --slack-config-file) SLACK_CONFIG_FILE="$2"; shift 2 ;;
        --discord-config-file) DISCORD_CONFIG_FILE="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

GATEWAY_BASE="http://gateway:4000/v1"

# The model's context window and output cap, seeded into the per-agent config.yaml. Both
# REQUIRED and both, when wrong, surface as a misleading "context length exceeded": Hermes
# cannot read a window from our gateway's /v1/models (assumes ~0 without this), and an
# over-cap max_tokens 400s at the provider (deepinfra caps some models at 32768). Same
# defaults as control-plane/app/agents.py so the two renderers agree byte-for-byte.
CONTEXT_LENGTH="${AGENT_CONTEXT_LENGTH:-128000}"
MAX_TOKENS="${AGENT_MAX_TOKENS:-8000}"

# The mode is derived from what was supplied rather than from a --mode flag that could
# disagree with it. Both halves are required together: a BYO key with no base URL would
# send the user's own provider credential to OUR gateway, which is the one combination
# that must never happen by accident.
BYO_KEY=""
if [[ -n "$BYO_KEY_FILE" || -n "${AGENT_BYO_API_KEY:-}" || -n "$BYO_API_BASE" ]]; then
    MODEL_SOURCE="byo"
    if [[ -n "$BYO_KEY_FILE" ]]; then
        [[ -r "$BYO_KEY_FILE" ]] || { echo "cannot read --byo-key-file ${BYO_KEY_FILE}" >&2; exit 1; }
        # Trailing newline stripped: a key with a \n on the end is a 401 nobody diagnoses.
        BYO_KEY="$(tr -d '\r\n' < "$BYO_KEY_FILE")"
    else
        BYO_KEY="${AGENT_BYO_API_KEY:-}"
    fi
    if [[ -z "$BYO_KEY" ]]; then
        echo "refusing: BYO mode needs a key (--byo-key-file FILE or AGENT_BYO_API_KEY)" >&2
        exit 1
    fi
    if [[ -z "$BYO_API_BASE" ]]; then
        echo "refusing: BYO mode needs --byo-api-base URL — the external provider's" >&2
        echo "  OpenAI-compatible base. Without it this agent would present the user's own" >&2
        echo "  provider credential to our gateway, which cannot spend it and must not see it." >&2
        exit 1
    fi
    if [[ "$BYO_API_BASE" == "$GATEWAY_BASE" || "$BYO_API_BASE" == *"//gateway:"* ]]; then
        echo "refusing: --byo-api-base points at our own gateway (${BYO_API_BASE})." >&2
        echo "  BYO means the traffic routes AROUND this layer; pointing it back here would" >&2
        echo "  hand the gateway a credential it cannot use and label the agent 'byo' while" >&2
        echo "  it is nothing of the kind." >&2
        exit 1
    fi
    API_BASE="$BYO_API_BASE"
    KEY_SECRET_SUFFIX="byo"
else
    MODEL_SOURCE="integrated"
    API_BASE="$GATEWAY_BASE"
    KEY_SECRET_SUFFIX="key"
fi

# ---------------------------------------------------------------- names, constrained
# The SAME pattern the workspace already enforces on project names
# (deploy/workspace/shell-server.py: SLUG_OK). Constrained, not sanitised, for the reason
# that file gives: a rejected name is easy to explain, a silently rewritten one is not.
# It is also load-bearing for Contract 1's alias grammar `<user>::agents/<name>` — it
# guarantees <name> contains no `::` and no `/`.
SLUG='^[a-z0-9][a-z0-9-]{0,38}$'
if [[ ! "$AGENT_NAME" =~ $SLUG ]]; then
    echo "refusing: agent name '${AGENT_NAME}' must match ${SLUG}" >&2
    exit 1
fi
if [[ ! "$USER_NAME" =~ $SLUG ]]; then
    echo "refusing: username '${USER_NAME}' must match ${SLUG} to be part of an object name" >&2
    exit 1
fi

OBJ="agent-${USER_NAME}-${AGENT_NAME}"
# RFC 1123 caps a k8s object name at 63 characters. REFUSE rather than truncate: a
# truncated name that collides with another user's agent is the one failure mode worth
# failing loudly on, because its symptom is two people sharing one agent's PVC.
if (( ${#OBJ} > 63 )); then
    echo "refusing: object name '${OBJ}' is ${#OBJ} characters, over the RFC 1123 limit of 63." >&2
    echo "  Shorten the agent name. This is not truncated on purpose: a truncated name can" >&2
    echo "  collide with another user's agent and silently share its volume." >&2
    exit 1
fi

KEY_SECRET="${OBJ}-${KEY_SECRET_SUFFIX}"

echo "==> agent ${AGENT_NAME} for ${USER_NAME}"
echo "    objects  ${OBJ}"
echo "    image    ${IMAGE}"
echo "    model    ${MODEL}"
echo "    api      ${MODEL_SOURCE} -> ${API_BASE}"

# ---------------------------------------------------------------- shared objects
# ServiceAccount + NetworkPolicy for the agents component. Applied here for the same
# reason provision-workspace.sh applies its own common file: an agent must never be able
# to exist before the policy that fences it does.
kubectl apply -f deploy/k8s/63-agent-common.yaml >/dev/null

# NO deployment-wide entrypoint ConfigMap any more. The opencode surface delivered its
# entrypoint plus every outside-world tool as a shared `agent-entrypoint` ConfigMap mounted
# at /etc/agent; the Hermes image carries its own entrypoint (s6-overlay), and its
# messaging connectors are read by `hermes gateway run` from the ENVIRONMENT, not from shell
# tools on a mounted PATH — so there is nothing deployment-wide to ship. The per-agent
# config.yaml is seeded from the ConfigMap the template renders inline (agent-<user>-<name>-
# config), copied onto the PVC by the init container in 64-agent.template.yaml.
#
# checksum/config over the inputs that DEFINE that seeded config.yaml, so a change to the
# model, the window, the cap or the gateway rolls the pod (env is injected at start and
# never updated). It MUST match control-plane/app/agents.py's CFGSUM over the same canonical
# string — sha256("<api_base>|<model>|<context_length>|<max_tokens>")[:16] — or provisioning
# by either route would needlessly roll the other's agents. `printf %s` (no trailing
# newline) so the bytes hashed are exactly the Python f-string's.
CFGSUM=$(printf '%s' "${API_BASE}|${MODEL}|${CONTEXT_LENGTH}|${MAX_TOKENS}" \
         | sha256sum | cut -c1-16)

# ---------------------------------------------------------------- the pod's secret
# Read what is already there FIRST. Re-provisioning must not roll a credential out from
# under a running agent, and must not silently replace a real key with the sentinel.
existing() { existing_in "${OBJ}-key" "$1"; }
existing_in() {
    kubectl -n "$NS" get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null \
        | base64 -d 2>/dev/null || true
}

# -055's sentinel, spelled so that anyone who greps a 401 finds the item that fixed it.
# It is still the value written when no key can be minted, and it is what this script
# looks for to decide that an agent needs one.
KEY_SENTINEL="unset-pending-enterpriseaiframework-39d"
API_KEY="${AGENT_OPENAI_API_KEY:-$(existing OPENAI_API_KEY)}"

# ---------------------------------------------------------------- the model API
secret_value() { kubectl -n "$NS" get secret enterprise-ai-secrets -o "jsonpath={.data.$1}" | base64 -d; }

if [[ "$MODEL_SOURCE" == "byo" ]]; then
    # Switching an integrated agent to BYO would leave its virtual key live at the gateway
    # — spendable, attributed to a user whose agent is no longer using it, and invisible
    # because nothing routes through it any more. Refuse rather than leave that behind.
    if [[ -n "$API_KEY" && "$API_KEY" != "$KEY_SENTINEL" ]]; then
        echo "refusing: ${OBJ} already holds an integrated virtual key." >&2
        echo "  Switching it to BYO here would leave that key live and spendable at the" >&2
        echo "  gateway with nothing using it. Revoke ${USER_NAME}::agents/${AGENT_NAME}" >&2
        echo "  first, then re-run with --byo-key-file." >&2
        exit 1
    fi
    # The pod's own Secret holds ONLY OPENAI_API_KEY, and it stays the sentinel — the pod
    # does not read it in this mode, and a real key sitting unused in a Secret is a
    # credential with no owner. There is no console password: `hermes gateway run` opens no
    # inbound port, so the OPENCODE_SERVER_PASSWORD the opencode surface stored is retired.
    kubectl -n "$NS" create secret generic "${OBJ}-key" \
        --from-literal=OPENAI_API_KEY="$KEY_SENTINEL" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null

    # SET-ONCE. Written here and never read back: nothing in this script, in the control
    # plane, or in any list endpoint returns it. Rotating BYO is re-supplying it.
    #
    # --from-file, not --from-literal, and that is the one place this differs from how
    # provision-workspace.sh writes OUR virtual key. `--from-literal=K=<secret>` puts the
    # value in argv, where any process on the host can read it out of `ps` for the life of
    # the call. That is an accepted cost for a virtual key we minted and can revoke in one
    # call; it is not an accepted cost for the user's own external provider credential,
    # which we cannot revoke and which buys tokens on their account, not ours.
    BYO_TMP="$(umask 077; mktemp -t agent-byo-XXXXXX)"
    trap 'rm -f "$BYO_TMP"' EXIT
    printf '%s' "$BYO_KEY" > "$BYO_TMP"
    unset BYO_KEY
    # A hash of the credential, never the credential. See checksum/api-key in the template
    # for why the pod has to roll when this changes.
    KEYSUM=$(sha256sum "$BYO_TMP" | cut -c1-16)
    kubectl -n "$NS" create secret generic "${OBJ}-byo" \
        --from-file=OPENAI_API_KEY="$BYO_TMP" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    rm -f "$BYO_TMP"
    trap - EXIT
    echo "    key      external provider credential stored in ${OBJ}-byo (not shown, not readable back)"
    echo "    ledger   NONE by design — this agent's inference does not traverse our gateway"
else
    kubectl -n "$NS" delete secret "${OBJ}-byo" --ignore-not-found >/dev/null

    if [[ -z "$API_KEY" || "$API_KEY" == "$KEY_SENTINEL" ]]; then
        # Through the control plane, never straight at the gateway — see the header. The
        # surface is Contract 1's `agents/<name>`, which /admin/keys/issue accepts because
        # issuance.issue treats it as a surface like any other; the alias it produces is
        # asserted below rather than assumed.
        kubectl -n "$NS" port-forward svc/control-plane 18091:8000 >/dev/null 2>&1 &
        CPF=$!
        trap 'kill $CPF 2>/dev/null || true' EXIT
        CP=http://localhost:18091
        for _ in $(seq 1 40); do
            curl -sS -o /dev/null -m 2 "${CP}/health" && break || sleep 1
        done

        ADMIN_TOKEN="$(secret_value CONTROL_PLANE_ADMIN_TOKEN)"
        # The principal must exist in the control plane's own table before a key can be
        # minted for it; /admin/sync is what puts it there, and it is idempotent.
        curl -sS -o /dev/null -X POST "${CP}/admin/sync" -H "Authorization: Bearer ${ADMIN_TOKEN}"
        ISSUED=$(curl -sS -X POST "${CP}/admin/keys/issue" \
            -H "Authorization: Bearer ${ADMIN_TOKEN}" -H "Content-Type: application/json" \
            -d "$(python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1], "surface": "agents/" + sys.argv[2]}))' \
                  "$USER_NAME" "$AGENT_NAME")")
        API_KEY=$(printf '%s' "$ISSUED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("key",""))' 2>/dev/null || true)
        VALIAS=$(printf '%s' "$ISSUED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("key_alias",""))' 2>/dev/null || true)
        kill $CPF 2>/dev/null || true
        trap - EXIT

        if [[ -z "$API_KEY" ]]; then
            # $ISSUED is an error body here, not a key — a control plane that issued
            # nothing has nothing secret to leak, and the reason is the whole diagnosis.
            echo "control plane did not issue a key for agents/${AGENT_NAME}: ${ISSUED}" >&2
            exit 1
        fi
        # Contract 1's grammar, checked rather than trusted. A third `::` or a lost
        # instance name here means the ledger will attribute this agent's spend to the
        # wrong user or to the whole agents family, and both are silent.
        if [[ "$VALIAS" != "${USER_NAME}::agents/${AGENT_NAME}" ]]; then
            echo "refusing: expected alias ${USER_NAME}::agents/${AGENT_NAME}, got '${VALIAS}'" >&2
            exit 1
        fi
        if [[ "$API_KEY" == "$(secret_value GATEWAY_MASTER_KEY)" ]]; then
            echo "refusing: the issued key is the gateway master key" >&2
            exit 1
        fi
        echo "    key      ${VALIAS} (minted)"
    else
        echo "    key      ${USER_NAME}::agents/${AGENT_NAME} (kept; re-running does not rotate a live agent)"
    fi

    KEYSUM=$(printf '%s' "$API_KEY" | sha256sum | cut -c1-16)
    # ONLY OPENAI_API_KEY (the OPENCODE_SERVER_PASSWORD is retired — no inbound port to
    # guard; the console authenticates over pods/exec by RBAC + the owner-label re-check).
    kubectl -n "$NS" create secret generic "${OBJ}-key" \
        --from-literal=OPENAI_API_KEY="$API_KEY" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi

# ------------------------------------------- the outside world: mail, Slack, Discord
# The agent's EMAIL capability (enterpriseaiframework-a4e) and its CHAT connectors
# (enterpriseaiframework-783). In every case an EXTERNAL provider the tenant already has —
# M365, Gmail or any IMAP+SMTP host; the tenant's own Slack workspace; the tenant's own
# Discord guild — reached with the tenant's own credential.
#
# THERE IS NO MAIL SERVER AND NO CHAT SERVER IN THIS DEPLOYMENT AND THERE MUST NEVER BE
# ONE: no Maddy, no Stalwart, no Postfix, no Mattermost, no Rocket.Chat, no Zulip, no chat
# or mail component in any manifest. That is Baron's ruling on -a4e and -783, and
# tests/test_agent_no_chat_server.py asserts it against every deploy manifest rather than
# trusting this comment.
#
# The Secret KEYS are Hermes's OWN native env var names (verified against
# nousresearch/hermes-agent:v2026.8.3), so `hermes gateway run` reads them directly with no
# translation. They match control-plane/app/agents.py CONNECTORS exactly — the two are bound
# by test_the_python_schema_matches_the_shell_provisioners_allowlists. Hermes denies unknown
# senders by default, so a connector with no *_ALLOWED_USERS connects but answers no one;
# that is the secure default, and the allow-list keys below are how you open it.
#
# Each credential is handled EXACTLY like the BYO key above, for the same reason: it is the
# user's own external credential, we cannot revoke it, and it buys real authority — a
# mailbox password sends mail as a real person at a real company, and a Slack `xoxb-` token
# posts as the organisation in every channel its app is in. So it comes from a FILE and
# never from argv, it is written set-once, it is never echoed, and there is no path in this
# script or in the control plane that reads it back out. Re-supplying the file is the only
# rotation.
#
# ONE FUNCTION, THREE CALLS. The three connectors differ only in their allowlist, their
# required keys and their nouns; everything that is actually load-bearing — the file is
# never printed, CRLF is refused, only allowlisted keys survive, the hash lives beside the
# credential — is identical, and three copies of it would be three places for one of those
# properties to quietly stop being true.
#
# Each config file is `KEY=value` per line, the shape `kubectl create secret
# --from-env-file` takes. Worked examples:
#
#     # --email-config-file (an M365 mailbox; Hermes auto-detects ports and TLS)
#     EMAIL_ADDRESS=ops-agent@contoso.com
#     EMAIL_PASSWORD=<app password>
#     EMAIL_SMTP_HOST=smtp.office365.com
#     EMAIL_IMAP_HOST=outlook.office365.com
#     EMAIL_ALLOW_ALL_USERS=true            # optional; blank = deny-by-default
#
#     # --slack-config-file (a Slack app with Socket Mode enabled)
#     SLACK_BOT_TOKEN=xoxb-...
#     SLACK_APP_TOKEN=xapp-...
#     SLACK_HOME_CHANNEL=C0123ABCD          # optional
#     SLACK_ALLOWED_USERS=U0123ABCD         # optional; without it the bot answers no one
#
#     # --discord-config-file (a Discord application's bot)
#     DISCORD_BOT_TOKEN=...
#     DISCORD_HOME_CHANNEL=123456789012345678   # optional
#     DISCORD_ALLOWED_USERS=987654321098765432  # optional; without it the bot answers no one
#
# Values are taken literally — kubectl does not strip quotes — so a token wrapped in quotes
# becomes a token WITH quotes, which is a 401 nobody diagnoses.

# provision_connector <label> <flag> <file> <noun> <sum-key> <none-message>
#                     <allowed-keys> <required-keys>
#
# Sets CONNECTOR_SUM to the value the pod template's checksum/<label> annotation renders
# from: the hash of the supplied file, the hash already stored beside an existing
# credential, or "none".
CONNECTOR_SUM=""
provision_connector() {
    local label="$1" flag="$2" file="$3" noun="$4" sum_key="$5" none_msg="$6"
    local allowed="$7" required="$8"
    local secret="${OBJ}-${label}"
    local k line tmp

    if [[ -z "$file" ]]; then
        # Untouched. A re-provision that says nothing about this connector must not delete
        # the credential of a running agent, and must not roll it either.
        CONNECTOR_SUM="$(existing_in "$secret" "$sum_key")"
        if [[ -n "$CONNECTOR_SUM" ]]; then
            printf '    %-8s %s (kept; re-supply %s to rotate)\n' "$label" "$secret" "$flag"
        else
            CONNECTOR_SUM="none"
            printf '    %-8s %s\n' "$label" "$none_msg"
        fi
        return 0
    fi

    [[ -r "$file" ]] || { echo "cannot read ${flag} ${file}" >&2; exit 1; }

    # Only allowlisted keys, checked here rather than trusted. The pod injects this Secret
    # with `envFrom`, so every key in it becomes an environment variable in a container that
    # holds a spendable API key — a file containing `PATH=/tmp/evil` or `LD_PRELOAD=...`
    # would be an arbitrary-code-execution channel dressed up as a chat setting. The
    # template's explicit `env:` already wins over `envFrom` for OPENAI_API_KEY, but that
    # only defends the one name anyone thought of.
    #
    # Parsed for VALIDATION only, and kubectl reads the file itself — nothing here is passed
    # on. Be precise about what that does and does not mean: `$line` DOES hold the
    # credential for one iteration, because a line-oriented parser cannot avoid it. What is
    # guaranteed is that no branch below prints `$line`, that only `$k` (the key name)
    # survives the loop, and that the refusal messages name the KEY and never the value —
    # which is the case that matters, since the malformed-line branch is exactly where
    # somebody has pasted a bare credential in by mistake.
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "${line//[[:space:]]/}" || "${line#"${line%%[![:space:]]*}"}" == \#* ]] && continue
        # CRLF, refused rather than tolerated or silently stripped. kubectl stores values
        # literally, so a file saved on Windows gives every setting a trailing carriage
        # return: a host becomes "smtp.office365.com\r" (DNS failure) and a token becomes
        # "xoxb-...\r" (authentication failure). Both present as "it is broken" with nothing
        # pointing at the file. The BYO key path strips \r for the same reason; here the
        # whole file is at stake, so it is a refusal with a diagnosis.
        if [[ "$line" == *$'\r' ]]; then
            echo "refusing: ${file} has Windows (CRLF) line endings." >&2
            echo "  Every value would gain a trailing carriage return, which reads as a" >&2
            echo "  wrong host and a wrong credential. Convert it: dos2unix, or" >&2
            echo "  \`sed -i 's/\\r\$//' ${file}\`." >&2
            exit 1
        fi
        if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
            echo "refusing: ${file} has a line that is not KEY=value." >&2
            echo "  (the offending value is not printed, on purpose)" >&2
            exit 1
        fi
        k="${line%%=*}"
        case " ${allowed//$'\n'/ } " in
            *" $k "*) ;;
            *)  echo "refusing: '${k}' is not a ${noun}." >&2
                echo "  This file becomes the agent pod's environment via envFrom, so an" >&2
                echo "  unexpected key here is an environment variable in a container that" >&2
                echo "  holds a spendable API key. Allowed:" >&2
                echo "    ${allowed//$'\n'/ }" >&2
                exit 1 ;;
        esac
    done < "$file"

    # Every required key, or none of them. A config with SMTP and no IMAP produces an agent
    # that can send and cannot read; a Slack config with a bot token and no app token
    # produces an agent that can post and can never hear an answer. Both read as "it is
    # broken" long after the provisioning that caused it, with nothing connecting the two.
    local requirement
    for requirement in $required; do
        grep -qE "^[[:space:]]*${requirement}=." "$file" || {
            echo "refusing: ${file} has no ${requirement}." >&2
            echo "  Every one of these is required, because a half-configured connector is" >&2
            echo "  not a configuration this surface offers:" >&2
            echo "    ${required//$'\n'/ }" >&2
            exit 1; }
    done

    # A hash of the file, stored BESIDE the credential in the same Secret. It is what the
    # pod template's checksum/<label> annotation renders from, and storing it in the Secret
    # (rather than deriving it from the credential, which this script may not read back) is
    # what lets a re-run with no flag produce the SAME annotation and therefore NOT restart
    # a healthy agent. A hash, never the credential.
    CONNECTOR_SUM=$(sha256sum "$file" | cut -c1-16)
    # ONE file, because `kubectl create secret` REFUSES `--from-env-file` together with
    # `--from-literal` ("from-env-file cannot be combined with from-file or from-literal").
    # So the sum is appended to a copy rather than passed as a literal. The copy is created
    # under `umask 077` and removed on every exit path — the same handling the BYO key gets,
    # and for the same reason: for the moments it exists this file holds a live credential.
    #
    # The leading newline before the sum is not cosmetic: a config file saved without a
    # trailing newline would otherwise concatenate its last value with the sum key, and the
    # result is a secret with a mangled setting and no sum at all. kubectl skips the blank
    # line that produces.
    tmp="$(umask 077; mktemp -t "agent-${label}-XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    cat "$file" > "$tmp"
    printf '\n%s=%s\n' "$sum_key" "$CONNECTOR_SUM" >> "$tmp"
    kubectl -n "$NS" create secret generic "$secret" \
        --from-env-file="$tmp" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    rm -f "$tmp"
    trap - EXIT
    printf '    %-8s credential stored in %s (not shown, not readable back)\n' "$label" "$secret"
}

# The allowlists are Hermes's OWN env var names (the retarget), in the SAME order as
# control-plane/app/agents.py CONNECTORS — the two are bound by
# test_the_python_schema_matches_the_shell_provisioners_allowlists. Hermes auto-detects mail
# ports and TLS, so there is no username/port/security key; EMAIL_ALLOW_ALL_USERS opts out of
# deny-by-default.
provision_connector email "--email-config-file" "$EMAIL_CONFIG_FILE" \
    "mail setting" EMAIL_CONFIG_SUM "none — this agent has no mailbox" \
"EMAIL_ADDRESS EMAIL_PASSWORD EMAIL_IMAP_HOST
EMAIL_SMTP_HOST EMAIL_HOME_ADDRESS EMAIL_ALLOW_ALL_USERS" \
    "EMAIL_ADDRESS EMAIL_PASSWORD EMAIL_SMTP_HOST EMAIL_IMAP_HOST"
EMAILSUM="$CONNECTOR_SUM"

# BOTH Slack tokens are required. The bot token (`xoxb-`) posts; the app-level token
# (`xapp-`) is what opens the Socket Mode websocket, and Socket Mode is how the agent
# RECEIVES without anyone publishing an inbound internet route into a pod that holds a
# spendable model key. An agent with only the bot token can talk and can never listen.
# SLACK_ALLOWED_USERS gates who it answers (Hermes denies unknown senders by default).
provision_connector slack "--slack-config-file" "$SLACK_CONFIG_FILE" \
    "Slack setting" SLACK_CONFIG_SUM "none — this agent has no Slack workspace" \
"SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_HOME_CHANNEL
SLACK_ALLOWED_USERS" \
    "SLACK_BOT_TOKEN SLACK_APP_TOKEN"
SLACKSUM="$CONNECTOR_SUM"

# Discord needs ONE token for both directions — the same bot token authenticates the REST
# call that posts and the Gateway websocket that listens. DISCORD_ALLOWED_USERS / _ROLES
# gate who it answers (deny-by-default without them).
provision_connector discord "--discord-config-file" "$DISCORD_CONFIG_FILE" \
    "Discord setting" DISCORD_CONFIG_SUM "none — this agent has no Discord guild" \
"DISCORD_BOT_TOKEN DISCORD_HOME_CHANNEL DISCORD_ALLOWED_USERS
DISCORD_ALLOWED_ROLES" \
    "DISCORD_BOT_TOKEN"
DISCORDSUM="$CONNECTOR_SUM"

# ---------------------------------------------------------------- apply
sed -e "s|__USER__|${USER_NAME}|g" \
    -e "s|__NAME__|${AGENT_NAME}|g" \
    -e "s|__IMAGE__|${IMAGE}|g" \
    -e "s|__MODEL__|${MODEL}|g" \
    -e "s|__CONTEXT_LENGTH__|${CONTEXT_LENGTH}|g" \
    -e "s|__MAX_TOKENS__|${MAX_TOKENS}|g" \
    -e "s|__CFGSUM__|${CFGSUM}|g" \
    -e "s|__KEYSUM__|${KEYSUM}|g" \
    -e "s|__MODEL_SOURCE__|${MODEL_SOURCE}|g" \
    -e "s|__API_BASE__|${API_BASE}|g" \
    -e "s|__KEY_SECRET__|${KEY_SECRET}|g" \
    -e "s|__EMAILSUM__|${EMAILSUM}|g" \
    -e "s|__SLACKSUM__|${SLACKSUM}|g" \
    -e "s|__DISCORDSUM__|${DISCORDSUM}|g" \
    deploy/k8s/64-agent.template.yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" rollout status "deployment/${OBJ}" --timeout=600s

echo
echo "  ${OBJ}: resident. \`hermes gateway run\` holds the session with nothing attached;"
echo "  console in with \`kubectl -n ${NS} exec -it deploy/${OBJ} -c agent -- hermes --tui\`;"
echo "  stop it with \`kubectl -n ${NS} scale deploy/${OBJ} --replicas=0\` (PVC kept)."
if [[ "$MODEL_SOURCE" == "byo" ]]; then
    echo
    echo "  BYO: this agent's inference goes to ${API_BASE} on the user's own credential."
    echo "  It produces NO rows on the gateway ledger — by design, not by accident. The pod"
    echo "  carries agent.enterprise-ai/model-source=byo so every spend view can say so"
    echo "  rather than showing it as \$0. Resident time and compute are still METERED as"
    echo "  usage (hours, CPU-core-hours) — BYO removes the inference row, not the residency row."
fi
