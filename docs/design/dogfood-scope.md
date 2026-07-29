# v0.1-dogfood — scope

The founder is the first user. The use case is a Claude-like enterprise UX across three surfaces
behind one login, one bill, and one audit trail.

A strict subset of design §7.2. Each item is a checkable end state, proven by `make test`.

**On who the user is.** The first deployment happens to serve a coding camp, so test fixtures,
screenshots and the workspace agent's house rules read like they were written for a nine-year-old.
That is a *tenant*, not the product. The platform makes no assumption about the person at the
keyboard: a lawyer, a marketer and a camper get the same login, the same gateway, the same ledger
row and the same audit entry, and differ only in the instructions and catalogue their operator
configures. Anywhere a doc, test or item says "student" or "camper", read "user" — where the
distinction actually matters it is called out explicitly.

| # | Outcome | Status |
|---|---|---|
| 1 | A user authenticates once and reaches all three surfaces — web chat, IDE coding agent, terminal coding agent — without a second credential | done |
| 2 | No surface holds a provider API key; each holds a virtual key minted against the operator's own upstream credentials, revocable per user | done |
| 3 | Every request from all three surfaces transits one gateway speaking both OpenAI-compatible and Anthropic-native inbound, streaming without buffering | done |
| 4 | One metering ledger; a single query returns total spend by user and by surface across all three | done |
| 5 | One audit trail, surviving a restart of every component | done |
| 6 | Revoking a user in the identity provider stops their traffic on all three surfaces | done |
| 7 | A per-user budget stop enforced at the gateway — past the limit requests are refused, not merely recorded | done |
| 8 | The bundle starts from one command on a single host with no GPU, with fakes for upstream providers so it is testable with no provider account and no spend | done |
| 9 | A tested exit path: export the ledger, revoke virtual keys, restore direct provider keys, and every surface keeps working with the layer removed | done |

**What "three surfaces" means now.** The control plane mints a key per surface for every user —
`chat`, `ide`, `terminal` (`control-plane/app/gateway.py`). In the deployed product `chat` and `ide`
are the two tabs of the portal; `terminal` is the key a user points their own terminal agent at,
which is why `make test` exercises it over raw Anthropic-native calls rather than through a UI. Three
keys, three metered principals, two tabs. The count is about billable surfaces, not windows.

## What shipped after the nine, and is not described by them

The nine outcomes are green and were never the whole product. What was built on top of them is
below, so that work has a home in this document rather than only in the commit log. These are not
new scope items with pass/fail rows; they are the shape the product now has.

- **One address, two tabs.** `$PUBLIC_BASE_URL/portal/` is the single front door. Chat and Code are
  tabs on one origin, remembering whichever was last used, with spend, keys, published work and the
  account console behind the avatar. The workshop has no URL of its own — the control plane
  authenticates and proxies each user to their own pod. Superseded the earlier design in which each
  surface was its own site behind its own oauth2-proxy.
- **The terminal coding agent is ttyd + opencode, not aider.** See "The coding agent" below.
- **A per-user workspace pod** with projects (plural, each its own git repo), a live preview behind
  a run gate, and `publish` to a share link that serves with no session at all.
- **An operator console** — add a user, set a budget, see spend — so the control plane has a place
  to look that is not `curl`.
- **Browser-level and end-to-end tests.** `make test-browser` drives a real Chromium and fails on
  any console error; `make test-e2e` does the whole journey with a real account and real money.
  Finding 33 records why: every suite before them was HTTP-level and proved a file was served, not
  that its JavaScript ran.

## The coding agent

The default terminal agent is **opencode (MIT), pinned**, served over `ttyd` (MIT). **aider
(Apache-2.0) stays installed as a fallback** and is one word away (`WS_AGENT=aider`, or type it).

aider was the original choice and lost on behaviour, not licence: it requires the user to nominate
files before it will work on them, which is the wrong shape for someone who does not yet know what
the files are. Finding 23 also measured it silently discarding a completed edit when the model
mentioned a file that was not in the chat. opencode explores the repo itself — the Claude Code
shape. What aider keeps is a known-good fallback measured against this exact gateway, which costs
one binary in the image.

Neither is Cline, which the brief and design §3.6 named as the default. That ruling and its
reasoning are recorded in design §3.6; this is the deployment consequence of it.

## Out of scope

The closed-loop router and its four actuation axes; promotion gates and paired shadow evaluation;
semantic cache; provider-invoice reconciliation; the capture ledger and paired-reference storage;
the Perses console and the 3am kit; breakglass; multi-replica availability; egress control; the
conformance harness suite. Plus everything already deferred by §7.2 — GPU serving, the compute
contract, the trainer, the eval harness, the factory loop.

## Running it

Two deployments, both real. The **compose bundle** is scope item 8 — one command, one host, no
GPU, no provider account — and is what `make test` proves. The **k3s cluster** under `deploy/` is
where the product is actually dogfooded, and is the only place the portal, the workspace pods and
the published-work path exist. They drifted once (rd `634`: the bundle was exercising a surface
configuration the cluster had not used for weeks), so treat a claim proven only on one of them as
proven only there.

```
make up      # one command, no GPU, no provider account
make test    # prove the nine items above
make spend   # the one bill, by user and surface
make audit   # verify the audit hash chain
make sync    # reconcile identity -> virtual keys (idempotent)

make forge-config # regenerate the gateway catalogue from Forge's live catalog
make test-forge   # live smoke tests against Forge (spends real money)

make export       # export the ledger and verify it (non-destructive)
make exit-direct  # write the direct-provider config for each surface
make exit         # leave: export, verify, direct config, revoke every key
```

Against the cluster — each needs it deployed, and each spends a fraction of a cent:

```
make test-browser    # drives both UIs in a real Chromium, fails on any console error
make test-workspace  # the workspace pods, two real users, isolation included
make test-e2e        # the whole journey: login, agent, run, publish, fetch with no session
```

Deployment itself is `deploy/bin/` — `deploy.sh`, `setup-portal.sh`, `kaniko-build.sh`,
`provision-workspace.sh`. See `deploy/README.md`, which is the operator-facing account of what is
running and what is known broken.

## Upstreams

The default bundle runs entirely against the fake provider, so the nine items above are
provable with no provider account and no spend. That property is load-bearing and
`make test` must never depend on a real upstream.

Real inference goes through **Forge** (`https://forge.3dl.dev`), not retail provider APIs.
`bin/render-gateway-config.py` generates the gateway catalogue from Forge's live catalog
joined to its rate card, and **refuses to emit a model it cannot price** — an unpriced
model meters at $0, so budgets never trip and the bill under-reports with no error
anywhere. Today that yields 8 usable models out of 68; see finding 10.

Live smoke tests are `make test-forge`, kept separate from `make test`. They reconcile our
computed cost against Forge's own usage record, which currently agrees to the cent.

## Leaving

The exit path is the anti-lock-in mechanism, and it answers the question a licence cannot.
`make export` writes `spend.csv`, `audit.jsonl` and `keys.csv` with a manifest, then
verifies the archive. `bundle/bin/verify-export.py` re-verifies it later using nothing but
the Python standard library and no running service — an archive that needs the vendor's
software to trust is not an exit. It detects both tampering and truncation, and a test
asserts its digest still agrees with the control plane's, so the two cannot drift apart.
