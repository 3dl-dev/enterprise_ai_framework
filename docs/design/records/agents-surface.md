# Design record — the resident "Agents" surface

**Status:** design record for epic `enterpriseaiframework-da7`. Normative for the six
contracts below; two rulings inside them were RESERVED to Baron and marked as such — the
email default (Contract 5) is still open, and the resident-metering cost basis (Contract 3b)
was RULED in `-914`: **meter usage, not cost. Do not reintroduce a rate.**
**Referenced by:** `docs/design/design.md` §12 (the source-of-truth section that points
here and states the binding contracts).
**Consumed by:** `enterpriseaiframework-055` (resident pod + parallel provisioner), `-627`
(Agents tab: create/status/stop/delete), `-0e7` (console attach, owner-scoped), `-39d`
(integrated key vs BYO), `-914` (resident-time + compute metering), `-a4e` (email
component), `-ede` (E2E). The per-contract consumer map is at the end.

This is a record, not a build. Every downstream item above is a build; this fixes the six
seams between them so they need no further design and cannot silently disagree about the
alias grammar, the lifecycle, or which number lands on whose bill.

## What this is, and the one sentence that produces the whole design

A fourth portal tab beside Chat and Code that lets a user **fire up and manage named,
persistent agents** — call them *hermes* agents — each a long-running opencode process on
its own PVC that keeps working after the browser closes and lives until the user
intentionally shuts it down.

The Code surface is the near-miss this generalises from, and the difference is the entire
design. Finding 43 records what Code does: clicking into the terminal spawns `opencode`
**per websocket** over ttyd, and the agent **dies on disconnect** — a fresh cold boot on
every reconnect, nothing resident between them. That is correct for Code, where the agent
is a tool a person drives while looking at it. It is exactly wrong for an Agent, whose
whole value is being *away* from it: an Agent must be a resident daemon that the console
**attaches to**, never a process the console **spawns**.

So the Agents surface is not a flag on the workspace. It is a new surface with a new
residency model, a new metering dimension (resident **time and compute**, not only
inference tokens), and a new config model (integrated metered key **or** bring-your-own
external key). It reuses, and does not reimplement: PVC-backed persistent state on k3s,
the virtual-key metering path gateway→Forge, the portal iframe surface pattern, the
opencode/ttyd console, and the one-control-plane constraint.

### The hard invariant that outranks everything here

**The Code/workspace surface stays byte-unchanged and green.** The camp runs on it
2026-08-11. Nothing in this record or in any item that consumes it may edit
`deploy/workspace/*`, `deploy/k8s/60-workspace-common.yaml`,
`deploy/k8s/61-workspace.template.yaml`, `deploy/bin/provision-workspace.sh`, or
`tests/test_workspace_shell.py`. Contract 6 makes that mechanical rather than a promise.
The Agents surface is built entirely from **new** files — a new k8s template, a new
provisioner, a new `deploy/agent/` tree, new portal modules — that sit *beside* the frozen
set. Where a shared helper genuinely must change (the alias grammar in `gateway.py`, the
metering surface), Contract 1 is written as a strictly **additive** change that leaves
every existing `user::surface` path byte-for-byte as it was, and `gateway.py`/`metering.py`
are **not** in the frozen set precisely so that additive change is legal — but the Code
manifests that consume them are.

---

## Contract 1 — agent identity, k8s naming, console path, and the metering alias

### Identity

An agent instance is `(<user>, <name>)`. `<user>` is the Keycloak username, exactly the
principal every other surface already uses. `<name>` is a user-chosen slug constrained by
the **same** pattern the workspace already enforces on project names
(`deploy/workspace/shell-server.py`: `^[a-z0-9][a-z0-9-]{0,38}$`). Constrained, not
sanitised, for the reason that file gives: a rejected name is easy to explain, a silently
rewritten one is not. The slug constraint is load-bearing for the alias grammar below — it
guarantees `<name>` contains no `::` and no `/`.

### k8s object names

Every per-agent object is `agent-<user>-<name>`: the PVC, the Deployment, the Service, and
the BYO Secret (Contract 4). This mirrors the workspace's `ws-<user>` convention and keeps
one object family greppable. RFC 1123 caps a name at 63 characters; with the `agent-`
prefix and one separator that leaves `<user>` + `<name>` ≤ 56, which the two slug
constraints already satisfy with room to spare. The provisioner (`-055`) rejects a longer
pair rather than truncating — a truncated name that collides with another user's agent is
the one failure mode worth refusing loudly.

