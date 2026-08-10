#!/usr/bin/env bash
# Provision one named, RESIDENT agent instance.
#
#   deploy/bin/provision-agent.sh <keycloak-username> <agent-name> [--model NAME]
#   deploy/bin/provision-agent.sh <user> <name> --byo-key-file FILE --byo-api-base URL
#
# A PARALLEL script to deploy/bin/provision-workspace.sh, not a generalisation of it.
# Contract 6 of docs/design/records/agents-surface.md freezes the Code/workspace surface
# byte-for-byte — the camp runs on it — so this file, deploy/k8s/63-agent-common.yaml,
# deploy/k8s/64-agent.template.yaml and deploy/agent/entrypoint.sh sit BESIDE the frozen
# set and never edit it. tests/test_agents_code_untouched.py makes that mechanical.
#
# What it guarantees, in the order the guarantees matter:
#
#   1. RESIDENCY. The pod's own process is `opencode serve` — a headless daemon that holds
#      a session with NO console attached and keeps holding it across every connect and
#      disconnect. This is the entire difference from Code, where ttyd spawns a fresh
#      opencode per websocket and it dies with the connection (finding 43). An agent that
#      needed a browser open would be a workspace with a different tab.
#   2. Object names are `agent-<user>-<name>` (Contract 1): the PVC, the Deployment, the
#      Service and the Secret. One greppable family, mirroring `ws-<user>`.
#   3. The opencode server is not published. The Service is ClusterIP with no NodePort,
#      the NetworkPolicy admits 4096 only from the control-plane pod, and the daemon
#      itself demands HTTP Basic. The portal decides WHICH agent you reach from your
#      authenticated name (enterpriseaiframework-0e7), so a request cannot name someone
#      else's.
#   4. The pod cannot reach the Kubernetes API, a workspace, another agent, the control
#      plane, Postgres or identity. Its in-cluster egress is an allowlist of NAMED
#      services — kube-dns, the gateway, and the MCP tool servers — never the namespace
#      and never the pod CIDR. Read the NetworkPolicy in 63-agent-common.yaml; do not
#      paraphrase it here, that is how finding 37 happened.
#
# IDEMPOTENT, AND DELIBERATELY NON-DISRUPTIVE. Re-running this for a healthy agent must
# not restart it: restarting an agent ends the resident session that is the whole product.
# So, unlike provision-workspace.sh, this does NOT rotate a credential on every run, and
# the pod template's rollout annotation tracks the entrypoint rather than the key.
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
# because the pod template's rollout annotation tracks the entrypoint rather than the key
# (see 64-agent.template.yaml), the running daemon would keep presenting a credential that
# no longer exists and start 401ing with nothing on screen to say why. Minting happens
# when there is no usable key — first provision, or -055's sentinel.
set -euo pipefail

cd "$(dirname "$0")/../.."

NS=enterprise-ai
REGISTRY="${RAIL_REGISTRY:-192.168.2.43:30500}"
IMAGE_NAME="enterprise-ai-workspace"
# The SAME image the Code surface runs, derived the same way provision-workspace.sh
# derives it — including the WORKSPACE_TAG/WORKSPACE_IMAGE overrides, because the tag that
# is actually deployed on a cluster is frequently not this checkout's HEAD. Reusing the
# artefact rather than building an agent image is how this surface gets a pinned opencode
# without touching one byte of deploy/workspace/.
WORKSPACE_TAG="${WORKSPACE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
IMAGE="${AGENT_IMAGE:-${WORKSPACE_IMAGE:-${REGISTRY}/${IMAGE_NAME}:${WORKSPACE_TAG}}}"

USAGE="usage: provision-agent.sh <keycloak-username> <agent-name> [--model NAME]
                                 [--byo-key-file FILE] [--byo-api-base URL]
                                 [--email-config-file FILE]"
USER_NAME="${1:?${USAGE}}"
AGENT_NAME="${2:?${USAGE}}"
shift 2
MODEL="${AGENT_MODEL:-glm-5.2@deepinfra}"
BYO_KEY_FILE=""
BYO_API_BASE="${AGENT_BYO_API_BASE:-}"
EMAIL_CONFIG_FILE="${AGENT_EMAIL_CONFIG_FILE:-}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)         MODEL="$2"; shift 2 ;;
        # A FILE, never the key itself. `--byo-key sk-...` would put a live provider
        # credential into argv, where every process on the node can read it out of `ps`.
        --byo-key-file)  BYO_KEY_FILE="$2"; shift 2 ;;
        --byo-api-base)  BYO_API_BASE="$2"; shift 2 ;;
        # Same rule, same reason: the mailbox app-password never appears in argv.
        --email-config-file) EMAIL_CONFIG_FILE="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

