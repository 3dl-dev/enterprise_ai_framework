.PHONY: up down logs ps sync spend audit test test-forge test-e2e test-browser test-browser-pod test-workspace test-workspace-isolation forge-config export exit-direct exit nuke

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

## ---------------------------------------------------------------------------------------
## THE LIVE SUITES, AND WHICH OF THEM YOU CAN RUN WHILE SOMEBODY IS WORKING
##
## These used to sign in as `student` out of secret/workspace-user-student — a person — and
## the Code tab drives that account's OWN pod: switching to it starts a session, rewrites
## .meta/<project>.session and clears .new-session. A measured run changed both. So none of
## these could be run while anybody was signed in, which in practice meant they could not be
## run at all (enterpriseaiframework-cf5).
##
## `make test-browser` no longer needs an account, a cluster, or anybody signed out. It
## hosts the portal, the shipped settings handlers, the workshop proxy and the real
## shell-server on loopback (tests-live/portal_harness.py) and asserts HARDER than the live
## version did, because a fixture can contain another account's spend row and a real
## deployment's data cannot.
##
## What genuinely needs a real pod, a real model or a real Keycloak is marked and gated. The
## gate is not advice: tests-live/conftest.py removes those tests at COLLECTION unless an
## account is named, and live_identity.py refuses to hand a credential to a test that is not
## marked. Both halves, because either alone leaves a hole.
##
## TO RUN THE GATED ONES you need a throwaway account — never a person's:
##   deploy/bin/ensure-second-user.sh --throwaway eaibot
##   deploy/bin/ensure-second-user.sh --throwaway eaibot2      # the isolation pair
##   deploy/bin/provision-workspace.sh eaibot
##   deploy/bin/provision-workspace.sh eaibot2
## then EAI_LIVE_TEST_USER=eaibot EAI_LIVE_TEST_USER_2=eaibot2 make test-browser-pod
## Those provisioning commands write to the cluster; run them in a maintenance window.
##
## A gated run with nothing selected exits non-zero (pytest's 5 = nothing collected). That is
## deliberate — a suite that held everything back must not print a green line.
## ---------------------------------------------------------------------------------------

## The UIs in a real Chromium. HERMETIC: no cluster, no account, no credential, safe at any
## hour. Asserts on the rendered DOM and fails on any console error, because a page that
## throws on load still looks fine to curl.
## Screenshots land in $$BROWSER_SHOT_DIR (default /tmp/eai-shots).
test-browser:
	@.venv-test/bin/pytest tests-live/test_browser.py -v --tb=short -p no:cacheprovider

## The two browser tests that cannot be hosted: they need ttyd's own xterm.js reporting what
## it fitted to, and a real opencode resolving a real model. Drives the throwaway account's
## pod — see the header above.
test-browser-pod:
	@.venv-test/bin/pytest tests-live/test_browser.py -v --tb=short -p no:cacheprovider \
		-m needs_real_user

## The whole journey in a real browser with a real account and real money: one login,
## chat signed in, the agent typed at until it writes a file, the run gate, running it,
## publishing, and fetching that link with NO session at all. Slow; waits on a model.
## Every step drives the account's pod, so the whole module is gated.
test-e2e:
	@.venv-test/bin/pytest tests-live/test_e2e_journey.py -v --tb=short -p no:cacheprovider

## The IDE surface (a browser terminal running opencode) against the live k3s cluster, with
## TWO throwaway Keycloak users — one proves nothing about isolation. It types into both
## shells, runs `make test` there and lets aider rewrite a file, so both accounts must be
## throwaway and both must be named. Spends a fraction of a cent per run.
test-workspace:
	@.venv-test/bin/pytest tests-live/test_workspace.py -v --tb=short -p no:cacheprovider

## Only the portal may reach a workspace. The NodePort claim in here needs no account and
## runs ungated; the pod-to-pod probes need the throwaway pair.
test-workspace-isolation:
	@.venv-test/bin/pytest tests-live/test_workspace_isolation.py -v --tb=short \
		-p no:cacheprovider
