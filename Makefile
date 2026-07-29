.PHONY: up down logs ps sync spend audit test test-forge test-e2e test-browser test-workspace forge-config export exit-direct exit nuke

BUNDLE := bundle
COMPOSE := docker compose -f $(BUNDLE)/docker-compose.yml --env-file $(BUNDLE)/.env

## Scope item 8: the whole bundle starts from one command on a single host, no GPU.
## render-env BEFORE make-certs, and the order is load-bearing. make-certs records
## IDP_PUBLIC_HOST into .env, but it only did so when .env already existed — and
## render-env is what creates .env. So the FIRST `make up` in a clean checkout left
## IDP_PUBLIC_HOST unset, KC_HOSTNAME resolved to "https://:8443", and identity
## crash-looped on a URISyntaxException; the second run passed, because by then .env
## existed. Another "failed once, passed on a re-run" (cf. fae71e0). render-env needs
## nothing from make-certs, so the dependency only runs one way.
up:
	@$(BUNDLE)/bin/render-env.sh
	@$(BUNDLE)/bin/make-certs.sh
	@$(BUNDLE)/bin/render-gateway-config.py
	@$(COMPOSE) up -d --build postgres valkey fakeprovider identity gateway control-plane
	@$(BUNDLE)/bin/wait-healthy.sh
	@$(BUNDLE)/bin/provision-chat-key.sh
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

test-workspace:
	@.venv-test/bin/pytest tests-live/test_workspace.py -v --tb=short -p no:cacheprovider