GATEWAY_BASE="http://gateway:4000/v1"

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

# The resident entrypoint, deployment-wide (one control plane), delivered as a ConfigMap
# because the image is the workspace image and Contract 6 forbids rebuilding it.
#
# It carries THREE files now: the entrypoint, the mail tool it puts on PATH, and the
# instructions file that tells opencode the mail tool exists
# (enterpriseaiframework-a4e). One ConfigMap rather than three because they roll together
# — a new agent-email with an old entrypoint is a tool nothing has put on PATH — and
# because the pod's rollout annotation is a single checksum over all of them.
kubectl -n "$NS" create configmap agent-entrypoint \
    --from-file=entrypoint.sh=deploy/agent/entrypoint.sh \
    --from-file=agent-email=deploy/agent/agent-email \
    --from-file=EMAIL.md=deploy/agent/EMAIL.md \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Over all three, so editing the mail tool actually rolls the agents. It used to hash
# entrypoint.sh alone; a checksum that covers only one of the files in a ConfigMap is a
# rollout trigger that quietly stops firing for the other two.
CFGSUM=$(cat deploy/agent/entrypoint.sh deploy/agent/agent-email deploy/agent/EMAIL.md \
         | sha256sum | cut -c1-16)

# ---------------------------------------------------------------- the pod's secret
# Read what is already there FIRST. Re-provisioning must not roll a credential out from
# under a running console, and must not silently replace a real key with the sentinel.
existing() { existing_in "${OBJ}-key" "$1"; }
existing_in() {
    kubectl -n "$NS" get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null \
        | base64 -d 2>/dev/null || true
}
SERVER_PASSWORD="$(existing OPENCODE_SERVER_PASSWORD)"
if [[ -z "$SERVER_PASSWORD" ]]; then
    SERVER_PASSWORD="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-32)"
fi

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
    # The pod's own Secret still carries the console password, and its OPENAI_API_KEY stays
    # the sentinel — the pod does not read it in this mode, and a real key sitting unused
    # in a Secret is a credential with no owner.
    kubectl -n "$NS" create secret generic "${OBJ}-key" \
        --from-literal=OPENCODE_SERVER_PASSWORD="$SERVER_PASSWORD" \
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
    kubectl -n "$NS" create secret generic "${OBJ}-key" \
        --from-literal=OPENCODE_SERVER_PASSWORD="$SERVER_PASSWORD" \
        --from-literal=OPENAI_API_KEY="$API_KEY" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi

# ---------------------------------------------------------------- the mailbox
# The agent's EMAIL capability (enterpriseaiframework-a4e). An EXTERNAL provider — M365,
# Gmail, or any IMAP+SMTP host — with the tenant's own mailbox and the tenant's own
# credential. THERE IS NO MAIL SERVER IN THIS DEPLOYMENT AND THERE MUST NEVER BE ONE: no
# Maddy, no Stalwart, no Postfix, no mail component in any manifest. That is Baron's
# ruling on -a4e, and tests/test_agent_email.py asserts it against every deploy manifest
# rather than trusting this comment.
#
# The credential is handled EXACTLY like the BYO key above, for the same reason: it is the
# user's own external credential, we cannot revoke it, and it buys real authority — it
# sends mail as a real person at a real company. So it comes from a FILE and never from
# argv, it is written set-once, it is never echoed, and there is no path in this script or
# in the control plane that reads it back out. Re-supplying the file is the only rotation.
#
# The file is `KEY=value` per line, the shape `kubectl create secret --from-env-file`
# takes. A worked example (an M365 mailbox):
#
#     AGENT_EMAIL_ADDRESS=ops-agent@contoso.com
#     AGENT_EMAIL_USERNAME=ops-agent@contoso.com
#     AGENT_EMAIL_PASSWORD=<app password>
#     AGENT_EMAIL_SMTP_HOST=smtp.office365.com
#     AGENT_EMAIL_SMTP_PORT=587
#     AGENT_EMAIL_SMTP_SECURITY=starttls
#     AGENT_EMAIL_IMAP_HOST=outlook.office365.com
#     AGENT_EMAIL_IMAP_PORT=993
#     AGENT_EMAIL_IMAP_SECURITY=ssl
#
# Values are taken literally — kubectl does not strip quotes — so a password wrapped in
# quotes becomes a password WITH quotes, which is a 401 nobody diagnoses.
EMAIL_SECRET="${OBJ}-email"

