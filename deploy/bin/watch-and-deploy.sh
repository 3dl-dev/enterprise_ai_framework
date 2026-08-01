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
#   * THE CATALOGUE HAS TWO MODES AND ONE FILE (enterpriseaiframework-7bb). The hermetic suite
#     needs a fakes-only bundle/litellm/config.generated.yaml; production needs the real Forge
#     catalogue. FORGE_API_KEY is ambient in the operator shell, so any unguarded render
#     writes the real one, and every later hermetic run then fails EVERY chat turn with
#     "illegal_model_request: fake-large" — after burning a 180s timeout each, turning a 6
#     minute suite into 19. So: render fakes, test, render real, deploy. Verified both times,
#     not assumed.
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
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://gateway.tailcb6ef9.ts.net:8443}"
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
env -u FORGE_API_KEY -u FORGE_ADMIN_KEY bundle/bin/render-gateway-config.py >>"$LOG" 2>&1
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
say "rendering the production catalogue"
bundle/bin/render-gateway-config.py >>"$LOG" 2>&1
real_entries=$(grep -c 'model_name:' bundle/litellm/config.generated.yaml || echo 0)
(( real_entries > 10 )) || give_up "the production render produced only ${real_entries} models; deploying that would replace the real catalogue with fakes"
say "production catalogue: ${real_entries} models"

say "deploying"
if ! PUBLIC_BASE_URL="$PUBLIC_BASE_URL" deploy/bin/deploy.sh >>"$LOG" 2>&1; then
    say "DEPLOY FAILED on ${SHA:0:9} — the cluster may be part-way. Tail of $LOG:"
    tail -40 "$LOG" >&2
    exit 1
fi

# deploy.sh ends in smoke.sh, so reaching here means the cluster served a prompt.
printf '%s\n' "$SHA" > "$STATE"
say "=== deployed ${SHA:0:9} and it serves prompts ==="

# Leave the checkout in the mode a human or a dispatch wave expects to find it.
env -u FORGE_API_KEY -u FORGE_ADMIN_KEY bundle/bin/render-gateway-config.py >>"$LOG" 2>&1
say "restored fakes-only catalogue for local work"
