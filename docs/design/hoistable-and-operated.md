# Hoistable OSS platform + our operated instance (one codebase)

> Status: proposed (2026-09-03). Adopts, for this platform, the architecture the sibling
> `freerouter` project already runs (`freerouter/docs/design/hoistable-and-operated.md`). The
> trigger was a concrete leak: a chat-surface change baked our backend (freerouter host,
> `user_provided`, `zai-org/*` model ids) into the shipped bundle. A grep for operator literals
> then found the leak is pervasive — 20+ files in the distributable carry our domains, LAN IP,
> tailnet name, operator username, and catalog slugs. The distributable is, today, **not
> hoistable**. This document says how we fix that and keep it fixed.

## Why this is the whole point

The thesis of this platform is that others can **run and fork arbitrary numbers of instances,
for arbitrary numbers of users, without inheriting any of our details.** A distributable that
carries `ai.3dl.one`, `192.168.2.42`, `PORTAL_ADMINS: baron`, or a specific model catalogue is
not a product other people can hoist — it is our deployment with the serial numbers still on.
"Open, self-hosted, no tenant lock-in" is falsified the moment a forker has to grep our name out
of the manifests before they can run it.

## The principle

**One codebase. Deployment-identity is CONFIGURATION. Our instance is the OSS release + a
private overlay — never a fork.** This mirrors freerouter exactly, and the mechanism is the
`degrade-clean-when-unset` discipline the code already uses in places:

- **Nothing configured** → a working *personal metered gateway*: SQLite/dev stores, the bundled
  LiteLLM gateway, BYO or no provider keys, a shared chat key, test funds, no payment, one
  local admin. A forker gets this by running the bundle with an empty `.env`.
- **Add provider keys** → a gateway that routes to real models.
- **Add the freerouter backend + per-user keys + open signup + a payment rail** → an *operated
  platform*.
- **Our instance** = that last profile, with our secrets/branding/catalogue/domain in a layer
  the public repo never sees.

## Two hard invariants (enforced every merge)

Same class as freerouter's forge-severed / extraction-clean gates.

1. **The distributable holds ZERO secrets and ZERO "we are the operator."** No committed creds,
   no baked domain/host/IP/tailnet, no hardcoded operator username, economics, or catalogue that
   config cannot override. Enforced by an **oss-clean gate**: a test (and CI/review lint) that
   greps the distributable for a denylist of operator literals and fails the build. See
   *The oss-clean gate* below.
2. **Our instance dogfoods the exact release.** The moment we fork, "when I say it's released it
   works, guaranteed" stops being true. Our operated instance = `<platform> <release>` + our
   private overlay, deployed from the same images the public builds.

## What lives where