### Console-proxy path

The portal proxies the workspace at a fixed `/workshop/` because a user has exactly **one**
workspace, so `_workspace_url` needs nothing per-user in the path (`portal.py`). A user has
**many** agents, so the instance name must appear in the path: **`/agents/<name>/`**,
proxied to the Service `agent-<user>-<name>`. The `<user>` is **never** in the path — it is
resolved from the authenticated portal identity, exactly as `_workspace_url` resolves the
workspace pod from the signed-in name and never from a parameter. That is the whole
owner-scoping guarantee `-0e7` consumes: there is no path component a caller can point at
somebody else's agent, because the only identity input is the one oauth2-proxy established
from loopback (`portal.py` module docstring). A request for `/agents/<name>/` where the
signed-in user owns no agent `<name>` is a 404, the same safe direction `require_admin_user`
already fails in.

### The metering alias — the exact grammar, and why it round-trips unchanged

Today the alias is `<username>::<surface>` with `surface ∈ {chat, ide, terminal}`.
`gateway.parse_alias` splits it with **`rpartition("::")`** (the *last* separator);
`metering.py`'s SQL splits it with **`split_part(alias, '::', 1)`** and `split_part(...,
'::', 2)` (the *first* separators). For a two-field alias these agree. They **diverge** the
moment a third `::` appears, and that divergence is the trap this contract exists to avoid:

- `baron::agents::scraper` — Python `rpartition` yields username `baron::agents`, surface
  `scraper`; SQL `split_part(_,2)` yields surface `agents`, losing the instance. Both
  renderers wrong, in *different* ways. This is the obvious grammar and it is the losing
  one; it is written here so nobody reaches for it.

The grammar that round-trips through **both** existing splitters with **zero** change to
either splitter's logic is to keep exactly **one** `::` and fold the instance discriminator
into the surface field with a `/`:

> **`<username>::agents/<name>`** — e.g. `baron::agents/scraper`.

- Python `rpartition("::")`: username `baron`, surface `agents/scraper`. One separator, so
  it behaves identically to every existing alias.
- SQL `split_part(alias,'::',1)` = `baron`, `split_part(alias,'::',2)` = `agents/scraper`.
  So `metering.spend_by_user_and_surface` attributes an agent's inference to the right user
  and to a **per-instance surface** with no query change at all — `baron / agents/scraper`
  appears on `/admin/spend` automatically. That per-instance surface row is a feature, not
  an accident: it is inference spend broken out per agent for free.

The **minimal additive change** — this is the whole change, and it touches only
`gateway.py`, which is not in the frozen set:

```
AGENT_SURFACE = "agents"          # the surface family; an instance is agents/<name>

def agent_key_alias(username, name):        # NEW; does not touch key_alias()
    if not SLUG.match(name): raise ValueError(...)
    return f"{username}::{AGENT_SURFACE}/{name}"

def parse_alias(alias):                     # ONE added clause, existing paths unchanged
    if "::" not in alias: return None
    username, _, surface = alias.rpartition("::")
    if surface in SURFACES or surface.startswith(AGENT_SURFACE + "/"):
        return username, surface
    return None
