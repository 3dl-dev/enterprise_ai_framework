# Design Brief: Private AI Stack

**Status:** input to adversarial design review (v3 — supersedes `open-inference-stack-brief.md`)
**Date:** 2026-07-26
**Author:** CEO (Atlas) with founder

> **v1 was mis-framed.** It anchored on Forge (an Azure-hosted billing proxy for frontier API
> calls) and on inference-infrastructure theory, and everything downstream inherited Forge's
> concerns. Forge is out of frame. This brief is written from the customer's requirement.

---

## 1. The customer, in their own words

> *"I'm MEDIUM-BIG CORP. I'm paying Anthropic and OpenAI $500k/yr. I don't want to do that
> anymore. I want a fully integrated stack that gives my users a UI, a coding agent, a whole
> slew of product features like Claude/ChatGPT, and all the enterprise usage, billing, metering,
> reporting goodness PHBs post on their board decks. I might rent GPU, I might buy GPU."*

That is the requirement. Not an inference control plane — **a private replacement for the
Claude/ChatGPT enterprise product**, running on hardware the customer rents or owns.

**But the enterprise is the first customer, not the only one.** See §1b.

## 1b. Timing, and why it does not matter

**The model question is about production, not serving.** It is not "can an open model answer a
user's query well enough to replace Claude." It is: **can a frontier model write the boring layer
correctly enough that one person can ship it** — the SSO, the RBAC, the tenant isolation, the
audit trail, the concurrent quota enforcement, the reconciliation. That layer is what the
open-in-name-only vendors gatekeep, and building it is the only thing standing between an
enterprise and a genuine commons.

That reframes the whole timing question, because the moat has a measurable shape:

> **The moat is exactly the set of work that is founder-attention-gated rather than
> fleet-throughput-gated.** Generation was never the constraint. Verification is. The boring
> layer is expensive because agent output on auth, isolation, and money is reliably
> plausible-and-wrong, so every line costs human review — and human review is the scarce input a
> solo founder cannot buy more of.

The attention-gated set is currently: the auth chokepoint and token exchange; quota enforcement
under concurrency; cost reconciliation to the cent; the eval gate with automatic rollback;
per-department scoping and tenant isolation. Everything else in this design is mechanical and
verifiable by tests.

**That boundary is the instrument.** Each model release either moves an item across it or does
not. The movement is monotone and observable, it needs no adopters and no external data, and it
measures the thesis directly rather than by proxy. Record which category every feature was built
in; watch the line move.

So there are two cases and both are winning positions:

- **Models are already good enough.** The attention-gated list is short enough to clear, we ship,
  and the demonstration is that the boring layer no longer requires a funded company.
- **They are not yet.** We build everything throughput-gated now, the foundation stands, and
  every release is a re-test of the remaining items. When the list empties, we are already there.

Either way we are first, and there is no branch where we learn nothing.

**Design consequence, and it is the sharpest one in this document: optimize for verifiability
under agent authorship.** Not for elegance, not for brevity — for how cheaply a human can
confirm the agent got it right. Invariant tests with supremacy over the implementation,
conformance suites that execute rather than describe, adversarial harnesses on the isolation
boundary, mechanical reconciliation against a real invoice. Every such mechanism converts
attention-gated work into throughput-gated work, which is to say: **every one of them shrinks
the moat directly.** This is not testing discipline as good practice. It is the core mechanism
of the thesis.

Nothing should be specialized to any particular model — not GLM, not Kimi, not whatever lands
next quarter. A new model is a serving-port implementation and a trainer target. That is the
whole integration cost, by design.

**The incumbents cannot follow.** The open-core vendors have headcount and valuations resting on
the exact layer this commoditizes; matching a free competitor means destroying their own revenue
line. The frontier labs cannot ship a multi-provider governance layer because its purpose is
helping customers leave them. Neither position is a failure of nerve — both are structural.

**And the target scales down.** The factory's realistic output is small specialized models — a
14B that beats a frontier general model on one company's specific work. That size class does not
need a datacenter. It runs on commodity hardware: a workstation GPU, a Mac, a homelab box. The
same artifact that governs a five-thousand-seat enterprise runs as a single-user install with
the enterprise machinery switched off.

