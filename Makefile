.PHONY: up down logs ps sync spend audit test nuke

BUNDLE := bundle
COMPOSE := docker compose -f $(BUNDLE)/docker-compose.yml --env-file $(BUNDLE)/.env

## Scope item 8: the whole bundle starts from one command on a single host, no GPU.
up:
	@$(BUNDLE)/bin/render-env.sh
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

## Destroy all state including the databases. Not reversible.
nuke:
	@$(COMPOSE) down -v
	@rm -f $(BUNDLE)/keycloak/realm-export.json
