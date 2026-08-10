"""Run the REAL deploy/bin/provision-agent.sh with kubectl and curl replaced by recorders.

This exists so the integrated/BYO seam can be tested as behaviour rather than as grep. The
script under test is the actual file that runs on the cluster — not a copy, not a parsed
model of it — and every decision it makes is observed the way an operator would observe
it: the manifests it applied, the argv it invoked, and what it printed.

WHAT IS FAKED AND WHAT IS NOT

Faked: `kubectl` and `curl`, i.e. the cluster and the control plane. Both record every
invocation, and `kubectl create secret --dry-run=client -o yaml` renders a real Secret
manifest (base64 and all) so that what the script pipes into `apply` is the same shape a
cluster would receive.

Not faked: the script, its argument parsing, its refusals, its sed rendering of
deploy/k8s/64-agent.template.yaml, and the ordering of what it does. Those are the subject.

The live half — that the minted key actually returns 200 at the gateway and lands a row in
the real ledger, and that a BYO agent lands none — cannot be faked and is not attempted
here; it is tests-live/test_agent_model_api.py against the real cluster.
"""

import base64
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy/bin/provision-agent.sh"

# What the fake control plane hands back when asked to issue a key, and what the fake
# cluster holds in enterprise-ai-secrets. Values are distinctive so their appearance
# anywhere unexpected is unambiguous.
ISSUED_KEY = "sk-issued-virtual-key-0000000000"
ADMIN_TOKEN = "test-admin-token"
MASTER_KEY = "sk-gateway-master-key-999999"

_KUBECTL = r'''#!/usr/bin/env bash
# Recording stand-in for kubectl. Every invocation is appended to $STUB_LOG; anything
# applied from stdin is appended to $STUB_DIR/applied.yaml.
set -u
{ printf 'kubectl'; for a in "$@"; do printf ' %s' "$a"; done; printf '\n'; } >> "$STUB_LOG"

cmd=""
args=("$@")
i=0
while (( i < ${#args[@]} )); do
    case "${args[$i]}" in
        -n|--namespace) i=$((i+2)); continue ;;
        -*) i=$((i+1)); continue ;;
        *) cmd="${args[$i]}"; break ;;
    esac
done

case "$cmd" in
apply)
    # The leading `---` matters: `kubectl create --dry-run -o yaml` emits a bare document,
    # and concatenating two of them without a separator merges them into one mapping —
    # which silently loses the first Secret rather than failing.
    for a in "$@"; do
        [[ "$a" == "-" ]] && { printf '\n---\n' >> "$STUB_DIR/applied.yaml"; \
                               cat >> "$STUB_DIR/applied.yaml"; break; }
    done
    ;;
get)
    # `get secret <name> -o jsonpath={.data.<field>}` — answered from $STUB_DIR/state.
    name=""; field=""
    for ((i=0; i<${#args[@]}; i++)); do
        case "${args[$i]}" in
            secret) name="${args[$((i+1))]:-}" ;;
            jsonpath=*) field="${args[$i]#jsonpath=\{.data.}"; field="${field%\}}" ;;
        esac
    done
    f="$STUB_DIR/state/${name}.${field}"
    [[ -f "$f" ]] && base64 -w0 < "$f"
    ;;
create)
    # `create ... --dry-run=client -o yaml` renders a manifest on stdout; the caller pipes
    # it into `apply -f -`. Rendered for real (base64 values) so the applied document is
    # the same shape the API server would be handed.
    kind=""; name=""
    for ((i=0; i<${#args[@]}; i++)); do
        case "${args[$i]}" in
            secret) kind=Secret; name="${args[$((i+2))]:-}" ;;
            configmap) kind=ConfigMap; name="${args[$((i+1))]:-}" ;;
        esac
    done

    # REAL KUBECTL REFUSES THIS COMBINATION, and this recorder used to accept it. That is
    # not a detail: the provisioner was written to pass `--from-env-file` alongside
    # `--from-literal`, every test here went green, and the command would have failed on
    # the first real cluster with "from-env-file cannot be combined with from-file or
    # from-literal". A recorder that is more permissive than the tool it stands in for
    # does not simplify the test, it deletes it. Verified against kubectl before being
    # written down here.
    have_env_file=0; have_other=0
    for a in "$@"; do
        case "$a" in
            --from-env-file=*) have_env_file=1 ;;
            --from-file=*|--from-literal=*) have_other=1 ;;
        esac
    done
    if (( have_env_file && have_other )); then
        echo "error: from-env-file cannot be combined with from-file or from-literal" >&2
        exit 1
    fi
    echo "apiVersion: v1"
    echo "kind: ${kind}"
    echo "metadata:"
    echo "  name: ${name}"
    echo "data:"
    for a in "$@"; do
        case "$a" in
            --from-literal=*)
                kv="${a#--from-literal=}"; k="${kv%%=*}"; v="${kv#*=}"
                echo "  ${k}: $(printf '%s' "$v" | base64 -w0)"
                ;;
            --from-file=*)
                kv="${a#--from-file=}"; k="${kv%%=*}"; p="${kv#*=}"
                echo "  ${k}: $(base64 -w0 < "$p")"
                ;;
            --from-env-file=*)
                # Real kubectl turns each KEY=value line into a Secret key, taking the
                # value LITERALLY (it strips no quotes) and skipping blanks and comments.
                # Modelled here rather than approximated, because the mail config
                # (enterpriseaiframework-a4e) arrives this way and the test that matters
                # asserts on which keys land in the Secret.
                p="${a#--from-env-file=}"
                while IFS= read -r line || [[ -n "$line" ]]; do
                    [[ -z "${line//[[:space:]]/}" ]] && continue
                    [[ "${line#"${line%%[![:space:]]*}"}" == \#* ]] && continue
                    k="${line%%=*}"; v="${line#*=}"
                    echo "  ${k}: $(printf '%s' "$v" | base64 -w0)"
                done < "$p"
                ;;
        esac
    done
    ;;
port-forward)
    sleep 30 &
    wait $! 2>/dev/null || true
    ;;
*)
    : # rollout status, delete, etc: nothing to do, already recorded
    ;;
esac
exit 0
'''

