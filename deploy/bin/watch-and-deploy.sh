#!/usr/bin/env bash
# Continuous deployment: main goes green, the cluster gets it, nobody asks.
#
#   deploy/bin/watch-and-deploy.sh          # one pass; exits 0 if nothing to do
#   deploy/bin/watch-and-deploy.sh --force  # deploy even if this SHA was already deployed
#
# Intended to run on a timer on the host that can reach the cluster. It is deliberately a
# single pass rather than a daemon: the timer owns the schedule, this owns one decision.
#
# WHY A LOCAL WATCHER AND NOT GITHUB ACTIONS. The cluster is k3s reachable only from this
# host/tailnet, so a hosted runner cannot deploy to it, and the standing constraint is that
# no 3DL-operated service sits in any data path. A GitHub Actions workflow for PR-level CI is
# still worth having — that is the story a customer self-hosting this would use — but it is
# not what makes this cluster current.
#
# THE ORDER OF THE STEPS BELOW IS LOAD-BEARING. Every one of them is a trap that was paid for
# in a real session, and a watcher that gets them wrong deploys on a false green, which is
# strictly worse than not deploying at all:
#
#   * THE GATEWAY CATALOGUE IS FAKES-ONLY, ALWAYS (7bb, e7f). Forge is retired; the real model
#     catalogue is freerouter's and is not rendered into the LiteLLM config. The render uses
#     --no-upstream, which reads no FORGE_* value, so the file the suite tests is the file
#     production deploys (7bb: a real 148-model catalogue made every hermetic chat turn fail
#     on "illegal_model_request: fake-large" after a 180s timeout each). Before deploying,
#     the guard asks LIVE freerouter for its catalogue and refuses a stub-only one.
#   * THE SURFACE GOES STALE (enterpriseaiframework-af5). `make up` does not restart chat when
#     only librechat.yaml changed, because LibreChat parses it once at startup. The suite then
#     tests a pre-checkout config — which can go green over a broken change just as easily as
#     red over a good one.
#   * DISK IS THE BINDING CONSTRAINT (enterpriseaiframework-25f). This host has hit 98-99% three
#     times and it is shared with other projects that write multi-GB images without warning.
#   * DO NOT RUN CONCURRENTLY WITH A DISPATCH WAVE. Agents drive the same compose stack; a
#     suite run against a stack somebody else is recreating produced 15 failures on a commit
#     that was in fact green.
set -euo pipefail

cd "$(dirname "$0")/../.."
REPO="$PWD"

STATE_DIR="${WATCH_STATE_DIR:-$HOME/.local/state/enterprise-ai}"
STATE="$STATE_DIR/last-deployed-sha"
LOCK="$STATE_DIR/watch-and-deploy.lock"
LOG="$STATE_DIR/watch-and-deploy.log"
MIN_FREE_GB="${MIN_FREE_GB:-8}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://ai.example.org}"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

mkdir -p "$STATE_DIR"
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "another run holds the lock; exiting" >&2
    exit 0
fi

say() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }
give_up() { say "STOP: $*"; exit 1; }

say "=== watch-and-deploy starting ==="

# --- 1. is there anything to do -------------------------------------------------------
git fetch --quiet origin main
SHA="$(git rev-parse origin/main)"
LAST="$(cat "$STATE" 2>/dev/null || echo none)"
if [[ "$SHA" == "$LAST" && $FORCE -eq 0 ]]; then
    say "origin/main $SHA is already deployed; nothing to do"
    exit 0
fi
say "candidate ${SHA:0:9} (last deployed: ${LAST:0:9})"

# A dirty tree means a human is mid-something; do not race them.
[[ -z "$(git status --porcelain)" ]] || give_up "working tree is dirty; refusing to deploy from it"

# --- 2. preconditions ------------------------------------------------------------------
free_gb=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
(( free_gb >= MIN_FREE_GB )) || give_up "only ${free_gb}GB free (need ${MIN_FREE_GB}); a run needs room for images and would risk an ENOSPC outage"
say "disk ${free_gb}GB free"

if pgrep -f '\.claude/worktrees/wf_' >/dev/null 2>&1; then
    give_up "a dispatch wave is working the shared stack; a suite run now would produce false verdicts"
fi

git checkout --quiet main
git merge --ff-only --quiet "$SHA"

# --- 3. hermetic suite, on a catalogue we VERIFY is fakes-only --------------------------
say "rendering fakes-only catalogue"
bundle/bin/render-gateway-config.py --no-upstream >>"$LOG" 2>&1
entries=$(grep -c 'model_name:' bundle/litellm/config.generated.yaml || echo 0)
grep -q 'model_name: fake-large' bundle/litellm/config.generated.yaml \
    || give_up "the rendered catalogue has no fake-large (${entries} entries); every chat-turn test would fail on illegal_model_request after a 180s timeout each"
say "catalogue is fakes-only (${entries} entries, fake-large present)"

say "bringing the bundle up"
env -u FORGE_API_KEY -u FORGE_ADMIN_KEY make up >>"$LOG" 2>&1 || give_up "make up failed; see $LOG"
# af5: make up will not restart chat for a librechat.yaml change on its own.
( cd bundle && docker compose -p enterprise-ai up -d --force-recreate chat >>"$LOG" 2>&1 )

say "running the full suite"
if ! env -u FORGE_API_KEY -u FORGE_ADMIN_KEY make test >>"$LOG" 2>&1; then
    say "SUITE RED on ${SHA:0:9} — not deploying. Tail of $LOG:"
    tail -30 "$LOG" >&2
    exit 1
fi
say "suite GREEN on ${SHA:0:9}"

# --- 4. deploy, on the REAL catalogue ---------------------------------------------------
# Forge is retired: production inference is freerouter's catalogue, and the gateway ships the
# fakes-only render already on disk. The guard asks the live freerouter, so a deploy cannot
# proceed into a cluster whose router has lost its providers.
say "checking the production freerouter catalogue"
fr_out=$(deploy/bin/check-freerouter-catalogue.sh 2>&1) || { say "$fr_out"; give_up "production freerouter does not serve a real catalogue; deploying would leave users on stubs"; }
say "production catalogue: $fr_out"

say "deploying"
if ! PUBLIC_BASE_URL="$PUBLIC_BASE_URL" deploy/bin/deploy.sh >>"$LOG" 2>&1; then
    say "DEPLOY FAILED on ${SHA:0:9} — the cluster may be part-way. Tail of $LOG:"
    tail -40 "$LOG" >&2
    exit 1
fi

# deploy.sh ends in smoke.sh, so reaching here means the cluster served a prompt.
printf '%s\n' "$SHA" > "$STATE"
say "=== deployed ${SHA:0:9} and it serves prompts ==="