```

`key_alias(user, surface)` and its `surface in SURFACES` guard are **untouched** — agents
mint through the new `agent_key_alias`, so no existing `chat/ide/terminal` call changes by
one byte. `issuance.issue` gains a parallel agent path (or an `agents/<name>` surface
argument) in `-055`/`-39d`; the five-step invariant it documents (principal exists and is
enabled, delete-before-mint, ledger hash follows the rotation, budget carried, audited)
applies unchanged.

**Consumed by:** `-055` (k8s names), `-627` (identity in the create/status API), `-0e7`
(console path + owner-scoping), `-39d` (`agent_key_alias`, issuance), `-914` (per-instance
surface on the bill).

---

## Contract 2 — the residency model, and how the console attaches to a living agent

### The distinction from Code, stated as the invariant

Code (finding 43): **ttyd spawns `opencode` on every websocket connection; the process is
bound to the connection and dies with it.** Agent: **`opencode` runs as the pod's own
long-lived process, decoupled from every console connection; connections attach and detach;
the process persists until the agent is intentionally stopped.** An Agent that spawned its
opencode per connection would be a workspace with a different tab, would lose all work on
disconnect, and would defeat the one thing it is for. This invariant is the surface.

### How the console attaches

The resident daemon is **`opencode serve`** — opencode's headless HTTP server mode, which
hosts a persistent session independently of any TUI. It runs as the Agent pod's main
container process (the process whose liveness *is* the pod's liveness). The console does
**not** spawn a new opencode; ttyd runs an opencode **client attached to the resident
server** on the pod's loopback, so the console renders and drives the session the daemon is
already running, and streams its events. On disconnect, the server keeps executing the
session; on reconnect, a fresh client attaches to the *same* running session and sees its
current state — no cold boot, which is precisely the 55%-CPU / 712-MB boot finding 43
measured and which an Agent must never pay on reconnect.

`opencode serve` (headless, session outlives any client) is the RECOMMENDED primitive
because it is opencode-native — no added dependency, and it is the mode designed for exactly
a session that must survive its viewer. The documented fallback, if attach ergonomics prove
insufficient in `-0e7`, is **tmux**: run the opencode TUI inside a long-lived `tmux` session
as the daemon, and have the console `tmux attach`. `-0e7` makes the mechanical call and
verifies it; this record binds the **contract** — a resident daemon holds the session, the
console attaches read/write, and a disconnect never ends the session — not the exact flag.

Session state lives on the PVC under `XDG_DATA_HOME` pointed **into** the PVC, the identical
fix finding 30 applied to the workspace so opencode's sqlite session db survives a restart
rather than living in an emptyDir. For an Agent this is not a nicety; it is what makes
stop→start resume the same agent.

### Lifecycle states and the exact k8s mechanism for each

The Deployment uses `strategy: Recreate` and a ReadWriteOnce PVC, for the same reason the
workspace does: two replicas of one stateful agent is never what anyone wanted, and a
rolling update would try to start a second.

| State | k8s reality | Meter (usage, not cost — see 3b) |
|---|---|---|
| **created** | PVC + Service + BYO Secret applied; Deployment applied at `replicas: 1`; pod scheduling. | begins accruing once the pod is Running |
| **running** | `replicas: 1`, pod Ready, `opencode serve` up. | accrues resident-time + compute (Contract 3) |
| **stopped** | Deployment scaled to **`replicas: 0`**; **PVC retained**; no pod. | **zero** — see below |
| **deleted** | Deployment + Service + Secret removed, then **PVC removed**. State destroyed. | none; irreversible |

Transitions, each a concrete k8s action `-627` drives:

- **created → running:** provisioner applies at `replicas: 1` (or `kubectl scale
  deploy/agent-<user>-<name> --replicas=1`). Pod schedules, PVC mounts, `opencode serve`
  boots.
- **running → stopped:** `kubectl scale deploy/agent-<user>-<name> --replicas=0`. The pod
  terminates gracefully; opencode's session is already checkpointed to the PVC sqlite, so
  nothing is lost. The PVC is **retained**.
- **stopped → running:** `kubectl scale --replicas=1`. The session resumes from the PVC —
  the same agent, not a new one.
- **any → deleted:** delete Deployment/Service/Secret, then delete the PVC. The PVC deletion
  is the point of no return and is the one step `-627` must confirm before reporting
  `deleted`, so a half-deleted agent cannot leave a spendable BYO Secret or a resident PVC
  behind.

**"Stopped accrues no resident usage" is expressible exactly because stopped means
`replicas: 0`.** There is no pod, so there is no `status.startTime` advancing and no cAdvisor
counter incrementing (Contract 3). The cost is *literally* zero, not "billed at a stopped
rate" — the meter has no pod to read, which is the cleanest possible form of the claim and
the reason the state is modelled as scale-to-zero rather than as a paused process inside a
still-running pod.

**Consumed by:** `-055` (Deployment/PVC/Service shape, Recreate, `opencode serve` as PID
1), `-627` (the four transitions as API actions), `-0e7` (attach mechanism).

---

## Contract 3 — two metering dimensions

An Agent consumes two things, and the platform must show both. Conflating them, or letting
either perturb the existing operator bill, is the failure this contract forecloses.

### (a) Inference tokens — the existing path, unchanged

The agent's inference goes through its `<user>::agents/<name>` virtual key → gateway →
Forge, and lands in `LiteLLM_SpendLogs` exactly like an IDE key's traffic. `metering.py`
already attributes it: `split_part` yields user `<user>` and surface `agents/<name>`
(Contract 1). **No change to the gateway path, no change to
`metering.spend_by_user_and_surface`, no change to `/admin/spend`.** The one-query,
one-bill invariant (finding 34's lesson) is preserved by not touching the query.

### (b) Resident-time + compute — net-new, in a separate ledger

This is the dimension the token ledger cannot see: an agent that is *up* costs a reserved
PVC, a scheduled pod, and CPU/memory, whether or not it is calling a model. Data sources,
named and chosen:

- **Wall-clock resident time:** the pod's **`status.startTime`**. A collector samples each
  `agent-*` pod; a Running pod is accruing, and `now − startTime` is the current interval.
  Intervals are summed into a durable counter in the **control-plane** DB so the total
  survives collector restarts and pod restarts.
- **Compute:** **cAdvisor's `container_cpu_usage_seconds_total`** (per-container cumulative
  CPU-core-seconds), exposed on every kubelet at `/metrics/cadvisor` by default, plus
  `container_memory_working_set_bytes` for a memory high-water mark. **Chosen over the
  alternatives for a stated reason:** cAdvisor's CPU metric is a **monotonic counter**, so a
  collector that misses a sample under-reports **nothing** — the next read still sees the
  full delta — and core-hours is just `Δcounter / 3600`, a cost basis by construction.
  **metrics-server is rejected** for the meter: it exposes only an *instantaneous* gauge
  (`kubectl top`), so a sampling collector integrates it by hand and any missed sample is
  lost forever — unreconcilable, which is the one thing a bill must never be.
  **kube-state-metrics is not a usage source at all** — it reports object *state*
  (`replicas`, phase); it is used here only to cross-check that a `stopped` agent really is
  at `replicas: 0` and therefore really is accruing zero, a check and not a meter.

- **Attribution key:** `(user, agent)`, taken from the pod's labels
  `agent.enterprise-ai/user` and `agent.enterprise-ai/name` — **not** from the virtual key,
  because compute is consumed by the *pod*, not by an inference call, and a stopped agent
  with no inference still had a PVC reserved while it ran.

- **Cost basis — RULED by Baron: there is none. Meter USAGE, not cost.** The recommendation
  below was a per-hour resident rate plus a CPU-core-hour rate, with the dollar figures
  RESERVED as a pricing decision. Baron ruled against the whole frame: **the hardware is
  owned, so its cost is sunk; inference already tracks a real Forge cost and compute does
  not.** So the second dimension is a set of **quantities per agent — resident hours,
  CPU-core-hours, peak megabytes — with no dollar basis, no rate configuration and no
  pricing anywhere in it.** The *shape* survives (resident-hours and core-hours are exactly
  what the recommendation would have multiplied); the multiplier does not exist. If
  commodity cloud compute is ever added, cost-wiring is a **FUTURE item**: the seam is these
  quantities, and building the multiplier before there is a real bill behind it would be
  inventing a number nobody owes. `-914` landed the collector and the ledger under this
  ruling — `control-plane/app/agent_usage.py`, table `agent_usage`, `/admin/agents/usage`
  and the `by_agent` sibling on `/portal/api/spend`.

### How it surfaces beside inference spend without perturbing the operator bill

The resident ledger lives in the **control-plane** database, **not** in the gateway's
`LiteLLM_SpendLogs`. So `metering.spend_by_user_and_surface` — which reads only the gateway
DB — is **byte-unchanged**, and `/admin/spend` (the query scope item 4 names, the one an
operator without a browser has) is untouched. The two numbers are composed **in the endpoint
layer**, not in SQL: `/portal/api/spend` already merges keys and spend after the database
answers, and gains a sibling `resident` section from the new ledger, summed alongside the
`by_surface` inference rows — the same additive pattern the portal already uses, never a
fold of resident usage into a token-spend row. `-914` adds the sibling (`by_agent` on
`/portal/api/spend`, `agent_usage` on `/portal/api/admin/overview`, and `/admin/agents/usage`
for the browserless operator); it does not edit the inference query — `metering.py` is
byte-identical to the commit this epic began at, and
`control-plane/tests/test_agent_usage.py` fails if that stops being true. This keeps finding 34's rule intact: one query names inference spend, and
the new number is *added beside* it rather than changing what it returns.

**Consumed by:** `-914` (collector, ledger, portal surfacing), `-627` (status shows
resident usage per agent), `-ede` (E2E asserts both dimensions appear).

---

## Contract 4 — the config model: integrated metered key vs bring-your-own external key

The console configures three things per agent: **email** (Contract 5), **coding** (model,
tenant instructions — reuse the workspace's ConfigMap-as-directory mechanism verbatim), and
the **model API**, which is the interesting seam.

### Integrated (the default)

The agent inferences through its `<user>::agents/<name>` virtual key at
`OPENAI_API_BASE=http://gateway:4000/v1`, identical to the workspace's one route out of the
building. Metered, budgeted, audited, on the one bill. This is the default because it is the
posture the whole platform exists to provide.