# Only AGENT_EMAIL_* keys, checked here rather than trusted. The pod injects this Secret
# with `envFrom`, so every key in it becomes an environment variable in a container that
# holds a spendable API key — a file containing `PATH=/tmp/evil` or `LD_PRELOAD=...` would
# be an arbitrary-code-execution channel dressed up as a mail setting. The template's
# explicit `env:` already wins over `envFrom` for OPENAI_API_KEY and
# OPENCODE_SERVER_PASSWORD, but that only defends the two names anyone thought of.
EMAIL_KEYS_ALLOWED="AGENT_EMAIL_ADDRESS AGENT_EMAIL_USERNAME AGENT_EMAIL_PASSWORD
AGENT_EMAIL_SMTP_HOST AGENT_EMAIL_SMTP_PORT AGENT_EMAIL_SMTP_SECURITY
AGENT_EMAIL_IMAP_HOST AGENT_EMAIL_IMAP_PORT AGENT_EMAIL_IMAP_SECURITY
AGENT_EMAIL_CA_FILE"

if [[ -n "$EMAIL_CONFIG_FILE" ]]; then
    [[ -r "$EMAIL_CONFIG_FILE" ]] || {
        echo "cannot read --email-config-file ${EMAIL_CONFIG_FILE}" >&2; exit 1; }

    # Parsed for VALIDATION only, and kubectl reads the file itself — nothing here is
    # passed on. Be precise about what that does and does not mean: `$line` DOES hold the
    # password for one iteration, because a line-oriented parser cannot avoid it. What is
    # guaranteed is that no branch below prints `$line`, that only `$k` (the key name)
    # survives the loop, and that the refusal messages name the KEY and never the value —
    # which is the case that matters, since the malformed-line branch is exactly where
    # somebody has pasted a bare credential in by mistake.
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "${line//[[:space:]]/}" || "${line#"${line%%[![:space:]]*}"}" == \#* ]] && continue
        # CRLF, refused rather than tolerated or silently stripped. kubectl stores values
        # literally, so a file saved on Windows gives every setting a trailing carriage
        # return: the host becomes "smtp.office365.com\r" (DNS failure) and the password
        # becomes "secret\r" (authentication failure). Both present as "email is broken"
        # with nothing pointing at the file. The BYO key path strips \r for the same
        # reason; here the whole file is at stake, so it is a refusal with a diagnosis.
        if [[ "$line" == *$'\r' ]]; then
            echo "refusing: ${EMAIL_CONFIG_FILE} has Windows (CRLF) line endings." >&2
            echo "  Every value would gain a trailing carriage return, which reads as a" >&2
            echo "  wrong host and a wrong password. Convert it: dos2unix, or" >&2
            echo "  \`sed -i 's/\\r\$//' ${EMAIL_CONFIG_FILE}\`." >&2
            exit 1
        fi
        if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
            echo "refusing: ${EMAIL_CONFIG_FILE} has a line that is not KEY=value." >&2
            echo "  (the offending value is not printed, on purpose)" >&2
            exit 1
        fi
        k="${line%%=*}"
        case " ${EMAIL_KEYS_ALLOWED//$'\n'/ } " in
            *" $k "*) ;;
            *)  echo "refusing: '${k}' is not a mail setting." >&2
                echo "  This file becomes the agent pod's environment via envFrom, so an" >&2
                echo "  unexpected key here is an environment variable in a container that" >&2
                echo "  holds a spendable API key. Allowed:" >&2
                echo "    ${EMAIL_KEYS_ALLOWED//$'\n'/ }" >&2
                exit 1 ;;
        esac
    done < "$EMAIL_CONFIG_FILE"

    # Both halves of the mailbox, or neither. A config with SMTP and no IMAP produces an
    # agent that can send and cannot read, which reads as "email is broken" long after the
    # provisioning that caused it.
    for required in AGENT_EMAIL_ADDRESS AGENT_EMAIL_PASSWORD \
                    AGENT_EMAIL_SMTP_HOST AGENT_EMAIL_IMAP_HOST; do
        grep -qE "^[[:space:]]*${required}=." "$EMAIL_CONFIG_FILE" || {
            echo "refusing: ${EMAIL_CONFIG_FILE} has no ${required}." >&2
            echo "  A mailbox needs an address, a password, an SMTP host and an IMAP host;" >&2
            echo "  send-only or read-only is not a configuration this surface offers." >&2
            exit 1; }
    done

    # A hash of the file, stored BESIDE the credential in the same Secret. It is what the
    # pod template's checksum/email annotation renders from, and storing it in the Secret
    # (rather than deriving it from the credential, which this script may not read back)
    # is what lets a re-run with no --email-config-file produce the SAME annotation and
    # therefore NOT restart a healthy agent. A hash, never the credential.
    EMAILSUM=$(sha256sum "$EMAIL_CONFIG_FILE" | cut -c1-16)
    # ONE file, because `kubectl create secret` REFUSES `--from-env-file` together with
    # `--from-literal` ("from-env-file cannot be combined with from-file or
    # from-literal"). So the sum is appended to a copy rather than passed as a literal.
    # The copy is created under `umask 077` and removed on every exit path — the same
    # handling the BYO key above gets, and for the same reason: for the moments it exists
    # this file holds the customer's mail password.
    #
    # The leading newline before the sum is not cosmetic: a config file saved without a
    # trailing newline would otherwise concatenate its last value with the sum key, and
    # the result is a secret with a mangled setting and no sum at all. kubectl skips the
    # blank line that produces.
    EMAIL_TMP="$(umask 077; mktemp -t agent-email-XXXXXX)"
    trap 'rm -f "$EMAIL_TMP"' EXIT
    cat "$EMAIL_CONFIG_FILE" > "$EMAIL_TMP"
    printf '\nAGENT_EMAIL_CONFIG_SUM=%s\n' "$EMAILSUM" >> "$EMAIL_TMP"
    kubectl -n "$NS" create secret generic "$EMAIL_SECRET" \
        --from-env-file="$EMAIL_TMP" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    rm -f "$EMAIL_TMP"
    trap - EXIT
    echo "    email    mailbox credential stored in ${EMAIL_SECRET} (not shown, not readable back)"
