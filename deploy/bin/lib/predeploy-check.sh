#!/usr/bin/env bash
# Pre-deploy safety check (enterpriseaiframework-e7f).
#
# deploy.sh is about to overwrite enterprise-ai-secrets. Before it does, compare every value
# it would write with what the LIVE cluster holds, and refuse if a value the operated
# instance depends on would change or go empty. This exists because a watcher run wrote
# PUBLIC_BASE_URL=https://ai.example.org over https://ai.3dl.one and rolled a control plane
# with no freerouter, from values that lived nowhere in the script.
#
# Two live sources, because the secret alone is not the truth:
#   * the live Secret (every key deploy.sh writes), and
#   * the RUNNING pod's resolved env for the keys pods consume from it. GATEWAY_PROVIDER was
#     'freerouter' in the running pod while the secret held '' (a previous deploy blanked
#     it); the pod would have lost it on its next restart. The pod env is what the instance
#     is actually doing.
#
# Rules per key (values are NEVER printed, only the key name):
#   live non-empty, desired differs  -> REFUSE (changes)
#   live non-empty, desired empty    -> REFUSE (becomes empty)
#   live empty,     desired set      -> allowed, noted (fills a hole)
#   ALLOW_OPERATED_CHANGE=KEY1,KEY2 (or "all") is the explicit override.
#
# Usage: predeploy_check <namespace> <literal>...   where each literal is "KEY=VALUE"
# (a leading --from-literal= is stripped). Returns 0 (ok / first deploy) or 1 (refused, or a
# live read failed for a reason other than "the object does not exist yet").

# app|container|KEY : keys read from a running workload's resolved env.
PREDEPLOY_POD_KEYS=(
    "control-plane|control-plane|GATEWAY_PROVIDER"
    "control-plane|control-plane|PUBLIC_BASE_URL"
    "chat|librechat|OPENID_ISSUER"
)

_predeploy_allowed() {
    local key="$1" x
    local -a a=()
    [[ "${ALLOW_OPERATED_CHANGE:-}" == all ]] && return 0
    IFS=',' read -ra a <<<"${ALLOW_OPERATED_CHANGE:-}"
    for x in "${a[@]}"; do [[ "$x" == "$key" ]] && return 0; done
    return 1
}

predeploy_check() {
    local ns="$1"; shift
    local -a refused=() notes=()
    local filled=0
    local secret_json rc=0
    secret_json=$(kubectl -n "$ns" get secret enterprise-ai-secrets -o json 2>&1) || rc=$?
    if (( rc != 0 )); then
        if grep -qi 'notfound\|not found' <<<"$secret_json"; then
            echo "predeploy: no live enterprise-ai-secrets yet (first deploy); nothing to compare"
            return 0
        fi
        echo "predeploy: REFUSING: cannot read the live secret to compare against: $(head -c 200 <<<"$secret_json")" >&2
        return 1
    fi

    # KEY<TAB>base64(value) lines; values stay base64 until compared, never echoed.
    local live
    live=$(python3 -c '
import json, sys
for k, v in json.loads(sys.stdin.read()).get("data", {}).items():
    print(k + "\t" + v)' <<<"$secret_json") || { echo "predeploy: REFUSING: live secret unparseable" >&2; return 1; }

    local lit key val liveval
    local -A desired=()
    for lit in "$@"; do
        lit="${lit#--from-literal=}"
        key="${lit%%=*}"; val="${lit#*=}"
        desired["$key"]="$val"
        liveval=$(awk -F'\t' -v k="$key" '$1==k{print $2}' <<<"$live" | base64 -d 2>/dev/null || true)
        if [[ -n "$liveval" && "$liveval" != "$val" ]]; then
            if _predeploy_allowed "$key"; then notes+=("$key: change ALLOWED by ALLOW_OPERATED_CHANGE"); continue; fi
            if [[ -z "$val" ]]; then refused+=("$key (live secret is set; deploy would blank it)")
            else refused+=("$key (live secret differs from the declared instance value)"); fi
        elif [[ -z "$liveval" && -n "$val" ]]; then
            case "$key" in PUBLIC_BASE_URL|OPENID_ISSUER|GATEWAY_PROVIDER) notes+=("$key: live secret empty/absent, deploy fills it") ;; *) filled=$((filled+1)) ;; esac
        fi
    done

    local spec app ctr envkey podval
    for spec in "${PREDEPLOY_POD_KEYS[@]}"; do
        IFS='|' read -r app ctr envkey <<<"$spec"
        [[ -v "desired[$envkey]" ]] || continue
        podval=$(kubectl -n "$ns" exec "deploy/$app" -c "$ctr" -- printenv "$envkey" 2>/dev/null) || podval=""
        if [[ -n "$podval" && "$podval" != "${desired[$envkey]}" ]]; then
            _predeploy_allowed "$envkey" && continue
            if [[ -z "${desired[$envkey]}" ]]; then
                refused+=("$envkey (running $app pod has it set; deploy would blank it)")
            else
                refused+=("$envkey (running $app pod differs from the declared instance value)")
            fi
        fi
    done

    local n
    (( filled )) && echo "predeploy: note: $filled other key(s) empty/absent live, deploy fills them"
    for n in "${notes[@]}"; do echo "predeploy: note: $n"; done
    if (( ${#refused[@]} )); then
        echo "predeploy: REFUSING to deploy; these operated values would regress the live instance:" >&2
        for n in "${refused[@]}"; do echo "    - $n" >&2; done
        echo "predeploy: fix the instance source (bundle/.env), or override deliberately with ALLOW_OPERATED_CHANGE=KEY[,KEY...]|all" >&2
        return 1
    fi
    echo "predeploy: ok - no operated value changes or blanks against the live cluster"
}