### Bring-your-own external key (BYO)

The user supplies their own provider credential (their own OpenAI/Anthropic account). The
agent's env `OPENAI_API_BASE` / `OPENAI_API_KEY` then point at the **external provider**,
not at `http://gateway:4000`. By design this routes inference **around the gateway** and
therefore **produces zero gateway ledger rows** — the traffic never touches our layer.

**The one-control-plane tension, ruled explicitly** (not RESERVED — only the email default
and the cost basis are). The purist objection is real and must be stated: finding 4 records
five routes *around* the gateway as precisely the leak the one-control-plane constraint
exists to prevent, and a BYO agent is an off-gateway path. The resolution is **provenance,
the same distinction findings 27 and 37 turn on**:

- Finding 4's leak was off-ledger **by accident**, on a **shared** enterprise surface where
  the operator pays and must meter, and it was rendered as **healthy** — an unmetered path
  the operator did not know existed.
- A BYO agent is off-ledger **by declaration**, on a **per-user** resident the user
  themselves spun up with **their own** credential — which is the exact posture the standing
  constraint already permits ("the customer's layer calling the customer's own providers
  with the customer's own credentials is not phone-home").

So BYO is allowed **iff it is visible**. The agent's config records `model_source: byo`, and
the portal shows the agent as BYO with its inference cost as an explicit **"off-ledger by
design"** label — **never a silent $0**, because a silent zero reads as "free" or "broken"
exactly the way finding 43's silence read as failure. The resident-time + compute meter
(Contract 3) still meters a BYO agent, because it still holds a PVC and burns CPU on our
hardware — BYO removes the *inference* row, not the *residency* row.

### BYO secret storage

Per-agent k8s Secret **`agent-<user>-<name>-byo`**, separate from the integrated virtual-key
Secret. **Set-once and never returned:** the create/config endpoint accepts the key, writes
it to the Secret, and every config-read returns only `model_source` (and at most a masked
hint), **never the key material** — the write-only mirror of the `keys/rotate` "shown once,
never again" rule, and a direct application of finding 2 (never hold or hand back a raw
credential). Rotating BYO is re-supplying it; there is no read path, ever.

