.PHONY: up down logs ps sync spend audit test test-forge test-e2e test-browser test-workspace forge-config export exit-direct exit nuke

BUNDLE := bundle
COMPOSE := docker compose -f $(BUNDLE)/docker-compose.yml --env-file $(BUNDLE)/.env

## Scope item 8: the whole bundle starts from one command on a single host, no GPU.
##
## render-env.sh before make-certs.sh, not the other way round: make-certs.sh only
## records IDP_PUBLIC_HOST into a .env that already exists (`elif [[ -f .env ]]`), and on
## a genuinely fresh worktree with no bundle/.env yet, running it first meant neither
## branch fired — IDP_PUBLIC_HOST was silently never written, and identity came up with
## a malformed KC_HOSTNAME (`https:` with nothing after it) and stayed unhealthy.
## render-env.sh's job is exactly "create .env from .env.example if it does not exist,
## fill gaps otherwise" and has no dependency on certs existing first, so running it
## first makes the later grep/sed in make-certs.sh see a real file every time, first run
## included. Found running `make up` in a brand-new worktree (enterpriseaiframework-cbf):
## an untested combination, since the primary checkout and every other worktree so far
## already had a bundle/.env before this ordering could matter.
up:
	@$(BUNDLE)/bin/render-env.sh
	@$(BUNDLE)/bin/make-certs.sh
	@$(BUNDLE)/bin/render-gateway-config.py
	@$(COMPOSE) up -d --build postgres valkey fakeprovider identity gateway control-plane ragvector
	@$(BUNDLE)/bin/wait-healthy.sh
	@$(BUNDLE)/bin/provision-chat-key.sh
	@$(BUNDLE)/bin/provision-rag-key.sh
	@$(COMPOSE) up -d --build
	@$(BUNDLE)/bin/wait-healthy.sh
	@$(BUNDLE)/bin/post-up.sh

down:
	@$(COMPOSE) down

logs:
	@$(COMPOSE) logs -f --tail=100

ps:
	@$(COMPOSE) ps

## Reconcile identity into virtual keys. Idempotent.
sync:
	@$(BUNDLE)/bin/api.sh POST /admin/sync

## The one bill.
spend:
	@$(BUNDLE)/bin/api.sh GET /admin/spend

## The one audit trail, plus a chain verification.
audit:
	@$(BUNDLE)/bin/api.sh GET /admin/audit/verify

test:
	@$(BUNDLE)/bin/run-tests.sh

## Export the ledger and verify it. Non-destructive — run it whenever.
export:
	@$(BUNDLE)/bin/exit.sh export

## Write the direct-provider configuration for each surface.
exit-direct:
	@$(BUNDLE)/bin/exit.sh direct

## Leave: export, verify, write direct config, then revoke every virtual key.
## Destructive — surfaces stop working until they hold direct provider keys.
exit:
	@$(BUNDLE)/bin/exit.sh full --confirm

## Destroy all state including the databases. Not reversible.
nuke:
	@$(COMPOSE) down -v
	@rm -f $(BUNDLE)/keycloak/realm-export.json

## Live smoke tests against Forge. Spends real money (fractions of a cent).
## Kept out of `make test` so the nine items stay provable with no provider account.
test-forge:
	@.venv-test/bin/pytest tests-live/ -v --tb=short -p no:cacheprovider

## Reload Forge credentials from 1Password and regenerate the gateway catalog.
forge-config:
	@$(BUNDLE)/bin/render-gateway-config.py

## Drive both UIs in a real Chromium against the live cluster: signs in, asserts on the
## rendered DOM, fails on any console error, and reads the terminal's xterm buffer to
## prove the agent actually booted. HTTP-level tests cannot see any of that — they prove
## a file was served, not that its JavaScript runs.
## Screenshots land in $$BROWSER_SHOT_DIR (default /tmp/eai-shots).

## The IDE surface (a browser terminal running opencode) against the live k3s cluster, with two real
## Keycloak users. Needs the workspaces provisioned first:
##   deploy/bin/ensure-second-user.sh student
##   deploy/bin/provision-workspace.sh baron && deploy/bin/provision-workspace.sh student
## Spends a fraction of a cent per run. Kept out of `make test` — it needs a cluster.
## The whole journey in a real browser with a real account and real money: one login,
## chat signed in, the agent typed at until it writes a file, the run gate, running it,
## publishing, and fetching that link with NO session at all. Slow; waits on a model.
test-e2e:
	@.venv-test/bin/pytest tests-live/test_e2e_journey.py -v --tb=short -p no:cacheprovider

test-browser:
	@.venv-test/bin/pytest tests-live/test_browser.py -v --tb=short -p no:cacheprovider

## enterpriseaiframework-40f: signs in to CHAT (not the portal) from a fresh browser context
## against the cluster's real public origin, then reloads and asserts the session survives —
## the cookie-policy defect is invisible to anything that only inspects response headers.
test-chat-login:
	@.venv-test/bin/pytest tests-live/test_chat_login.py -v --tb=short -p no:cacheprovider

test-workspace:
	@.venv-test/bin/pytest tests-live/test_workspace.py -v --tb=short -p no:cacheprovider