_CURL = r'''#!/usr/bin/env bash
# Recording stand-in for the control plane. Only the two endpoints the script calls.
set -u
{ printf 'curl'; for a in "$@"; do printf ' %s' "$a"; done; printf '\n'; } >> "$STUB_LOG"
url=""
body=""
prev=""
for a in "$@"; do
    case "$a" in
        http*) url="$a" ;;
    esac
    [[ "$prev" == "-d" ]] && body="$a"
    prev="$a"
done
printf '%s' "$body" >> "$STUB_DIR/curl-bodies.txt"
printf '\n' >> "$STUB_DIR/curl-bodies.txt"
case "$url" in
    */health) echo ok ;;
    */admin/sync) echo '{"principals":1,"keys_minted":0,"keys_revoked":0,"details":[]}' ;;
    */admin/keys/issue)
        python3 - "$body" <<'PY'
import json, os, sys
req = json.loads(sys.argv[1] or "{}")
user, surface = req.get("username", ""), req.get("surface", "")
# The fake control plane applies the real alias grammar rather than echoing whatever it
# was asked for, so a script that requested the wrong surface gets the wrong alias back
# and the script's own assertion is what catches it.
alias = f"{user}::{surface}"
if os.environ.get("HARNESS_BAD_ALIAS"):
    # Fault injection: answer with the LOSING grammar (`<user>::agents::<name>`), which is
    # what a regression in gateway.py's alias code would actually produce. The script's own
    # guard is what must catch it; see tests/test_agent_model_api_config.py.
    alias = f"{user}::agents::{surface.split('/')[-1]}"
print(json.dumps({
    "username": user, "surface": surface,
    "key_alias": alias,
    "key": "ISSUED_KEY_PLACEHOLDER", "max_budget": None, "rotated": False,
}))
PY
        ;;
    *) echo '{}' ;;
esac
exit 0
'''