### How `model_source` is actually spelled, as landed by `-39d`

Recorded here so `-627` and `-914` read it rather than guess it. The provenance lives on
the Kubernetes objects, not in a side table: `agent-<user>-<name>`'s Deployment **and its
pod template** carry the label

> **`agent.enterprise-ai/model-source: integrated | byo`**

— hyphenated, matching the `agent.enterprise-ai/user` and `.../name` labels Contract 3
already keys the resident meter on, and deliberately **not** in the Deployment's
`selector.matchLabels`, which is immutable and would make switching an agent between the
two modes a delete-and-recreate of a pod holding a ReadWriteOnce PVC and a live session.
A spend view therefore reads the mode from the same object it already reads the
attribution key from, and a BYO agent cannot render as `$0` without that label being
looked at.

**Consumed by:** `-39d` (integrated-vs-BYO routing, the Secret, the visibility label),
`-627` (config UI), `-a4e` (email config sits in the same model).

---

## Contract 5 — the email component (RESERVED to Baron)

An agent must be able to send email (`-a4e`). The default component must be OSI-approved
with **no** user-count, seat, revenue or feature trigger (CLAUDE.md component-defaults rule);
source-available and open-core are disqualifying *as defaults*, and a component whose
*contracted capability* sits behind a paid tier fails the rule even if its core is
permissive — the exact test that disqualified Open WebUI and Cline's enterprise tier.

Candidates, each against the rule:

