#!/usr/bin/env bash
# The production catalogue guard, in freerouter terms (enterpriseaiframework-e7f).
#
# Forge is retired. The operated instance's real model catalogue is what production
# freerouter serves on GET /v1/models; the LiteLLM gateway is deployed fakes-only. So the
# question "would this deploy leave users with only stubs?" is answered by asking freerouter.
#
#   check-freerouter-catalogue.sh                 # asks the live cluster (kubectl exec control-plane)
#   check-freerouter-catalogue.sh --from-file F   # F = a saved /v1/models JSON body
#
# Passes only if freerouter advertises MORE than MIN_REAL_MODELS (default 10) models that are
# not fakes (ids beginning `fake`, or `<vendor>/fake-*`). Exit 1 with the count otherwise.
set -euo pipefail
NS="${NS:-enterprise-ai}"
FR_BASE="${FREEROUTER_URL:-http://freerouter:8080}"
MIN="${MIN_REAL_MODELS:-10}"

if [[ "${1:-}" == "--from-file" ]]; then
    body="$(cat "${2:?--from-file needs a path}")"
else
    cp=$(kubectl -n "$NS" get pods -l app=control-plane -o jsonpath='{.items[0].metadata.name}')
    body="$(kubectl -n "$NS" exec "$cp" -c control-plane -- python3 -c "
import urllib.request
print(urllib.request.urlopen('$FR_BASE/v1/models',timeout=15).read().decode())")"
fi

BODY="$body" MIN="$MIN" python3 - <<'PY'
import json, os, re, sys
try:
    ids = [m.get("id", "") for m in json.loads(os.environ["BODY"]).get("data", [])]
except Exception as e:
    print(f"freerouter catalogue unreadable: {e}", file=sys.stderr)
    sys.exit(1)
real = [i for i in ids if i and not re.match(r"^(fake|[^/]+/fake)", i)]
n, need = len(real), int(os.environ["MIN"])
print(f"freerouter serves {len(ids)} models, {n} non-fake")
if n <= need:
    print(f"REFUSING: {n} real models (need > {need}); deploying would leave users on stubs", file=sys.stderr)
    sys.exit(1)
PY