else
    # Untouched. A re-provision that says nothing about email must not delete the mailbox
    # of a running agent, and must not roll it either.
    EMAILSUM="$(existing_in "$EMAIL_SECRET" AGENT_EMAIL_CONFIG_SUM)"
    if [[ -n "$EMAILSUM" ]]; then
        echo "    email    ${EMAIL_SECRET} (kept; re-supply --email-config-file to rotate)"
    else
        EMAILSUM="none"
        echo "    email    none — this agent has no mailbox"
    fi
fi

# ---------------------------------------------------------------- apply
sed -e "s|__USER__|${USER_NAME}|g" \
    -e "s|__NAME__|${AGENT_NAME}|g" \
    -e "s|__IMAGE__|${IMAGE}|g" \
    -e "s|__MODEL__|${MODEL}|g" \
    -e "s|__CFGSUM__|${CFGSUM}|g" \
    -e "s|__KEYSUM__|${KEYSUM}|g" \
    -e "s|__MODEL_SOURCE__|${MODEL_SOURCE}|g" \
    -e "s|__API_BASE__|${API_BASE}|g" \
    -e "s|__KEY_SECRET__|${KEY_SECRET}|g" \
    -e "s|__EMAILSUM__|${EMAILSUM}|g" \
    deploy/k8s/64-agent.template.yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" rollout status "deployment/${OBJ}" --timeout=600s

echo
echo "  ${OBJ}: resident. \`opencode serve\` holds the session with nothing attached;"
echo "  stop it with \`kubectl -n ${NS} scale deploy/${OBJ} --replicas=0\` (PVC kept)."
if [[ "$MODEL_SOURCE" == "byo" ]]; then
    echo
    echo "  BYO: this agent's inference goes to ${API_BASE} on the user's own credential."
    echo "  It produces NO rows on the gateway ledger — by design, not by accident. The pod"
    echo "  carries agent.enterprise-ai/model-source=byo so every spend view can say so"
    echo "  rather than showing it as \$0. Resident time and compute are still METERED as"
    echo "  usage (hours, CPU-core-hours) — BYO removes the inference row, not the residency row."
fi
