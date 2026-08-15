# arlo lights-out runbook — enterprise_ai_framework

**The frontier is down and you still have to operate. You do not need it.**
Every command below is real — harvested from this system's own ground truth,
never invented. Find what you need and run it.

## Find a command with no agent and no network

- **Search this file** for what you want to do (a service name, "restart", "logs").
- **Ask by intent** with the local core (no frontier):

      python3 -m arlo.translate .arlo/cards.json "bounce the deriver"

  It returns the closest real command, its confidence, and the alternatives, or
  says "no confident match" rather than guess. (Ranking uses a small local model
  if one is provisioned; otherwise search the list below.)

## Every real command, by source

### .envrc

- `direnv allow` — PRECONDITION: allow this repo's .envrc once so direnv loads the Forge env references.
- `op signin` — PRECONDITION: restore the 1Password session so Forge credentials load. Run this first on a cold s...

### Makefile

- `make audit` — The one audit trail, plus a chain verification. (GET /admin/audit/verify)
- `make down` — Stop the bundle stack (docker compose down). State volumes are retained.
- `make exit` — Leave the platform: export, verify, write direct config, then revoke every virtual key. DESTRUCTI...
- `make exit-direct` — Write the direct-provider configuration for each surface (so surfaces can run on direct provider ...
- `make export` — Export the ledger and verify it. Non-destructive — run it whenever.
- `make forge-config` — Reload Forge credentials from 1Password and regenerate the gateway model catalog.
- `make logs` — Follow the bundle's logs (docker compose logs -f --tail=100).
- `make nuke` — Destroy all state including the databases. NOT reversible (docker compose down -v + removes realm...
- `make ps` — List the bundle's running services and their health (docker compose ps).
- `make spend` — The one bill. Show spend across every provider. (GET /admin/spend)
- `make sync` — Reconcile identity into virtual keys. Idempotent. (POST /admin/sync)
- `make test` — Run the scope-item test suite against the running bundle. These are the adjudication evidence for...
- `make test-browser` — Drive both UIs in a real Chromium against the live cluster; fails on any console error.
- `make test-chat-login` — Sign in to CHAT (not the portal) from a fresh browser context against the real public origin, rel...
- `make test-e2e` — Full browser journey against the live k3s cluster: login, chat, agent writes a file, run gate, pu...
- `make test-first-conversation` — A never-signed-in Keycloak account through the documented front door: a real assistant reply grou...
- `make test-forge` — Live smoke tests against Forge. Spends real money (fractions of a cent). Kept out of `make test`.
- `make test-workspace` — Drive both browser-terminal workspaces as a person would, against the live cluster. Needs workspa...
- `make up` — Start / bring up / launch the whole bundle stack from one command on a single host, no GPU (scope...

### bundle/bin/api.sh

- `bundle/bin/api.sh METHOD PATH [json]` — Thin wrapper for calling the control-plane admin API from the host. e.g. api.sh GET /admin/spend ...

### bundle/bin/ensure-user.sh

- `bundle/bin/ensure-user.sh <username> [password] [email]` — Create or update a realm user in the local bundle via the admin API. Idempotent.

### bundle/bin/exit.sh

- `bundle/bin/exit.sh direct` — Write the direct-provider configuration for each surface.
- `bundle/bin/exit.sh export` — Export the ledger and verify it (safe, non-destructive).
- `bundle/bin/exit.sh full --confirm` — The full exit: all three (export, direct, revoke) in the order that keeps surfaces working. DESTR...
- `bundle/bin/exit.sh revoke --confirm` — Revoke every virtual key at the gateway. DESTRUCTIVE — requires --confirm.

### bundle/bin/make-certs.sh

- `bundle/bin/make-certs.sh` — Generate a self-signed TLS certificate for the identity provider. Required, not cosmetic: the cha...

### bundle/bin/post-up.sh

- `bundle/bin/post-up.sh` — First-run reconcile, then print where everything is. Called automatically by `make up`.

### bundle/bin/provision-chat-key.sh

- `bundle/bin/provision-chat-key.sh` — Mint the chat surface's virtual key and write it into .env. Idempotent.

### bundle/bin/provision-rag-key.sh

- `bundle/bin/provision-rag-key.sh` — Mint rag-api's virtual key and write it into .env. Idempotent.

### bundle/bin/render-codeapi-keys.py

- `bundle/bin/render-codeapi-keys.py` — Generate/fill the codeapi Ed25519 keypairs that .env needs, in place.

### bundle/bin/render-env.sh

- `bundle/bin/render-env.sh` — Ensure .env exists and every required secret has a value, then render config templates that need ...

### bundle/bin/render-gateway-config.py

- `bundle/bin/render-gateway-config.py` — Render the gateway model catalog from Forge's live catalog and price list into litellm/config.gen...

### bundle/bin/run-tests.sh

- `bundle/bin/run-tests.sh` — Run the scope-item test suite against the running bundle (creates .venv-test on first use). Same ...

### bundle/bin/verify-export.py

- `bundle/bin/verify-export.py <export-dir>` — Verify an exported ledger. Standalone: stdlib only, talks to no running service.

### bundle/bin/wait-healthy.sh

- `bundle/bin/wait-healthy.sh` — Block until every service with a healthcheck reports healthy, or fail loudly.

### deploy/README.md

- `direnv reload` — PRECONDITION: reload the repo env after `op signin` so the FORGE_* vars populate in the shell.
- `kubectl -n enterprise-ai delete deploy,svc,pvc,secret -l workspace.enterprise-ai/user=<name>` — DELETE / reclaim / tear down one user's existing workspace objects on the cluster (deploy, svc, p...
- `kubectl -n enterprise-ai port-forward svc/control-plane 8081:8000` — Reach the unpublished control-plane admin API from your machine (then call /admin/spend, /admin/s...
- `kubectl delete namespace enterprise-ai` — Complete uninstall of the cluster deployment: delete the whole enterprise-ai namespace.

### deploy/bin/deploy.sh

- `PUBLIC_BASE_URL=https://ai.3dl.network deploy/bin/deploy.sh` — Deploy the bundle to the k3s cluster. Reuses bundle/.env as the source of secrets so cluster and ...

### deploy/bin/ensure-second-user.sh

- `deploy/bin/ensure-second-user.sh [username] [email]` — Create a realm user on the cluster so multi-user isolation can be demonstrated. Password stored o...

### deploy/bin/hermes-up.sh

- `deploy/bin/hermes-up.sh <keycloak-username> <agent-name> [--chat slack|discord] [--slack-config-file FILE] [--discord-config-file FILE] [--model NAME]` — ONE COMMAND: a resident agent, on the metered Forge path, talking in the company's chat (Slack or...
- `deploy/bin/hermes-up.sh baron hermes --slack-config-file ~/.secrets/hermes-slack.env` — Worked example: bring up a Slack-connected resident agent 'hermes' for user baron.

### deploy/bin/kaniko-build.sh

- `deploy/bin/kaniko-build.sh <context-dir> <image-ref> [--build-arg K=V ...]` — Build an image in the cluster with kaniko and push it to the rail registry (no Docker socket; con...

### deploy/bin/post-deploy.sh

- `PUBLIC_BASE_URL=https://host deploy/bin/post-deploy.sh` — Post-deploy reconcile for the cluster — the k8s equivalent of post-up.sh (realm redirect URIs, bo...

### deploy/bin/provision-agent.sh

- `deploy/bin/provision-agent.sh <keycloak-username> <agent-name> [--model NAME]` — Provision one named RESIDENT agent instance (INTEGRATED mode — gateway-metered per-agent virtual ...
- `deploy/bin/provision-agent.sh <user> <name> --byo-key-file FILE --byo-api-base URL` — Provision a resident agent in BYO mode: the user's own provider credential, no gateway ledger row.

### deploy/bin/provision-workspace.sh

- `deploy/bin/provision-workspace.sh <keycloak-username> [--model NAME] [--instructions FILE]` — Create / make / set up / onboard one user's new browser-terminal (IDE) workspace on the cluster. ...

### deploy/bin/setup-portal.sh

- `deploy/bin/setup-portal.sh` — Register the portal's Keycloak client and put its secrets where the pod can read them. Idempotent...

### deploy/bin/smoke.sh

- `deploy/bin/smoke.sh` — Prove the deployment can actually serve a prompt (exercises the real credential path with a live ...

### deploy/bin/watch-and-deploy.sh

- `deploy/bin/watch-and-deploy.sh` — Continuous deployment: one pass; deploys the cluster if main went green, exits 0 if nothing to do...
- `deploy/bin/watch-and-deploy.sh --force` — Force a cluster deploy even if this SHA was already deployed.

### deploy/caddy/README.md

- `scp deploy/caddy/Caddyfile baron@gateway:/tmp/Caddyfile.new` — Stage a gateway-VM Caddy edge-config change: copy the repo Caddyfile to the gateway host.
- `ssh baron@gateway 'sudo caddy validate --adapter caddyfile --config /tmp/Caddyfile.new && sudo cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S) && sudo cp /tmp/Caddyfile.new /etc/caddy/Caddyfile && sudo systemctl reload caddy'` — Validate, back up, install and reload the gateway VM's Caddy config over ssh (edge/ingress change).

### docs/design/records/agents-surface.md

- `kubectl scale deploy/agent-<user>-<name> --replicas=0` — Stop a resident agent (scale to zero); its PVC is retained so it can resume.
- `kubectl scale deploy/agent-<user>-<name> --replicas=1` — Start / resume a resident agent (scale back to one replica).

---
Regenerate after the system changes: re-run `/arlo:start`, or the auto-refresh
hook keeps this current. This file is safe to read when nothing else works.