| Distributable (`<platform>`, OSS) | Instance overlay (`<platform>-3dl`, private) |
|---|---|
| All code: control-plane, bundle configs, deploy manifests as **parameterised templates** | The **values** for those parameters: domain/TLS, `PUBLIC_BASE_URL`, NodePorts, `hostAliases` IP, tailnet name |
| Operator-agnostic defaults (LiteLLM gateway, shared chat key, alias model ids, `PORTAL_ADMINS` empty→no console) | Our profile selection: `GATEWAY_PROVIDER=freerouter`, `CHAT_ENDPOINT_APIKEY=user_provided`, `CHAT_INFERENCE_BASE`, `GATEWAY_SURFACE_BASE`, `PORTAL_ADMINS=baron` |
| The per-user-key mechanism, freerouter spoke, all surfaces | Our catalogue choices (which models the pickers default to), economics (markup) |
| Generic/example fixtures, clearly labelled | Our branding, skin, and the camp-flavoured fixtures/house-rules (one tenant's config) |
| Zero secrets (gate-enforced) | All secrets (gitignored `.env` / deploy secrets manager, never committed) |
| The image builds (`Dockerfile`s), the app | `docker-compose` / k8s **overlay** wiring the published images + Postgres/Valkey/etc. |

The secret channel already exists and is correct (`bundle/.env` is gitignored; `enterprise-ai-secrets`
is injected, never committed). The gap is the **non-secret operator values** that sit in tracked
files. Those move to the overlay via parameters with agnostic defaults — exactly the pattern the
chat fix used (`CHAT_INFERENCE_BASE`/`CHAT_ENDPOINT_APIKEY` default to the bundled gateway, the
overlay sets freerouter in `bundle/.env`).

## Deployment profiles (the config surface)

A deployment is described by a profile answering: storage backend + DSN; gateway backend
(`litellm` | `freerouter`); provider set (operator-held keys vs BYO-only); chat key model
(shared + forwarded-user vs per-user seeded keys); payment (off vs a processor + account);
self-signup (open/closed); operator economics (markup, root id); admins (`PORTAL_ADMINS`);
public base URL + TLS + network shape; branding/skin; model catalogue defaults. Named,
documented profiles — `personal`, `gateway`, `operated` — each a set of the above, all reached by
setting config, never by editing tracked code.

## The oss-clean gate

A test in the distributable (`tests/test_oss_clean.py`, matching freerouter's approach) that
walks the tracked tree and fails on a denylist of operator literals:

- domains: `3dl.one`, `3dl.dev`, `3dl.network`, `router.3dl`, `ai.3dl`
- network: `192.168.2.` (and any RFC1918 literal outside example ranges), `tailcb6ef9`, specific `nodePort:` values that encode our cluster
- operator identity: `PORTAL_ADMINS: baron`, our operator email, our wallet/keys
- catalogue lock-in: bare `zai-org/…` / vendor slugs as the *only* option (allowed as commented examples, not as hardcoded defaults)

Allowed: these strings in `docs/` prose as examples, and in `*.example` files. The gate is the
enforcement that makes invariant #1 real; without it, the next leak lands the same way this one
did. It runs in `make test` and CI.

## Migration plan (the effort behind enterpriseaiframework-8f7)

1. **Audit** — enumerate every operator literal in the tracked tree (the grep above is the seed;
   ~20 files). One rd item per cluster (domains, network, admins, catalogue, branding).
2. **Parameterise** — for each, add a config knob with an agnostic default and substitute at
   deploy (the chat fix is the template). Prove the default yields a working personal profile
   and our overlay reproduces the deployed prod artifact (as the chat fix did — verified, no
   drift).
3. **Gate** — land `test_oss_clean.py` once the tree is clean; from then on it stays clean.
4. **Stand up `<platform>-3dl`** — a thin overlay repo (README, `.env.example`, the k8s overlay
   or compose wiring the published images, the deploy runbook), consuming the distributable's
   images rather than forking. Model it on `freerouter-3dl`.
5. **Move the values** — our real `bundle/.env`, network/domain overlay, branding/fixtures, and
   catalogue into `<platform>-3dl`. The distributable keeps only agnostic defaults + examples.
6. **Cascade** — this is an architecture change; run the downstream review cascade
   (dap:docs/practice/claude-md/architecture-change-cascade.md).

## The rename — scrubbed (2026-09-03)

A rename was considered alongside this split and **dropped**. The split does not depend on it:
the distributable/instance boundary, the oss-clean gate, and the `<current-name>-3dl` overlay
all work under the current name. Recorded here so it is not reopened without new information:

- The name search hit a wall. The AI-platform namespace is saturated — real words, trendy
  compounds (`*stack`, `*build`, `vibe*`, `frontier*`), and even near-coinages are almost all
  taken on domains, and several collided with **funded competitors or near-clones** (e.g.
  `Tesslate` — a self-hosted full-stack AI dev tool — sits right on our positioning). Chasing a
  clean, rival-free, ownable name in real time was not converging and was not worth the cost.
- If revived later, the useful findings stand: name into the **ownership / actually-free /
  sovereign** lane (which SaaS rivals structurally cannot follow us into — no competitor
  collisions there), expect to solve the domain with a variant rather than a bare `.com`, and
  vet every candidate for a *product/trademark* collision (not just a GitHub handle, which lives
  under the org and does not matter). Coined words are the only lane with open namespace.
- Until then, the current name stays, and nothing in this document is blocked by that.

## What is already done

The chat-surface leak (the trigger) is fixed: the bundle ships agnostic (shared key + LiteLLM +
alias model ids), the freerouter/per-user-key profile is selected in `bundle/.env`, and the
overlay reproduces the deployed prod config with no drift (commit on the `work/chat-per-user-keys`
branch / PR #47). That fix is the worked example the migration above generalises.