That is the larger claim, and it should be designed for from the start rather than retrofitted:
**consumer AI is currently rented from three companies, and this is the substrate that makes
owning it possible at any scale.** The enterprise deployment funds and hardens the thing; the
commodity-hardware deployment is what it is ultimately for.

## 2. The organizing principle: opinionated defaults, unopinionated core

Spring, not Rails.

Rails is encumbered by vertical integration: ActiveRecord *is* the ORM, by identity rather than
by interface, and swapping a layer means fighting the framework. Spring defines `DataSource` and
lets you run Postgres or Oracle or H2 without touching application code. Spring Boot then layers
opinions on top — one starter pulls a coherent working set — but every piece is declared rather
than welded, and auto-configuration **backs off the moment you define your own.**

So this product is three things:

1. **Contracts** for each part of the system.
2. **Wiring** — the control plane and factory that make the parts a system.
3. **One reference bundle** that installs a complete, working stack in one command.

A customer who wants the whole thing takes the bundle. A customer already standardized on
LibreChat, or with an existing Ray cluster, or with Keycloak already deployed, swaps that one
piece and nothing else changes. **We do not weld. We do not pick winners. We ship defaults that
get out of the way.**

The enforcement mechanism that makes "unopinionated" safe rather than reckless: **a conformance
suite per contract.** You do not test combinations of components — you test conformance, once
per contract. The supported configuration set is exactly what passes.

### One control plane, or it isn't a product

Nine contracts must not become nine consoles and nine login prompts. **A pluggable core is only
acceptable if it is invisible to the people using and running the system.** The closest analogy
is Spring Boot Actuator and Admin: every component exposes the same management surface no matter
what it is underneath, and there is one console over all of them.

So the contracts cover more than APIs. Anything implementing a port must:

- **Delegate authentication** to the control plane. One login, everywhere, always.
- **Emit usage and signal** in the standard shape.
- **Surface its administration through the control-plane API**, not its own console.

Which gives a rule sharp enough to enforce:

> Swapping a component may change how *that component feels to use*. It must never change where
> you log in, where you check your spend, or where an operator goes to do anything.

**The honest boundary** — abstractions leak and pretending otherwise loses trust. Identity,
navigation, policy, quota, cost, audit, and every administrative action are consistent by
contract. The *interaction design* of a swapped component is that component's own: choose
LibreChat over Open WebUI and you get LibreChat's chat experience. That is the customer's choice
and its consequence. The reference bundle is fully consistent because we integrated it.

Components that cannot delegate administration are not disqualified — they are conformant at a
lower tier, and the operator accepts a second place to go. The bundle is entirely top-tier.

This also strengthens the pitch: **one control plane over whatever you are already running** is a
better proposition than "replace your stack with ours." A customer with three chat UIs across
three departments can put one console over all of them without decommissioning anything.

