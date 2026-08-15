# arlo distilled runbooks — enterprise_ai_framework

Rung-4 (compose) flows for this project's real multi-step operations. Each atom is a real
card grounded to source; the **order** is inferred (less-trusted) and re-verifiable against
live source. Preconditions marked **[conditional]** may already hold — skip if so.

- Harvested/distilled: **2026-08-15** (agent-tier LOM, lights on).
- Not frozen truth: re-verify each atom against live source on use; if a source moved,
  re-infer and re-distill (re-run `/arlo:start`).

---

## Flow 1 — Bring up the local bundle (single host)

**Goal:** "start the whole platform locally", "bring the stack up".

1. `make up` — the whole flow is one command (scope item 8). It renders env → makes certs →
   renders gateway config → brings up core services → waits healthy → mints chat/rag keys →
   brings up all services → waits healthy → post-up reconcile. — `Makefile:up`

**[conditional]** For the metered Forge path (not the fake provider): `op signin` → `direnv
reload` first so `FORGE_*` are in the shell. Without them the bundle still runs on the fake
provider. — `.envrc:22`, `deploy/README.md:9`

Effective source: the `up` target itself is the process; its ordered recipe governs the
outcome, so grounding is the target, not a doc describing it.

---

## Flow 2 — Deploy to the k3s cluster (cold)

**Goal:** "deploy to the cluster", "ship main to k3s".

1. **[conditional]** `op signin` — restore the 1Password session. — `deploy/README.md:8`
2. **[conditional]** `direnv reload` — populate `FORGE_*` in the shell. — `deploy/README.md:9`
3. `export PUBLIC_BASE_URL=https://ai.3dl.network` — the URL a **browser** will use; must be
   https or OIDC login fails after a clean deploy. — `deploy/README.md:10`, `deploy/bin/deploy.sh:8`
4. `deploy/bin/deploy.sh` — deploy the bundle to k3s (reuses `bundle/.env` as secret source).
   — `deploy/README.md:11`
5. `deploy/bin/post-deploy.sh` — post-deploy reconcile (realm redirect URIs, bootstrap user,
   key sync via Keycloak admin API). — `deploy/README.md:12`
6. `deploy/bin/smoke.sh` — prove the deployment can actually serve a prompt (real credential
   path, live inference). "Pods Running" is not this claim. — `deploy/bin/smoke.sh:2`

**Precondition:** requires a `bundle/.env` (run `make up` locally first) — `deploy.sh` exits
if it is missing. Requires kubectl context to the cluster (assumed by the runbook; a
`kubeconfig`/context step is **not documented** here — honest gap, supply your own context).

Effective source: the deploy README's fenced runbook is the ordered process; each atom is a
real script invocation. `deploy.sh` reads `PUBLIC_BASE_URL` and `bundle/.env` at runtime, so
steps 3–4 are the outcome-deciding path.

---

## Flow 3 — Onboard a user with a workspace (cluster)

**Goal:** "add a user and give them a workspace / IDE".

1. `deploy/bin/ensure-second-user.sh <username>` — create the realm user; password stored only
   in that user's own `workspace-user-<username>` Secret. — `deploy/README.md:110`
2. `deploy/bin/provision-workspace.sh <username>` — provision their browser-terminal workspace
   (idempotent; rotates a fresh `<username>::ide` virtual key). — `deploy/README.md:111`
3. **[optional]** `deploy/bin/provision-workspace.sh <username> --instructions ./house-rules.md`
   — replace the terminal-agent house rules for every workspace, no image rebuild. — `deploy/README.md:129`

**[conditional]** If the workspace image isn't built yet:
`deploy/bin/kaniko-build.sh deploy/workspace 192.168.2.43:30500/enterprise-ai-workspace:$(git rev-parse --short HEAD)`
— build & push the workspace image in-cluster. — `deploy/README.md:109`

To add a **resident agent** for the user instead of / as well as a workspace:
`deploy/bin/provision-agent.sh <username> <agent-name>` (integrated, gateway-metered), or the
one-command chat form `deploy/bin/hermes-up.sh <username> hermes --slack-config-file FILE`.
— `deploy/bin/provision-agent.sh:4`, `deploy/bin/hermes-up.sh:4`

---

## Flow 4 — Leave the platform (exit / anti-lock-in, scope item 9)

**Goal:** "leave", "get you gone", "stop using the gateway".

Non-destructive first (run any time):
1. `make export` — export the ledger and verify it. — `Makefile:export`
2. `make exit-direct` — write the direct-provider configuration for each surface. — `Makefile:exit-direct`

Full destructive leave (surfaces stop working until they hold direct provider keys):
- `make exit` — export → verify → write direct config → **revoke every virtual key**. Runs
  `bundle/bin/exit.sh full --confirm`. — `Makefile:exit`

Effective source: `make exit` wraps `exit.sh full --confirm`; `exit.sh`'s own header documents
the safe order (export → direct → revoke), so the ordering is ground-truth, not inferred.

---

## Flow 5 — Recover / restart

**Goal:** "restart a service", "reset the local stack".

- Local bundle, a single service: `make down` then `make up` (idempotent) — `Makefile:down`, `Makefile:up`.
  (No `docker compose restart <svc>` card exists — the bundle's documented recovery is
  down/up; do not invent a restart verb.)
- Cluster, a resident agent: `kubectl scale deploy/agent-<user>-<name> --replicas=0` then
  `--replicas=1` (PVC retained). — `docs/design/records/agents-surface.md:203`,`:205`
- Cluster, reclaim a workspace: `kubectl -n enterprise-ai delete deploy,svc,pvc,secret -l
  workspace.enterprise-ai/user=<name>`. — `deploy/README.md:172`
- Complete cluster uninstall: `kubectl delete namespace enterprise-ai`. — `deploy/README.md:18`

---

## Flow 6 — Gateway-VM edge (Caddy) change

**Goal:** "change the gateway ingress / Caddyfile".

1. `scp deploy/caddy/Caddyfile baron@gateway:/tmp/Caddyfile.new` — stage. — `deploy/caddy/README.md:42`
2. `ssh baron@gateway 'sudo caddy validate --adapter caddyfile --config /tmp/Caddyfile.new && sudo cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S) && sudo cp /tmp/Caddyfile.new /etc/caddy/Caddyfile && sudo systemctl reload caddy'`
   — validate, back up, install, reload. — `deploy/caddy/README.md:43`

`[owner, gateway VM]` — a multi-host op driven over ssh; run yourself, not via the operator.
