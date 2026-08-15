# arlo coverage self-assessment — enterprise_ai_framework

A per-project self-report, regenerated each `/arlo:start`. Not a shipped grader — an honest
account of what this arlo corpus covers and where it abstains. Stamped **2026-08-15**.

## What it covers (rung earned)

| operator need | rung | grounded to |
|---|---|---|
| Local bundle up/down/logs/ps/test | 0 | `Makefile`, `bundle/bin/*` |
| The one bill / audit / identity sync | 0 | `Makefile:spend,audit,sync` → `api.sh` |
| Exit / anti-lock-in (export, direct, revoke, full) | 0–1 | `Makefile`, `bundle/bin/exit.sh` |
| Deploy to k3s + post-deploy + smoke | 4 | `deploy/bin/*`, `deploy/README.md` |
| Onboard a user (realm user + workspace + agent) | 1–4 | `deploy/bin/*`, `deploy/README.md` |
| Provision workspace / agent / hermes (args) | 1 | `deploy/bin/*` headers |
| Build a cluster image (kaniko) | 1 | `deploy/bin/kaniko-build.sh` |
| Cluster ops: scale agent, reclaim workspace, uninstall | 1 | `deploy/README.md`, `agents-surface.md` |
| Preconditions: 1Password / direnv restore | 0 | `.envrc`, `deploy/README.md` |
| Gateway-VM Caddy edge change | 4 | `deploy/caddy/README.md` |

**58 cards**, all with `command` verbatim in the cited source. 6 distilled runbooks in
`RUNBOOKS.md`.

## What it abstains on (honest gaps — not invented)

- **kubectl context / kubeconfig for a cold operator.** The deploy runbook assumes a working
  cluster context; no repo source documents how to obtain it (`get-credentials`/context switch).
  Flagged in `RUNBOOKS.md` Flow 2, not filled.
- **Per-service `docker compose restart`.** The bundle documents recovery as `make down` +
  `make up`; there is no restart-a-single-service card because no source documents one. arlo
  will not invent `docker compose restart <svc>`.
- **`render-codeapi-keys.py`** is grounded only by its own header — not referenced in the
  Makefile or any runbook. Carded, but low-confidence on when to run it.
- **No helm, no az.** Confirmed absent from all scripts and runbooks (helm appears once as a
  path-string comment; Azure DNS is prose only). arlo will not surface either.

## Known ranking limitation (the model-free floor)

With no local model on this host, the default LOM is the deterministic lexical floor, labeled
`[lexical match, no model]`. Measured in situ: it resolves most intents correctly but, as a
bag-of-words ranker, it **cannot distinguish create from delete** on close wording — e.g.
"make a new workspace for a user" surfaces the *delete* card first (its purpose leads with
`DELETE / reclaim / tear down` so the wrong-real is visible, and `provision-workspace` shows as
an alternative). This resolves correctly under the model-tier LOM (an agent running the skill,
or a provisioned embedder — see `config.json` ladder). The invariant holds regardless: arlo
never emits a command that is not real ground truth; at worst it surfaces a labeled wrong-real.

## Staleness

Re-run `/arlo:start` when: `Makefile`/`bundle/bin`/`deploy/bin` change, `deploy/README.md` or
`deploy/caddy/README.md` runbooks change, or the registry IP / `PUBLIC_BASE_URL` examples move.
Each atom is re-groundable against live source; a moved source rots that atom — re-infer.