class Run:
    def __init__(self, proc, stub_dir: Path):
        self.proc = proc
        self.stub_dir = stub_dir

    @property
    def returncode(self) -> int:
        return self.proc.returncode

    @property
    def output(self) -> str:
        """Everything a human or a log collector would see."""
        return self.proc.stdout + self.proc.stderr

    @property
    def calls(self) -> str:
        """Every kubectl/curl invocation, argv included."""
        p = self.stub_dir / "calls.log"
        return p.read_text() if p.exists() else ""

    @property
    def applied(self) -> str:
        """Every manifest the script piped into `kubectl apply -f -`."""
        p = self.stub_dir / "applied.yaml"
        return p.read_text() if p.exists() else ""

    def secret_data(self, name: str, key: str) -> str | None:
        """A value out of an applied Secret, decoded. None if not present."""
        import yaml

        for doc in yaml.safe_load_all(self.applied):
            if not doc or doc.get("kind") != "Secret":
                continue
            if doc.get("metadata", {}).get("name") != name:
                continue
            raw = (doc.get("data") or {}).get(key)
            if raw is not None:
                return base64.b64decode(raw).decode()
        return None

    def deployment(self) -> dict | None:
        import yaml

        for doc in yaml.safe_load_all(self.applied):
            if doc and doc.get("kind") == "Deployment":
                return doc
        return None


def provision(tmp_path: Path, *args: str, existing_key: str | None = None,
              existing_email_sum: str | None = None, env: dict | None = None) -> Run:
    """Run the real provisioner against the recorders.

    `existing_key` seeds what the cluster already holds in `agent-<user>-<name>-key`'s
    OPENAI_API_KEY — the input that decides whether the script mints, keeps, or refuses.

    `existing_email_sum` seeds `agent-<user>-<name>-email`'s AGENT_EMAIL_CONFIG_SUM, i.e.
    an agent that already has a mailbox. That is the input that decides whether a re-run
    with no `--email-config-file` leaves the mailbox alone and renders the SAME rollout
    annotation — the difference between re-provisioning being a no-op and it silently
    restarting a resident agent (enterpriseaiframework-a4e).
    """
    stub_dir = tmp_path / "stubs"
    (stub_dir / "bin").mkdir(parents=True, exist_ok=True)
    (stub_dir / "state").mkdir(parents=True, exist_ok=True)

    (stub_dir / "state" / "enterprise-ai-secrets.CONTROL_PLANE_ADMIN_TOKEN").write_text(ADMIN_TOKEN)
    (stub_dir / "state" / "enterprise-ai-secrets.GATEWAY_MASTER_KEY").write_text(MASTER_KEY)
    # args[0]/args[1] are <user> <name>; the object family is agent-<user>-<name>.
    obj = f"agent-{args[0]}-{args[1]}"
    if existing_key is not None:
        (stub_dir / "state" / f"{obj}-key.OPENAI_API_KEY").write_text(existing_key)
    if existing_email_sum is not None:
        (stub_dir / "state" / f"{obj}-email.AGENT_EMAIL_CONFIG_SUM").write_text(existing_email_sum)

    kubectl = stub_dir / "bin" / "kubectl"
    kubectl.write_text(_KUBECTL)
    kubectl.chmod(0o755)
    curl = stub_dir / "bin" / "curl"
    curl.write_text(_CURL.replace("ISSUED_KEY_PLACEHOLDER", ISSUED_KEY))
    curl.chmod(0o755)

    environ = dict(os.environ)
    environ.update({
        "PATH": f"{stub_dir / 'bin'}:{environ['PATH']}",
        "STUB_DIR": str(stub_dir),
        "STUB_LOG": str(stub_dir / "calls.log"),
        "AGENT_IMAGE": "registry.invalid/enterprise-ai-workspace:test",
    })
    environ.update(env or {})

    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=300, env=environ, cwd=str(REPO),
    )
    return Run(proc, stub_dir)