**Honest headline claim** (v1's claim was false as written and is retired):

> One policy surface, one bill, and one audit trail across owned hardware, rented GPUs, and
> frontier APIs — with an inference bill that declines every quarter because the system keeps
> getting better at this specific customer's work.

Not claimed: a live scheduler bin-packing across owned and rented capacity. Model load times
foreclose it. One rule set across independently-scheduled tiers is achievable and is what
enterprises actually complain about.

## 3. The system

### 3a. The parts, as contracts

Each row is a contract we define, a default we ship, and a choice the customer keeps.

**Selection rule for defaults.** Reference-bundle defaults must be OSI-approved with no
user-count, revenue, seat, or feature trigger. Source-available and open-core licenses are
disqualifying **as defaults**; they may appear as documented swap options with the constraint
noted. A component whose *contracted capability* sits behind a paid tier fails the rule even if
its core is permissively licensed.

| Part | What the contract covers | Default we ship | Swap examples |
|---|---|---|---|
| Chat surface | OpenAI-compatible client, delegated auth, emits signal | **LibreChat** (MIT) | AnythingLLM, onyx-foss, in-house |
| Coding agent | OpenAI-compatible client, delegated auth; outcomes captured at git/CI, not from the tool | **opencode** (MIT) in a browser terminal over ttyd (MIT) — *superseded Cline during v0.1-dogfood; ruling and losing argument in design §3.6* | Cline (base only), aider (installed as the fallback), Continue, Tabby |
| Serving | OpenAI-compatible engine, plus capacity and health introspection | vLLM (Apache 2.0) | SGLang, llm-d, Dynamo, TGI |
| **Model catalog & routing** | Logical model → physical paths, each with price, feature/latency fingerprint, residency label | **Control-plane core — not swappable** | — |
| **Provider path** | Frontier adapter: auth, rate limits, price, residency, terms class, capture policy | Direct API adapters | Bedrock, Vertex, aggregators |
| **Router** | `route(request, weights) → path`, subject to a hard scope filter | **Control-plane core** | — |
| **Cache** | Exact and semantic lookup, scope-aware keys | GPTCache + **Valkey** (BSD-3) | Any scope-safe store |
| Compute | Node inventory and lifecycle | Static file + SkyPilot (Apache 2.0) | Existing k8s, Slurm, a list of IPs, **a single workstation GPU, a Mac, a homelab box** |
| Identity | OIDC / SAML / LDAP | Keycloak or Ory (Apache 2.0) | Authentik, Entra, Okta |
| State | Transactional store — **welded in v1, not a swap port** | Postgres | — |
| Trainer | Consumes a dataset, produces a model artifact | Axolotl (Apache 2.0) | Unsloth, torchtune, in-house |
| Eval | Consumes a test set, produces a verdict | Bundled harness | Existing internal evals |
| Signal | Accept, reject, regenerate, test-pass, commit-stuck | Additive namespace on OTel GenAI conventions | — |
| Console / reporting | Dashboards, alerting, export | **Perses** (Apache 2.0, CNCF) | Superset; Grafana **configured via auth.proxy, never patched** |

**Standing constraints on defaults, so they are not silently reintroduced:**
- **Cline's enterprise tier is out of scope** — and Cline is no longer the default at all. It was
  replaced as the *bundled* coding surface during v0.1-dogfood because an IDE extension cannot be
  reached by a user who installs nothing; the pass-through contract for a customer's own tool is
  unchanged, and that is where Cline still fits. Ruling in design §3.6. The constraint below still
  binds anyone who integrates it as a swap. The base extension is clean, and this design never
  needs its fleet features — auth is enforced at the network chokepoint and outcomes come from the
  git/CI shim. Do not integrate "Cline for Enterprise" as a convenience later.
- **LiteLLM's MIT core only.** Its `enterprise/` directory gates SSO, RBAC, and audit behind a
  key. We build those anyway; do not take a dependency on that directory.
- **Valkey over Redis.** Redis is tri-licensed since 8.0 and its default packaging steers to SSPL
  and RSAL, neither OSI-approved. Valkey is BSD-3 and is now the distro default for this reason.
- **Grafana, if swapped in, is configured — never patched.** AGPL's network clause triggers on
  modified versions; patching its auth to integrate with the control plane would make our
  integration code AGPL inside an otherwise-Apache bundle. `auth.proxy` header mode achieves the
  same result unmodified. Defaulting to Perses removes the judgment call entirely.

**We never write an inference engine, a chat UI, a coding agent, a cloud price catalog, or a
trainer.** The factory emits datasets and consumes model artifacts; that is the contract. A
customer with an existing MLOps pipeline plugs it in rather than abandoning it.

Every contract also gets a fake implementation, so the system is testable end to end without
GPUs.

### 3b. The wiring — what we build

Nothing joins the surfaces today. Open WebUI has its own users, the coding agent has its own
config, internal apps have their own keys, and no admin can answer one question across them.

- One login (existing IdP) granting chat, coding agent, and API access
- Usage and cost per person, per team, per model, per surface
- Quotas and budgets that actually stop overspend
- Audit trail compliance accepts
- The savings report — what was spent before, what now, what moved, what didn't
- Verifiable collection — an inspectable record of exactly what was captured and what it trained

This is the layer that is paywalled in every open-core competitor.

#### Visibility parity is an adoption gate, not a feature

**No org will leave Anthropic for something that shows them less than Anthropic does.** The bar
is public and specific, and any evaluating admin will open both dashboards side by side.

What the incumbents already give them: usage and cost broken out by model, by workspace or
project, by API key and by individual user; token detail split input / output / cache-read /
cache-write, because that is how the bill is actually computed; hourly (or finer) time buckets;
data fresh within minutes; export via API, not just a dashboard, because it has to land in their
BI tool; and **numbers that reconcile to the invoice**, which is what makes a finance team trust
any of it.

Treat that list as a hard requirement. Missing any single line is a reason to stay.

**And then exceed it, because we structurally can:**

- They see API calls. We see **people** — SSO tells us it was Sarah in Platform Engineering, not
  `api_key_id: sk-…7f3`.
- They see their own spend. We see **every provider plus owned and rented capacity in one view**.
  No vendor can ever offer this, and it is the only place the savings number can honestly live.
- They see tokens. We see **surfaces** — chat versus coding agent versus internal app — which is
  what tells an operator where the money actually goes.
- Nobody's enterprise tier tells you whether the AI helped. We have **outcome data**: accepted,
  regenerated, tests passed, commit stuck.

**Migration consequence:** the control plane must continuously ingest the provider usage and cost
APIs, not just meter local traffic. During transition the customer runs both, and the before-and-
after has to be one continuous series in one set of units. A savings report assembled from two
disconnected systems will not survive contact with a CFO.

### 3c. The factory — first-class, not a later phase

The customer owns the rack and the logs. Therefore:

- **Idle capacity becomes training capacity.** Corporate duty cycle is roughly 25–30% — weekdays,
  office hours, one or two timezones. Nights and weekends stop being waste.
- **Escalation becomes a corpus.** Every request routed to a frontier model is paid for anyway;
  capturing the pair turns the API bill into a labeling budget.
- **Behavioral signal is the foundation.** Accept/reject, regeneration, diff accepted, tests
  passed, commit stuck or reverted. First-party, ground-truthed, better than thumbs.
- **The bill declines over time** as the escalation rate drops. This is what justifies owning
  hardware rather than renting forever.

Realistic output is **small specialized models that beat big general ones on this customer's
work** — a 14B excellent at their ticket triage and code conventions. Nobody out-trains GLM 5.2
on one rack. The result is a tiered estate whose tiers shift downward over time.

We build the pipeline. The trainer and the eval harness are contracts.

### 3d. The bundle

One command installs a complete working stack: chat UI, coding agent, serving, control plane,
factory, Postgres, identity. It is the product for most customers and the demo for the rest.

## 4. Decided

- **Opinionated defaults, unopinionated core.** Contracts plus wiring plus a reference bundle.
  Defaults back off when a customer declares their own. Conformance suites make swapping safe.
- **One control plane, non-negotiable.** A pluggable core is only acceptable if it is invisible
  to users and operators. Every conformant component delegates authentication, emits usage and
  signal in the standard shape, and is administered through the control plane rather than its own
  console. Swapping a component may change how that component feels to use; it must never change
  where you log in, where you check spend, or where an operator goes to act. A design that
  produces a consistent architecture and an inconsistent experience is not a winner.
- **Integrate, don't reimplement.** Writing our own inference engine, chat UI, coding agent,
  price catalog, or trainer kills the project.
- **Chokepoint enforcement.** Every surface authenticates to the gateway; the gateway is the only
  path to serving, enforced at the network level.
- **Apache 2.0, no CLA, no open-core hooks, no reserved enterprise tier.** Irreversible by design.
- **No phone-home. Air-gap capable.** Verifiability is a headline product property, not a
  compliance checkbox — it is the strongest claim in the deck and no vendor can match it
  structurally. For many buyers it is the lead and cost is merely the justification.
- **Visibility parity with the incumbents is an adoption gate.** Anything less than what
  Anthropic's and OpenAI's admin surfaces already provide is a reason for an org not to move.
  Parity is table stakes; the ceiling above it (per-person attribution, cross-provider view,
  per-surface breakdown, outcome data) is where the product actually wins.
- **All spend flows through the layer; frontier is a tier.** We intermediate the frontier
  providers rather than racing to replace them. Enforcement is procurement, not quality — company
  money is spent through this layer or not at all. Savings start on day one from routing on price
  across provider paths, caching and dedup, before any local capacity exists.
- **Rent-first, buy-on-evidence.** An 8×H200 box is roughly $370k. Nobody should guess. The system
  must produce the utilization evidence that makes the buy decision defensible.
- **Calendar estimates are not a design input.** Do not shape the architecture around how long a
  conventional team would take to build it. The conventional estimate is worth recording once, as
  a pre-registered prediction the cost ledger will test — the delta between it and what actually
  happens is the cleanest evidence this project will ever produce. It is a measurement, not a
  constraint.
- **Behavioral signal is the factory's foundation; the escalation corpus is an accelerant.** The
  loop must work in an air-gapped deployment with no frontier tier at all.
- **Data provenance is an operator decision, not an engineering constraint.** We capture the
  operator's own logs — their users' inputs and the outputs produced from them. What a deploying
  org does with data it owns is theirs to decide. Escalation capture is configurable per
  deployment. We do not ship a feature whose stated purpose is training a competitor, and we do
  not cripple a general-purpose data pipeline to preempt someone else's legal argument.

## 5. Objective function (unusual — read carefully)

The founder is **not optimizing for revenue capture.** The goal is to demonstrate that production
cost has collapsed far enough that a commodity layer can be built outside a funded company — that
software moats are no longer defensible. Acceptable outcomes are "someone funds it" or "someone
hires us to deploy it," both downstream of the artifact existing and being adopted.

The sharper statement of what this design does: **it redistributes an amplification advantage
that currently accrues to the frontier providers, to whoever generates the traffic — and keeps
nothing for the intermediary.** For an enterprise that means their own corpus on their own work.
Scaled down to commodity hardware it means the same thing for a team or a person. Capture
position and moat ownership are separable, and this is the mechanism that separates them.

Consequences:
- **Adoption is the evidence.** A spoiler nobody installs proves nothing. Deployability and
  findability outrank feature count.
- **Nothing is specialized to a model.** A new open model is a serving-port implementation and a
  trainer target — never a rewrite. This is what makes the timing argument in §1b hold.
- **No data moat either.** The stack must not require phoning home.
- **Agent-legible maintenance**, operationalized as a test rather than a value statement: hand a
  fresh agent the abandoned repo, no help, have it add a serving backend. Repeat over a year. If
  it gets harder, the claim is false.
- **A quiet cost ledger** recording $/feature from commit one — kept, not announced.

## 6. Two constraints that decide success, and neither is technical

**All spend flows through the layer. Frontier is a tier, not the enemy.**

An earlier draft argued for sequencing around defection risk — don't move coding first, because
developers will notice a quality drop and go back to personal accounts. That framing is wrong and
is retired. Defection is a procurement problem, not a quality problem: **company money is spent
through this layer or it is not spent.** If a user wants Claude, they get Claude — through us —
and we route the request down the cheapest path that serves it.

Consequences, and they are large:

- **Savings begin before a single GPU exists.** The same Claude model costs different amounts
  direct, through Bedrock, or through an aggregator. Routing on price, plus caching and dedup
  across surfaces, cuts the bill on day one while the customer is still entirely on frontier.
- **The coding agent is in from the start.** It routes to frontier. Nobody's experience degrades,
  so nobody has a reason to route around it.
- **The best training signal is available immediately.** Coding outcomes are objective — tests
  pass or they don't — and we capture them from day one because the coding agent is on our layer
  even while frontier is doing the work. There is no window in which the factory is starved of
  its best input.
- **Migration is eval-gated, not calendar-gated.** A workload moves to local capacity when the
  evals say it can, using data collected the whole time. Nothing has to move on faith.

The old sequencing advice inverted into an ordering *preference*, not a constraint: chat,
summarization, document Q&A, classification and internal search are where open models are closest
to parity, so they will clear the eval gate first. That is an observation about what the evidence
will show, not a rule imposed ahead of it.

**Duty cycle.** Roughly 25–30% utilization on owned hardware. Naive break-even math says $42k/mo
is far past the threshold for owning; duty-cycle-adjusted math is much less flattering. The
factory is the answer, but no part of the design may assume saturation.

## 7. Enterprise-grade means (procurement checklist, not aspiration)

**Every item below is configurable, not assumed.** A single-user install on a workstation runs
the same artifact with all of it switched off — no SSO, no RBAC, no multi-tenancy, no chargeback,
because there is one tenant and they are the operator. This is a first-class configuration, not a
degraded enterprise deployment, and the architecture must not make the enterprise case load-bearing
for the small one. Scale of governance is a deployment choice; the substrate is the same.

HA control plane with no single point of failure · SSO and provisioning · fine-grained RBAC ·
immutable audit · air-gap install · backup and restore · tested upgrade path · tenant isolation
that survives a hostile tenant · quota enforcement against capacity · chargeback output finance
accepts · per-department training scopes, because one model trained on all internal traffic
redistributes data across departments through weights — a path that did not exist before and
needs exclusion flags and scoping.

## 8. Open questions for adversarial review

Unranked and deliberately not weighted. Attack the whole system; do not privilege any one of
these because it appears first or sounds hardest.

1. Does the router decide or advise? Advisory is a much smaller build and produces the savings
   number; automatic routing on live quality and cost signals is the full product. Which is v1,
   and does the advisory version calcify against the routing version?
2. Can one rack serve and train concurrently, or does the factory need dedicated windows — and
   what does that do to the utilization argument?
3. What does the eval harness look like when the test set is the customer's own traffic? It must
   gate every model swap with automatic rollback, and privacy scoping applies to the test set too.
4. Training on your own model's outputs degrades over generations. Frontier escalations are the
   external quality anchor. What happens in an air-gapped deployment that has none?
5. What is the minimum feature set at which users don't defect? Chat parity with ChatGPT is a real
   and unforgiving bar. Which features are load-bearing and which are theater?
6. Where exactly does opinionation stop? Defaults that back off are easy to describe and hard to
   bound. What is the supported-configuration policy, and what does a conformance suite have to
   assert to make swapping genuinely safe?
7. Are these the right contracts, at the right seams? A bad abstraction is worse than none. Which
   of the nine are wrong, missing, or should collapse into each other?
7b. Where is the abstraction permitted to leak? Every component delegating identity, usage, and
   administration to one control plane is easy to state and hard to deliver — some components
   will not cooperate. What is genuinely enforceable, what degrades to a second console, and is
   the tiered-conformance answer honest or a fudge?
8. How does the system produce buy-versus-rent evidence a CFO will sign, given capex,
   amortization, and duty cycle have no neutral answer?
9. What is v0.1, exactly — deployable this quarter, producing the savings number — and what forces
   v0.2 to happen rather than the cut line quietly becoming the whole project?
10. Does the whole thing survive its own objective function? Adoption is the evidence, and the
    stack cannot phone home to measure it.

## 9. Carried forward from the v1 deliberation (still valid)

- **Rejection-stream as signal.** Capture what callers asked for and were refused — wrong
  endpoint, unsupported parameter. That is the caller telling you what it expected. A vendor
  cannot ship the equivalent because their version phones home.
- **Carrying-cost measurement.** Track whether each new feature costs the same as the last as
  surface area grows. Needs no external data; it is the only clean test of the thesis. Freeze
  costs at the rate table in force on the commit date.
- **One policy surface, not one scheduler.**

## 10. Non-goals

- Reimplementing inference engines, chat UIs, coding agents, cloud price catalogs, trainers, or
  accelerator support.
- Any hosted service, telemetry, or account requirement.
- Revenue model design — explicitly out of scope for this review.