| Component | License | Verdict against the rule |
|---|---|---|
| **Maddy** | GPL-3.0 | **Passes cleanly.** Single Go binary (SMTP submission + outbound, optional IMAP), **no enterprise tier**, no trigger. GPL, not AGPL: run as a standalone daemon the agent talks to over standard SMTP submission — no linking into our Apache code, so the copyleft is mere-aggregation and never reaches us. |
| **Stalwart** | AGPL-3.0 **+ commercial** | **Disqualified as default.** Richer (JMAP/IMAP/full stack), but it ships an **Enterprise edition** — a contracted capability behind a paid tier, which is the open-core disqualifier by the same reasoning as Open WebUI/Cline. AGPL additionally makes it a "configure, never patch" component like Grafana/Firecrawl (§3.6) if ever used. Kept as a **documented swap**, not the default. |
| **Postfix** | IBM Public License 1.0 | OSI-approved, **no tier**, battle-tested. But it is an **MTA only** — no IMAP/mailbox/submission stack without bolting on Dovecot etc., so it is the **heaviest to operate** and is not one component. A sound no-tier alternative for an operator who wants the classic MTA. |

**RECOMMENDATION: Maddy (GPL-3.0)** — single no-tier binary, GPL-not-AGPL so even a future
wrapping carries less network-copyleft hazard than Stalwart, operated as a standalone SMTP
submission daemon so there is no linking at all. **RESERVED — Baron decides**, because
adding a new default component is a product decision, and the alternatives (Stalwart's reach
vs its enterprise tier; Postfix's rock-solid MTA-only ops) are a real trade he should make.
See *Reserved rulings*.

**Consumed by:** `-a4e` (the component and its integration), `-39d`/`-627` (email config in
the console).

---

## Contract 6 — the Code-untouched invariant, made mechanical

The camp runs on the Code/workspace surface 2026-08-11. This surface must not touch it. The
frozen set, exhaustively:

- `deploy/workspace/*` (every file — shell-server.py, opencode.json, AGENTS.md, Dockerfile,
  model-settings.yml, and the rest)
- `deploy/k8s/60-workspace-common.yaml`
- `deploy/k8s/61-workspace.template.yaml`
- `deploy/bin/provision-workspace.sh`
- `tests/test_workspace_shell.py`

**Mechanical enforcement:** a hermetic test — `tests/test_code_surface_frozen.py`, owned by
`-055` (the first item to land Agents infra) — runs

```
git diff --exit-code <PRE_AGENTS_BASELINE> -- \
    deploy/workspace \
    deploy/k8s/60-workspace-common.yaml \
    deploy/k8s/61-workspace.template.yaml \
    deploy/bin/provision-workspace.sh \
    tests/test_workspace_shell.py
```

and fails on any non-empty diff. `<PRE_AGENTS_BASELINE>` is the pinned commit at which the
Agents epic began (recorded in the test). The test is proven to bite by fault injection —
touching one byte of any frozen path must turn it red — the same discipline
`tests/test_workspace_egress_allowlist.py` applies to the netpol shape.

The Agents surface is therefore built as **new files beside the frozen set**: a new
`deploy/k8s/62-agent.template.yaml`, a new `deploy/bin/provision-agent.sh`, a new
`deploy/agent/` tree, new portal modules. The one legal shared edit is Contract 1's additive
change to `gateway.py` (not frozen) and the additive resident-ledger surfacing in the portal
(not frozen); neither the Code manifests nor `test_workspace_shell.py` change by one byte.

**Consumed by:** `-055` (owns the test), and **every** other item (`-627`, `-0e7`, `-39d`,
`-914`, `-a4e`, `-ede`) must keep it green — `-ede` in particular asserts the Code surface
is still byte-identical and still boots at the end of the whole build.

---

## Reserved rulings — Baron decides

Two rulings inside the contracts above are RESERVED. Each carries its recommendation, the
alternatives, and the trade, and is **not** silently chosen.

1. **Email component default (Contract 5).** RESERVED — Baron decides.
   - **Recommendation:** Maddy (GPL-3.0).
   - **Alternatives:** Stalwart (AGPL-3.0 + commercial — richer JMAP/IMAP stack but an
     enterprise tier that disqualifies it as *default* under the open-core rule; documented
     swap); Postfix (IPL-1.0 — no tier, battle-tested, but MTA-only and the heaviest to
     operate).
   - **Why:** adding a new default component is a product decision; Maddy is the cleanest
     rule-compliant single binary, but the reach-vs-tier and ops-weight trades are Baron's.

2. **Resident-time + compute cost basis (Contract 3b).** ~~RESERVED~~ — **RULED by Baron,
   2026-08-10, in `-914`: METER USAGE, NOT COST. Do not reintroduce a cost basis.**
   - **What was recommended:** a per-hour resident rate (charged only while `running`, zero
     while `stopped`) plus a CPU-core-hour rate applied to cAdvisor core-hours, with the
     dollar figures read from config.
   - **What was ruled:** none of it. Owned hardware is sunk cost, and inference is the only
     dimension with a real upstream bill behind it (Forge). The resident dimension is
     surfaced as **usage quantities** — hours, CPU-core-hours, peak megabytes — beside
     inference spend, and there is no rate, no currency and no pricing configuration.
   - **What is deferred, and where the seam is:** if commodity cloud compute is ever added,
     it has a real invoice and the quantities above are what a cost would multiply. That is
     a FUTURE item, not a gap in `-914`. The alternatives the recommendation weighed
     (compute-only, flat per-agent-hour, memory-weighted core-hours) are all *rate shapes*
     and are moot until there is a rate.
   - **Why it is recorded here rather than quietly dropped:** the record said RESERVED, and
     a reader who found this section without the ruling would build the rate. Contract 3(b)
     above carries the same ruling inline for the same reason.

---

## Downstream consumer map

| Item | Consumes |
|---|---|
| `-055` resident pod + parallel provisioner | Contract 1 (k8s names), 2 (Deployment/PVC/`opencode serve`, lifecycle mechanics), 6 (owns `test_code_surface_frozen.py`) |
| `-627` Agents tab create/status/stop/delete | Contract 1 (identity), 2 (the four transitions), 3 (per-agent resident usage in status), 4 (config UI) |
| `-0e7` console attach, owner-scoped | Contract 1 (console path + owner-scoping), 2 (attach-not-spawn) |
| `-39d` integrated key vs BYO | Contract 4 (routing, Secret, visibility label), 1 (`agent_key_alias`/issuance) |
| `-914` resident-time + compute metering | Contract 3 (collector, ledger, additive portal surfacing) |
| `-a4e` email via OSI component | Contract 5 (component + integration) |
| `-ede` E2E | all six — especially 6 (Code still byte-identical and green) and the full lifecycle across 1–4 |
| `-783` third-party chat (Slack, Discord) | Contract 5's shape reused for chat: the tenant's own workspace/guild, the tenant's own bot tokens, no chat server in any manifest |
| `-e5ca` turnkey one-shot (`deploy/bin/hermes-up.sh`) | Contracts 1 (alias + console path), 2 (residency, and that a re-run must not end the session), 4 (integrated is the default and BYO is refused here), 5's connector shape. Composes `provision-agent.sh` and the pod's own chat tools; implements none of them |

---

## Live turnkey depends on the Agents-surface deploy (`-a39`)

`deploy/bin/hermes-up.sh` cannot be confirmed end to end against the **currently deployed**
control plane, and this is a deploy-lag fact rather than a defect in either.

Observed 2026-08-10, read-only: `deploy/control-plane` on the cluster runs image tag
`enterprise-ai-control-plane:04e98a9`. At commit `04e98a9` the files
`control-plane/app/agents.py`, `agent_usage.py` and `agent_console.py` **do not exist** —
they arrive with `-783`/`-a4e` on `main`. So `POST /admin/keys/issue` there cannot mint
`<user>::agents/<name>` (Contract 1's grammar), and the provisioner's own alias assertion
is what refuses, correctly.

Consequences, so nobody re-derives this:

- The turnkey path is proven in `tests/test_hermes_up.py` against the real scripts through
  recording kubectl/curl, including each validation failure injected in turn. That is the
  proof that exists today and it is not a live proof.
- A live confirmation needs either the Agents-surface deploy (ship checklist
  **`enterpriseaiframework-a39`**) or the local-app mint (`-ede`), plus a real tenant bot
  token for the chat leg — the connector's presence check asks `agent-<chat> config` inside
  the pod, and no fixture can answer that honestly.
- Do not "confirm" it by pointing `hermes-up.sh` at a fixture. Its entire value is that it
  refuses to print READY over something it did not observe; a faked observation removes the
  only thing it does.
