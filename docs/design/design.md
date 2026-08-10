# Private AI Stack — Design

**Status:** architect synthesis of the adversarial design deliberation (item `3dl-7e0`). Normative.
**Supersedes:** nothing. **Depends on:** `docs/design/private-ai-stack-brief.md` (the requirement), `~/projects/dap/docs/specs/dap.md` (the framework).
**Audience:** the implementer. Not a record of the deliberation; the deliberation is in `rd show 3dl-7e0`.

Every section below either specifies a concrete design or explicitly names a gap. Where the four
dispositions disagreed, this document rules and says why the losing position lost. Where something
is genuinely unresolved it is in §11, not smoothed over.

Calendar estimates appear nowhere in this document by policy. Where the deliberation produced
session or LOC estimates, they survive only as the sealed pre-registered prediction in §9 (C2),
which is a measurement, not a plan.

---

## 1. What this is, and the honest headline claim

### 1.1 The artifact

A private replacement for the Claude/ChatGPT enterprise product, running on hardware the customer
rents or owns, assembled from components the customer can swap, governed by one control plane.
Apache 2.0, no CLA, no reserved tier, no phone-home, air-gap capable.

The enterprise is the **first** customer, not the only one. The same artifact runs as a single-user
install on a workstation GPU with the enterprise machinery switched off. That is a first-class
configuration, not a degraded one, and the architecture must never make the enterprise case
load-bearing for the small one.

### 1.2 What we actually build

Three things, and only three:

1. **Contracts** — twelve of them (§3), each defined by an executable conformance suite.
2. **Wiring** — the control plane, the gateway, the router, the ledgers, the factory pipeline. This
   is the layer that is paywalled in every open-core competitor and it is the entire product.
3. **One reference bundle** — a single command that installs a complete working stack.

We never write an inference engine, a chat UI, a coding agent, a cloud price catalog, or a trainer.

### 1.3 The headline claim

The brief's claim survives, but it must be split into two claims with different evidentiary
character, because collapsing them is how the project would deceive itself (§5.4):

> One policy surface, one bill, and one audit trail across frontier APIs, rented GPUs, and owned
> hardware.
>
> **Day one, the bill falls by a measured step** — from cross-surface cache hits, in-flight dedup,
> and routing to the cheapest *already-contracted* path (provider prompt-cache eligibility, batch
> eligibility, committed-discount path preference, tier selection, avoiding an unnecessary
> residency premium).
>
> **Over time it falls further on a slope**, as the customer's own corpus lets local models clear
> an eval gate on the customer's own work.
>
> The step is engineering. The slope is the thesis. The savings report is built so that a **flat
> slope is visible rather than hidden by the step.**

### 1.4 Claims explicitly retracted

These were in the brief or in earlier framing. They are wrong and are retired here. Do not
reintroduce them.

| Retracted claim | Why | Replacement |
|---|---|---|
| "Route on price across providers for the same model" | S16: identical list price direct vs Bedrock vs Vertex on standard endpoints; regional endpoints carry a ~10% *premium*; OpenRouter passes through provider price plus a ~5.5% surcharge. Meanwhile negotiated committed-spend discounts at ~$500k/yr run 15–30% off list. The arbitrage margin is 0–10% and is dominated by a discount the customer already has. | Route to the cheapest **already-contracted** path. Commitment-aware routing (§4.3). |
| "The bundle is entirely top-tier" | A4/S3: Cline (and every OSS coding agent surveyed) has no fleet auth surface. | Per-component tier table (§3.6). The bundle is top-tier on identity, usage and administration; the coding agent is tier-2 on its own configuration surface. |
| "Everything reconciles to the invoice" | A10: for owned/rented capacity there is no metered ground truth — depreciation, power, cooling and staff allocation are *modeled*. | Every displayed dollar carries an evidentiary tier label T1/T2/T3 (§5.3). |
| "Tenant isolation that survives a hostile tenant" (at the accelerator layer) | A18: process-per-tenant with no shared KV cache defeats the multiplexing that makes the utilization economics work. | Claim retained at the control-plane/data layer (scope, cache, ledger, budget) — which is where v0.1 lives. At the accelerator layer the supported model is trusted-tenant multiplexing within one org, with node/process isolation available as a configurable option at a stated utilization cost. |
| "Test conformance, not combinations" | A3: trainer × serving resource contention exists only at the intersection. | Conformance per contract, **plus** a short enumerated list of interaction suites at named seams (§3.5). The enumeration is incomplete by construction and that is accepted. |
| "No phone-home" as stated | A1/A15: read as absolute it contradicts continuous provider usage-API ingestion. | Precise restatement: **no telemetry to 3DL, and no 3DL-operated service in any data path.** The customer's layer calling the customer's own providers with the customer's own credentials is not phone-home. |

### 1.5 The objective function this design serves

The founder is not optimizing for revenue capture. The goal is to demonstrate that production cost
has collapsed far enough that a commodity layer can be built outside a funded company.

The sharpest statement, and the one that should drive design decisions when they are close:

> This design redistributes an amplification advantage that currently accrues to the frontier
> providers, to whoever generates the traffic — and keeps nothing for the intermediary.

Consequence, stated so nobody is surprised by it later: **3DL accrues zero data amplification from
this product.** That is deliberate. Three legitimate compounding channels remain and they are
reputation and methodology rather than data: the cost ledger (3DL's own production telemetry, which
is what 3DL is actually selling), the conformance-submission ledger (every third party who runs a
suite tells us where our contracts are wrong, at zero data cost), and the legibility series.

---

## 2. The organizing principle and the enforcement model

### 2.1 Spring, not Rails

Contracts, wiring, one reference bundle. Defaults back off the moment a customer declares their own.
The supported configuration set is exactly what passes conformance.

### 2.2 One control plane, non-negotiable

Twelve contracts must not become twelve consoles. Anything implementing a port must:

- **Delegate authentication** to the control plane. One login, everywhere, always.
- **Emit usage and signal** in the standard shape.
- **Surface its administration** through the control-plane API, not its own console.
- **Expose the parameters the control plane may adapt at runtime.**

The fourth clause is new and it is the fix for the deliberation's central structural finding: the
original three-clause contract gave the system capability and instrumentation but no configuration
surface, so by DAP §1.7's own words **every feature in the design was incomplete and the product had
no fast loop.** A controller needs parameters to actuate. Adding this clause is what makes §4
buildable.

The enforceable rule:

> Swapping a component may change how *that component feels to use*. It must never change where you
> log in, where you check your spend, or where an operator goes to do anything.

**Where the abstraction is permitted to leak.** Identity, navigation, policy, quota, cost, audit and
every administrative action are consistent by contract. The interaction design of a swapped
component is that component's own. A component that cannot delegate administration is conformant at
a lower tier and the operator accepts a second *view* — but never a second *login*, because tier-2
consoles sit behind an identity-aware reverse proxy with a shared nav shell. Nine consoles becomes
nine views, not nine doors.

### 2.3 The enforcement model: procurement, not quality

All spend flows through the layer. Frontier is a tier, not the enemy. Defection is a procurement
problem: **company money is spent through this layer or it is not spent.** If a user wants Claude,
they get Claude — through us.

That assertion was L0 text doing L4 work until the deliberation named the mechanism. The mechanism
is:

> **Network egress policy blocks direct access to provider API endpoints from managed devices and
> networks. The gateway's egress addresses are the only permitted path.**

Three supported enforcement mechanisms: network egress policy (firewall/security group), forward-proxy
allowlist, and managed-device configuration (MDM). One explicitly unsupported case: a flat trusted
internal network with no egress control (A14 — common in Slurm HPC estates). In that case the layer
is best-effort and **coverage is measured rather than assumed**, via the leak detector in §2.5.

### 2.4 The intermediation posture, and what it is not

The customer provisions their **own** upstream provider keys under their **own** committed-spend
agreements. The gateway mints per-user **virtual keys** against them, enforcing budget, quota and
rate limit per virtual key before forwarding. We are software the customer runs. This is the
documented model of LiteLLM, Portkey, Kong AI Gateway and Cloudflare AI Gateway.

This posture — not a design nicety, a legal and commercial one — is what defuses the whole cluster
of round-2 attacks: no new subprocessor (A23), no reseller or account-pooling question (A24), no
stripping of the customer's negotiated throughput tier (A26), no reconstruction of a gatekeeper
position (NA2). A shipped **security review pack** (data-flow diagram, no-egress attestation, SBOM,
exit procedure) is a deliverable, because their security team will ask.

### 2.5 The leak detector

Reconciling the provider invoice against our own metering ledger is normally described as a finance
feature. It is also the only mechanism that can detect that the chokepoint premise has quietly
failed. Spend on the provider invoice that does not appear in our ledger, and is not attributable to
an open breakglass window (§4.6), means somebody has direct provider access we do not know about.

**An unexplained invoice/ledger delta is an alarm, not a rounding note.** This gives customers who
cannot enforce egress (A14) a way to *measure* their non-coverage, and it makes the coverage claim
falsifiable rather than assumed.

### 2.6 The three loops (DAP §1.7), instantiated

| Loop | Mechanism in this design | Status |
|---|---|---|
| **Fast** (hours) | The router as a closed-loop controller over an operator-set weight vector (§4). Was **absent**; constructible at v0.1 because intermediation gives it live axes before any GPU exists. | Built in v0.1 |
| **Medium** (days–weeks) | Signal → corpus → train → **eval gate with automatic rollback** → promote. The gate is a textbook DAP invariant test with supremacy: it sits outside the trainer's optimization loop and the trainer cannot weaken it. Strengthened by paired shadow evaluation (§6.2). | v0.2 |
| **Slow** (weeks–quarters) | Tiered estate drift, buy-vs-rent, quarterly weight-vector review. Materially improved by intermediation: the customer learns their full workload profile (token mix, request shapes, peak/trough, cacheability, per-surface distribution) **on someone else's hardware before any capex.** | v0.1 collects, v0.2+ acts |

---

## 3. The twelve contracts

### 3.1 Core versus swappable

The classification rule: a thing is **core** if fragmenting it would recreate the multi-console
problem, if it is the single place a security property is enforced, or if it is the fast loop itself.
Everything else is swappable.

| # | Contract | Classification | Notes |
|---|---|---|---|
| 1 | **Chat surface** | Swappable | Default LibreChat (MIT). |
| 2 | **Coding agent** | Swappable | Contract rewritten — see §3.3. |
| 3 | **Serving engine** | Swappable | Single-model inference engine only. Default vLLM. |
| 4 | **Model catalog & routing table** | **CORE** | Logical model → physical paths, each with price, feature/latency fingerprint, residency label, commitment state. |
| 5 | **Provider path** | **CORE contract, pluggable adapters** | Frontier adapter: auth, rate limits, price, residency, terms class, capture policy, feature fingerprint. |
| 6 | **Router** | **CORE** | The fast loop. §4. |
| 7 | **Cache** | **CORE key derivation, swappable store** | Scope partitioning is the isolation boundary, not an optimization. §3.4. |
| 8 | **Compute** | Swappable | Node inventory and lifecycle. Static file + SkyPilot. Also: a single workstation GPU, a Mac, a homelab box. |
| 9 | **Identity** | Swappable | Keycloak or Ory. OIDC/SAML/LDAP + SCIM. |
| 10 | **State** | **WELDED — not a port** | Postgres. §3.4. |
| 11 | **Trainer** | Swappable | Dataset in, model artifact out. Default Axolotl. |
| 12 | **Eval** | **Harness swappable, GATE is core** | §3.4. |

Two things in the deliberation's list of fourteen names are **not peer contracts** and are recorded
here as what they actually are:

- **Signal** is a cross-cutting emission requirement, like auth. Every port emits it. It is a
  versioned schema owned by core, not a swappable component. Ruled per the purist; the losing view
  (signal as a thirteenth contract) lost because a contract you cannot swap out of is not a port.
- **Console / reporting** is a swappable *presentation* over a core-owned query-and-action API.
  Default Perses (Apache 2.0, CNCF). The query API and the action API are core. Grafana, if swapped
  in, is **configured via `auth.proxy` header mode and never patched** — AGPL's network clause
  triggers on modified versions and patching its auth would make our integration code AGPL inside an
  otherwise-Apache bundle.

Two core components that appear in no contract row because they are not ports, and which the
deliberation found missing from the original nine — name them explicitly so they get built and
owned:

- **Quota / budget enforcement point.** One admission point in the gateway. S6's conformance
  assertion: *a user over budget receives a 429 from the gateway before the request reaches any
  engine or any provider path, on every conformant backend.*
- **Capture ledger.** Append-only, hash-chained. Records every captured (prompt, completion,
  outcome, provider path, scope, capture-policy decision) tuple. Not an audit feature — an L4
  circuit breaker: **the trainer cannot consume a dataset whose rows are absent from the ledger,
  enforced in code.**

### 3.2 The four primitive shapes

The creative's C1 finding, adopted: the twelve contracts reduce to four shapes, which collapses most
of the conformance work.

| Shape | Interface | Contracts of this shape |
|---|---|---|
| **Surface** | Authenticated client of the gateway; base URL redirectable; emits a signal sub-schema | Chat surface, coding agent |
| **Resource provider** | register / health / capacity / delegate-admin / expose-adaptable-parameters | Serving engine, compute, identity, cache store, provider path |
| **Job** | typed input → judged artifact; resumable, versioned, idempotent, scope-carrying | Trainer, eval harness |
| **Signal** | Additive namespace on OTel GenAI semantic conventions | Cross-cutting |

Build **four generic conformance harnesses**, not twelve bespoke ones. Each contract then contributes
a per-contract assertion set that plugs into its shape's harness.

**Hard caveat, ruled explicitly: same shape does not mean same contract.** Trainer and eval harness
are both Jobs and they must **not** collapse into one contract. The seam between them is where the
rollback gate lives; merging them puts the gate inside the thing it governs, which is the exact DAP
§3.2 failure the gate exists to prevent.

### 3.3 Contracts whose shape changed during deliberation

**Coding agent — the second clause is rewritten.** Original: *"the coding agent emits diff/test
signal."* No OSS coding agent surveyed (Cline, Continue, OpenCode, Tabby) satisfies it at the free
tier; Cline's fleet features including OTel export are enterprise-only and are themselves configured
through Cline's own dashboard, so even the escape hatch is console-gated.

Replacement clause, which is both more honest and easier:

> **The deployment captures diff and test-outcome signal at the git and CI layer, independent of
> which coding agent is attached.**

The conformance question becomes *"can we observe outcomes from outside the tool"* — a different and
far easier claim than *"does the tool cooperate."* It works identically for every coding agent, and
it means the **base extension only** is ever needed. Cline's enterprise tier is out of scope,
permanently; do not integrate it later as a convenience.

The coding agent's *auth* clause becomes: the tool's upstream endpoint is redirected to the gateway
with a control-plane-minted, SSO-derived, rotatable per-user virtual key. Add to every coding-agent
evaluation an explicit gating question (S15): **can this tool's upstream endpoint be redirected to
our gateway with an API key?** Tools that hardcode their upstream, or that authenticate via a
non-redirectable vendor OAuth flow, cannot be intermediated without MITM-style interception, which is
out of scope.

**Serving — split into two seams.** vLLM serves one model per process. Multi-model routing, health
aggregation across models, and "which models exist" are not engine concerns; they live one layer up.
So the original Serving row conflated an engine contract (which vLLM, SGLang, TGI and Dynamo all
satisfy) with a model-catalog/routing contract (which was unnamed). The catalog is now core (#4),
owned by the control plane, which is its natural owner since it already needs model identity for the
cost ledger.

Engine lifecycle — start/stop/scale/quantize/multi-LoRA — is **explicitly out of contract** in v0.1
(A2). The five candidate engines implement it completely differently and there is no shared standard.
Process lifecycle belongs to the Compute contract; quantization and multi-LoRA are surfaced as
per-engine capability flags in the known-gaps ledger. We shrank the contract to the part that is
genuinely shared, rather than pretending a standard exists.

### 3.4 The three core/swappable splits, and why each split is where it is

**Cache: core key derivation, swappable store.** A semantic cache spanning chat, coding agent and
internal apps is a day-one savings source *and* a day-one multi-tenant security surface. NA3 posed
the horns: a global cache defeats per-department scoping; a partitioned cache guts the cross-surface
hit rate the savings lean on.

**Ruling: take the partitioned horn.** Cache keys are derived by a core function from
`(scope_hash, normalized_request)`; the store sees a physically namespaced key prefix per scope.
Cross-scope hits are impossible **by construction**, not by predicate. The reason is not primarily
security posture, it is verifiability (§8): a human can confirm partition-by-construction by reading
one function; confirming that a similarity threshold never leaks across departments requires
reasoning about every retrieval path forever.

Cost of this ruling, stated plainly: **cross-scope dedup is forfeited.** The cache's realized hit
rate will be below S20's blended 15–35% general-enterprise estimate, and hit rate must be reported
**per scope**, never globally. Cache-ON by default for chat; cache-OFF by default for the coding
surface pending eval evidence, because a "close enough" cached diff can be actively wrong.

**State: welded.** Transactional-store portability is the single most reliably project-eating
abstraction in this industry. Writing to a lowest-common-denominator SQL contract inherits exactly
what we accuse ActiveRecord of. State is Postgres and we say so. *Admitting one welded piece makes
the other eleven credible; twelve ports where one is fiction is worse than eleven ports that are
real.*

This also resolves A5 (a swapped store passing functional CRUD conformance while silently dropping
durability the audit trail depends on): **anything with a durability requirement lives in State.**
The audit trail, the capture ledger, the metering ledger and the falsification ledger are all on the
welded component. The cache store, which is swappable, is required to be *reconstructible* — a cache
outage degrades cost, never correctness or availability.

**Eval: harness swappable, gate core.** A6 was right that the brief's framing ("consumes a test set,
produces a verdict") is a stateless pure function, while what the design actually needs is a live,
stateful, continuously-resampled, privacy-scoped pipeline gating every model swap with automatic
rollback. Both are needed, so split them:

- **Eval harness** — a Job. Stateless, conformance-testable, swappable, bring-your-own.
- **Eval gate** — core. Owns sampling, scope enforcement on the test set, paired shadow comparison,
  the non-inferiority decision, promotion and automatic rollback. Runs in a separate process with
  separate credentials from anything it gates. Its supremacy is **structural**, not policy: the
  promoting actor has no write path to the gate.

### 3.5 Conformance: what each suite asserts, and who owns it

**Ownership ruling: conformance suites are invariant.** A component author may never amend a suite to
make their component pass — if they can, the suite is worthless. Mechanically:

- Suites live in a protected directory under CODEOWNERS.
- An L3 CI gate auto-rejects any PR touching both `contracts/*/suite/**` and an implementation.
- Suite amendment requires the same gate as a spec change: recorded rationale, contract version bump,
  and a **re-run of every previously-passing implementation against the amended suite.** A suite
  amendment that breaks a previously-conformant component is itself a finding, not a fait accompli.

Per DAP §3.3, prompt-level enforcement (L0–L2) fails predictably under completion pressure. This is
L3/L4 or it does not exist.

| Contract | The suite must assert (non-exhaustive; each is executable) |
|---|---|
| Chat surface | Delegates auth (no first-party credential accepted); every request bears the control-plane principal; emits valid OTel GenAI + `x_signal.*`; catalog is pushed by the control plane, not self-discovered; admin actions available through the control-plane API. |
| Coding agent | Upstream endpoint is redirectable to the gateway; accepts a rotatable minted key; **git/CI shim observes diff and test outcomes from outside the tool** against a fixture repo. |
| Serving engine | OpenAI-compatible inference; `/health`; capacity and queue-depth introspection in the declared metric shape; **a user over budget receives a 429 from the gateway before this engine sees the request.** |
| Model catalog & routing | (core) Catalog entries carry price, per-path feature fingerprint, latency fingerprint, residency label, terms class, commitment state. No entry may lack a fingerprint. |
| Provider path | Auth; declared rate limits observed; **automated fingerprint probe** confirms declared features are actually present on the live path; declared capture policy honored; price recorded at request time. |
| Router | (core) Determinism given `(request, weights, path set, clock)`; **PermittedPaths is enforced as a type boundary — no weight assignment can admit an excluded path**; empty permitted set fails closed with a distinct error and never falls back. |
| Cache | Scope-partitioned keys; **zero cross-scope hits under the adversarial harness** (crafted near-duplicate embeddings, timing probes, poisoning attempts); bounded timing differential; cache loss degrades cost only. |
| Compute | Node register/health/capacity; lifecycle operations idempotent; declared durability and isolation class recorded in the known-gaps ledger. |
| Identity | OIDC/SAML/LDAP; SCIM provisioning; **token exchange** produces a control-plane principal; no component other than the gateway validates tokens. |
| State | (welded) Not a swap suite. Migration, backup, restore and hash-chain integrity checks run as invariants. |
| Trainer | Dataset in / artifact out; **fails conformance if it consumes a dataset lacking `consented_scope`**; fails if it consumes rows absent from the capture ledger; resumable and idempotent; artifact content-addressed and reproducible from `(dataset, config)`. |
| Eval harness | Test set in / verdict out; scope enforcement on the test set; deterministic given a fixed sample; **cannot write to the gate.** |
| Signal (cross-cutting) | Valid OTel GenAI semconv; `x_signal.outcome ∈ {accept, reject, regenerate, test_pass, test_fail, commit_stuck}`; carried as span attributes/events on existing `gen_ai.*` spans. Testable with off-the-shelf OTel tooling. |
| Console | Renders from the core query API only; every action goes through the core action API; **no export path emits a netted savings number without its term decomposition** (§5.4). |

**Interaction suites.** A3 was correct that per-contract conformance cannot catch cross-contract
failures. Build a deliberately short, explicitly enumerated set:

1. Serving × Trainer — accelerator memory and driver-lock contention.
2. Router × Cache — scope isolation across the routing decision.
3. Identity × Gateway — token exchange and validation under rotation and revocation.
4. Eval gate × Trainer — supremacy: the trainer cannot weaken, skip, or write to the gate.
5. Provider path × Router — feature fingerprint currency; a diverged path is auto-excluded from
   `PermittedPaths` for requests requiring the diverged feature.

The enumeration is incomplete by construction. That is accepted and recorded, not hidden.

**Known-gaps ledger.** Every swap option surveyed had at least one undocumented or roadmap-only gap
discoverable only by primary-source reading. A per-swap-option known-gaps ledger ships from day one,
or customers find these in production instead of in the docs.

**Third-party submissions.** Publish the suites; let component authors self-certify for a badge their
users will ask for. This does three things at once: it offloads integration labor onto the people who
already maintain those components; it is an **adoption ledger that respects air-gap absolutely**
(§9, C10); and every submission tells us where our contracts are wrong at zero data cost.

### 3.6 Component defaults and the tier table

Defaults are settled in the brief and are not reopened. Selection rule: **OSI-approved with no
user-count, revenue, seat or feature trigger.** Source-available and open-core are disqualifying *as
defaults*; they may appear as documented swap options with the constraint noted. A component whose
*contracted capability* sits behind a paid tier fails the rule even if its core is permissive.

Standing constraints carried from the licensing audit, so they are not silently reintroduced:

- **Cline base extension only, *if* Cline.** Enterprise tier permanently out of scope. The default
  itself was superseded during v0.1-dogfood — see the ruling immediately below.
- **The default coding agent is opencode (MIT), served in a browser terminal over ttyd (MIT), with
  aider (Apache-2.0) retained as an installed fallback.** Cline lost on *shape*, not on licence: it
  is an IDE extension, and v0.1's coding surface has to be reachable from a browser by a user who
  installs nothing. The losing argument is worth stating because it was good — Cline is the tool a
  professional developer already has, and "nothing about the coding experience changes, only the
  pipe" (§7.2) is the strongest version of the intermediation pitch. It still holds for a customer
  bringing their own tool, and the base-URL pass-through contract is unchanged. What it could not
  do is be the *bundled default*, because a bundle whose coding surface requires a local IDE install
  is not one login to three surfaces. aider was the first replacement and lost to opencode on
  behaviour: it requires the user to nominate files before it will act, and finding 23 measured it
  silently discarding a completed edit when the model named a file outside the chat. It stays
  installed because a known-good fallback measured against this gateway costs one binary.
  **Consequence for the tier table below: the coding agent remains tier 3** — this changes which
  agent, not what it delegates.
- **LiteLLM MIT core only.** Its `enterprise/` directory gates SSO, RBAC and audit behind a key; we
  build those anyway. Do not take a dependency on that directory.
- **Valkey over Redis.** Redis is tri-licensed since 8.0 and its default packaging steers to SSPL and
  RSAL, neither OSI-approved.
- **Perses over Grafana** as the default, which removes the AGPL judgment call entirely. Grafana as a
  swap is *configured*, never patched.
- **LibreChat stays the chat surface, and the paywall objection against it has expired.** Re-examined
  2026-07-29 because the objection was strong: its code interpreter was its own paid hosted API, and
  its web search steered to Firecrawl, Jina and Cohere. Three things resolve it. **First, the rule
  was never violated:** the chat-surface *contract* is "OpenAI-compatible client, delegated auth,
  emits usage and signal", and every part of that is in the MIT core. Code execution is not in the
  contract, so a paid interpreter is a paid *adjacent* product, not a gated contracted capability.
  The distinction matters and is the difference between this and Cline, whose gated features are
  fleet auth and telemetry export — squarely inside its contract. **Second, the interpreter was open
  sourced** as `ClickHouse/code-interpreter` under Apache 2.0, self-hostable by compose or Helm, with
  NsJail and libkrun microVM isolation modes. **Third, only the search leg of web search is
  mandatory** — `rerankerType: "none"` is supported and the scraper is pluggable at a URL — so
  SearXNG alone satisfies the required part. The general lesson is worth keeping: **the paywall in
  this category is not in the chat UI, it is in anti-bot scraping infrastructure**, which every
  alternative also has to buy or do without. Switching surfaces would not have bought a free
  scraper. Self-hosted Firecrawl is AGPL-3.0 with a closed-source anti-bot engine, so it is a
  documented swap under the same HTTP-separation rule as SearXNG and Grafana, never a patched
  dependency.
- **Open WebUI is not the default.** It moved off BSD-3 in April 2025 to a source-available license
  with a branding-retention clause; a deployment over 50 users wanting branding removed needs a
  second commercial relationship. It remains a documented swap with the constraint disclosed in
  bundle docs rather than discovered by a procurement team.

Conformance tier is a per-component property. Publish it as a table; do not claim a uniform tier for
the bundle.

| Tier | Meaning |
|---|---|
| **1** | Delegates auth, emits usage+signal, administered through the control plane, exposes adaptable parameters. |
| **2** | Delegates auth and emits usage+signal, but retains its own configuration surface. Behind the identity-aware proxy and the shared nav shell, so the operator sees a second *view*, never a second *login*. |
| **3** | Auth is simulated by control-plane-minted credentials; signal is captured from outside the tool. The coding agent sits here, and this is the honest tier for every OSS coding agent surveyed. |

---

## 4. The router and the control loop

Q1 — "does the router decide or advise?" — is **CLOSED: it decides.** The advisory position lost, and
the reason is not preference. Under intermediation, deciding is justified by already-contracted path
selection on day one with zero local capacity and zero quality risk. **There is therefore no phase in
which advisory is the rational stopping point**, which means the calcification risk the question was
guarding against is *removed*, not mitigated. An advisory router is Product → Human → Product relay
executed manually, forever — a product with a dashboard instead of a metabolism.

### 4.1 Actuation axes versus objective terms

These are different things and conflating them is a common error. Name them separately.

**Actuation axes** — what the router can change. Four exist before any GPU:

1. **Provider path** for the same logical model (direct / Bedrock / Vertex / aggregator / regional).
2. **Cache** — serve from cache or not.
3. **Dedup** — coalesce against an identical in-flight request.
4. **Model tier within frontier** — the cheapest model that clears the class's quality bar.

A fifth appears in v0.2: **local capacity**, which is *just another path*. Adding local serving adds
entries to the catalog. It does not change the router.

**Objective terms** — what the router scores. Four:

1. **Cost** — hard live value, priced at request time.
2. **Latency** — hard live value, p50/p95/p99 per path, rolling.
3. **Quality deficit** — **proxy only** until paired shadow eval lands (strong on coding via test-pass,
   weak elsewhere); hard thereafter.
4. **Rate-limit headroom penalty** — §4.4.

### 4.2 The decision function

```
PermittedPaths(r) = { p ∈ Paths :
      scope_class(r)        ∈ p.permitted_scopes
  ∧   residency_req(r)      ⊆ p.residency
  ∧   required_features(r)  ⊆ p.verified_features
  ∧   terms_class(r)        ∈ p.allowed_terms }

route(r) = argmin over p ∈ PermittedPaths(r) of
             W(t) · [ cost(r,p), latency(r,p), quality_deficit(r,p), headroom_penalty(p) ]
             + shortfall_penalty(p)
```

`W(t)` is the operator-set weight vector — the customer's platform team saying *"this quarter, weight
cost over latency."* Its absence in the brief is why the router previously had nothing to optimize
against. It is set through the control plane, versioned, and every change is a gated promotion (§4.5).

**If `PermittedPaths(r)` is empty, the request fails closed with a distinct error.** There is no
fallback. A router that falls back out of a scope filter has no scope filter.

### 4.3 Commitment awareness

A27 is the sharpest surviving attack on the savings story and it needs a mechanism, not a caveat. A
customer at ~$500k/yr has committed minimums with true-up penalties and a negotiated discount better
than list on any alternate path. Routing volume *away* from the committed path does not count toward
the minimum and abandons the discount.

So the catalog carries per-path commitment state — `minimum`, `consumed_to_date`, `period_end`,
`shortfall_rate` — and the objective carries an explicit `shortfall_penalty(p)`: the marginal expected
true-up cost of under-consuming that path, given current pace. Routing away from a committed path has
a real price and the optimizer must **see** it rather than discover it at period close.

This is what "route on price" actually means for this customer segment, and the levers are the ones
S16 identified as replacing the dead arbitrage story:

- Provider **prompt-cache eligibility** (up to ~90% off input tokens) — largely a *request-shaping*
  decision, therefore a router job.
- **Batch eligibility** (~50% off) for latency-tolerant classes.
- **Committed-discount path preference**, weighted by shortfall.
- **Avoiding the residency premium** (~10%) where compliance does not require it.
- **Tier selection within frontier.**

### 4.4 The hard privacy-scope constraint, and why it is outside the optimizer

Privacy scope is a **filter, not a weight.** It must never enter the weighted sum.

The reason, stated as sharply as the purist put it: *a router that trades privacy scope against cost
will occasionally route regulated or department-scoped data down the cheap path, **and be correct by
its own objective function every time it does.*** There is no weight setting that fixes this, because
the failure is not a mis-weighting — it is a category error about what kind of thing a scope is.

Implementation requirements, and these are structural rather than procedural:

- **One scope function.** A single pure function, no I/O, with **three consumers**: the router, the
  factory, and per-person telemetry. No consumer implements its own check. If it is scattered, no
  exhaustive table exists and every call site needs review forever (§8).
- **Type boundary, not predicate.** `PermittedPaths` returns a set. The optimizer's *input type* is
  the filtered set. It is structurally unable to select an excluded path, rather than merely
  instructed not to.
- **Exhaustive table test.** The cross product of (scope class × path × residency × terms class) is
  enumerated and asserted in a table a human reads once.
- **Residency degenerates correctly.** A30 observed that residency turns "cheapest path" into "the
  only legal path." That is not a degeneration, it is the specified behavior.

Same machinery, three consumers — the same scope model the enterprise checklist already requires for
per-department training scopes, and the same one that governs aggregate-only telemetry mode.

### 4.5 Rate-limit headroom with hysteresis

Nobody but the purist mentioned this and it is a real controller defect: **a controller minimizing
price piles onto the cheapest path until it 429s, backs off, and piles on again.** Textbook control
oscillation, visible to users as latency variance, indistinguishable from a broken product.

Specification:

- `u(p)` = observed request/token rate over a sliding window ÷ provisioned limit for that path.
- `headroom_penalty(p)` engages as a **Schmitt trigger**: engage when `u > u_hi` (default 0.85),
  release only when `u < u_lo` (default 0.65). Never a single threshold.
- **Minimum dwell time in each state, measured in request counts, not wall-clock.** Request counts are
  the correct unit throughout this design (§5.5, §9).
- A 429 from a path triggers **immediate demotion** with an exponentially-decaying penalty, independent
  of the sliding-window estimate.
- **Randomized tie-breaking**: among paths within ε of the argmin, select proportional to headroom.
  Prevents knife-edge flapping between two near-equal paths.
- Per-path token-bucket admission at the gateway, so headroom is enforced and not merely preferred.

Rate-limit **concentration** remains real (S19a): RPM/TPM are enforced at the org/API-key level and are
not summed across virtual keys, so employees who each had their own seat capacity now share one
upstream key's budget. The only real mitigation is the negotiated dedicated-capacity conversation
enterprises already have with vendor sales. Engineering's contribution is to make headroom utilization
a first-class reported line so procurement walks into that conversation with data.

### 4.6 Latency budget

A25 is accepted as a real risk: our gateway is now a mandatory hop between every coding-agent call and
the provider, on a fresh unhardened control plane, from day one. Under the old framing a defecting
developer cost one signal source; under "all spend flows through the layer" a developer routing around
it **breaks the universal-visibility premise the product is sold on.**

Hard design budget, asserted as a conformance property of the gateway itself and published live in the
console:

| Metric | Budget |
|---|---|
| Added p50 latency, pass-through, excluding upstream time | ≤ 15 ms |
| Added p99 latency, pass-through, excluding upstream time | ≤ 50 ms |
| Added time-to-first-token | ≤ 25 ms |

Streaming must be **true pass-through**. Metering reads the stream as it passes; it never buffers a
response to meter it. Buffering a stream to count tokens is the single most likely way this budget is
blown.

Pair this with **ambient provenance disclosure**: a small marker on every answer showing
served-from-cache / local / frontier-via-path plus p50/p95 for that path. It makes any complaint
attributable instead of atmospheric, and it extends the verifiability claim to the end user rather
than only the auditor.

### 4.7 Promotion gate: paired shadow evaluation

Any change to the weight vector, the routing policy, or the admission of a new path or model into
`PermittedPaths` is a **promotion** and must clear a gate.

The gate is **paired shadow evaluation**, and it is the strongest capability intermediation creates.
Frontier is answering the request anyway. Shadow the candidate on the same input and compare against
the response that was **actually served and actually accepted or regenerated by a real user on real
work.**

The test set is not "the customer's traffic." It is the customer's traffic **with a frontier reference
response and a human verdict attached** — labeled, paired, continuously refreshed, generated as a
byproduct of serving at zero marginal cost.

This matters because of an asymmetry nobody drew until round 2: **the eval gate for the
first-migrating surfaces cannot be grounded in outcome data.** Coding has abundant objective signal
(tests pass or they do not) but the widest quality gap, so it clears its gate *last*. Chat clears
*first* and has the weakest signal to gate with. Q3 is therefore harder, not easier, for exactly the
workloads that move first. Paired shadow evaluation is the only mechanism that makes eval-gated
migration honest for the subjective surfaces.

**Special case: zero-quality-delta promotion.** Pure path arbitrage — same model, same version, same
verified feature set — has `quality_class` identical *by construction*. The gate is satisfied by a
declared equivalence assertion, which is itself a conformance assertion on the provider path
(fingerprint probe, §3.5). This is what lets the controller run on day one with zero quality risk.
One mechanism, two eras.

**Promotion mechanics:**
- Candidate runs in shadow over a sampled slice, scope-filtered identically to production.
- Promote when the paired comparison over a pre-declared request count shows non-inferiority within a
  declared margin **for that workload class**. Margins are per-class and are set by the operator.
- **Automatic rollback** on regression, to a versioned declared-good config snapshot covering the
  weight vector, routing policy and active model versions — with **one** "rollback to last-good" lever
  regardless of what changed. At 3am the operator does not want a policy surface, they want one lever.

### 4.8 Breakglass (R3)

Egress control makes this layer a **company-wide single point of failure for all AI work.** Before, if
the layer died, frontier still worked. Now, if the layer dies, nobody in the company can use any AI at
all — and the 3am operator is not managing degraded local capacity, they are managing a total
company-wide AI outage with the CTO awake.

The fail-open path is required. Its design is dominated by one constraint: **its correctness must not
depend on the component it bypasses being alive.**

| Property | Specification |
|---|---|
| Credential | A **distinct** upstream provider key held in the customer's secret store and never used by the gateway. Separate key so breakglass spend is separable on the provider invoice — which is what keeps it auditable even while the control plane is down. |
| Activation | A standalone single-binary tool plus a documented manual procedure. Requires: an identity on a pre-provisioned break-glass roster (offline-verifiable — pre-distributed signing keys or out-of-band MFA), a **typed justification string with no default** (the tool refuses empty or whitespace), a declared duration ≤ `TTL_max` (default 4h), and a declared blast radius (which egress rule, which user set). |
| Record | Signed, hash-chained, append-only, written to durable local storage at activation — same ledger primitive as capture and falsification (§8.6). |
| Expiry | Enforced **at the network layer**. The rule carries a TTL and the job that applies it removes it. If expiry depended on the control plane, a dead control plane would mean a permanent hole. |
| Alarm | Page to a channel outside this layer (email/SMS/webhook, configured at install) plus a persistent console banner while active. |
| Renewal | Renewal is a **new activation with a new justification**, never an extension. Renewal count is itself a reported metric. |
| Enforcement level | L3/L4. **There is no `breakglass: always` setting.** A standing bypass requires editing the egress policy itself, which is visible in the customer's infrastructure-as-code. |

**The anti-normalization instrument.** A breakglass that quietly becomes the permanent path kills the
chokepoint premise without anyone noticing. So:

- **Breakglass minutes as a fraction of total AI-serving minutes** is a first-class reported metric
  with a pre-declared threshold (default 1% over a rolling request-count window).
- Above threshold, the console raises a governance finding.
- **The savings and visibility figures are annotated as covering only `(1 − breakglass_fraction)` of
  spend.** A report that silently excludes breakglass traffic overstates its own coverage.
- Breakglass spend appears on the provider invoice and not in our ledger; the reconciliation job
  attributes the delta to breakglass windows. An **unattributable** delta is the leak detector firing
  (§2.5).

### 4.9 Availability posture (A28 / NA1)

HA is day one, not a hardening milestone, because the mandate takes effect immediately. But scope it
honestly: v0.1 must be **available**, not highly-available in the multi-region sense.

- Gateway is **stateless and horizontally scalable** behind a load balancer, N ≥ 2.
- Postgres with a replica and a **tested, documented** failover.
- Cache is **optional by design** — a cache outage degrades cost, never availability.
- **Policy is fail-static, not fail-closed.** Each gateway caches control-plane policy in-process with
  a TTL, so a control-plane outage degrades to last-known-good policy rather than denying all traffic.
  This is the single highest-leverage availability decision in the design.
- Breakglass covers the residual.

State plainly in customer-facing material: without these, the availability posture is **worse** than
direct multi-vendor provider access. They are therefore v0.1 scope.

### 4.10 The 3am operator kit

DAP's operator audience was the largest hole in round 1: Tuesday quarterly planning was excellently
served and 3am was not served at all. Four mechanisms, all reusing infrastructure v0.1 already builds:

1. **Declared per-tenant degradation ladder** — primary → fallback-1 → fallback-2 → fail-closed/open,
   surfaced as a live traffic-light view of what every workload is *actually* running on versus its
   declared preference. This is the one genuinely live operational view in an otherwise retrospective
   design.
2. **Health-driven spill** — the serving contract's required introspection feeds the same weight vector.
3. **"What changed"** — a relative-time panel: now versus 1h / 24h / 7d across error rate by path, p95
   by tier, escalation rate, cache-hit rate, spend rate.
4. **Universal rollback** — the model-swap rollback generalized to a versioned declared-good config
   snapshot, one lever (§4.7).

---

## 5. Metering, reporting, and the savings decomposition

### 5.1 Visibility parity is an adoption gate

No org will leave Anthropic for something that shows them less than Anthropic does. The bar is public,
documented, and higher than casual expectation. Any evaluating admin will open both dashboards side by
side. Treat every line as a hard requirement; missing any single one is a reason to stay.

Breakdowns by model, by workspace/project, by API key, by individual user. Token detail split into
uncached-input / cached-input / cache-creation / output, because that is how the bill is actually
computed. Time buckets down to 1m. Freshness within minutes. Export by API, not just dashboard.
Numbers that reconcile to the invoice.

Two ingestion caveats found by primary-source reading, both of which change build scope:

- **Claude Platform on AWS does not support the programmatic usage endpoints at all** — console only.
  Matters when a customer's Anthropic spend runs through Bedrock or Marketplace rather than direct.
- **Claude Enterprise (claude.ai seats) uses a separate Analytics API and key from Claude Platform.**
  A real "before" state may require ingesting **two** different Anthropic APIs.

OpenAI's endpoints group by project/user/api-key/model/batch/service-tier but document 1d default
granularity and are operationally younger.

Then exceed the bar, because we structurally can: we see **people** (SSO tells us it was Sarah in
Platform Engineering, not `sk-…7f3`); we see **every provider plus owned and rented capacity in one
view**, which no vendor can ever offer and is the only place a savings number can honestly live; we see
**surfaces** (chat vs coding agent vs internal app), which is what tells an operator where the money
goes; and we have **outcome data**, which nobody's enterprise tier provides.

**Intermediation makes parity materially easier, not harder.** Because 100% of frontier spend flows
through the gateway by construction, the provider usage APIs stop being the source of truth for a
partially-bypassed system and become a **reconciliation check** against our own metering. The hardest
part of the original analysis — counterfactually estimating "spend that would have gone to Anthropic"
across a mid-migration cutover — largely dissolves.

### 5.2 The metering record

This is the most consequential data-structure decision in the design, because reconciliation, the
savings decomposition, the escalation metric and the corpus all read from it.

Requirements, each with a reason:

- **Request granularity.** You cannot reconcile a bill from aggregates.
- **Every field the invoice is computed from**: model, provider path, service tier, token class
  (uncached input / cached input / cache creation / output), batch flag, region, timestamp.
- **Priced at write time.** The record carries the computed amount, not a foreign key into a mutable
  price table. A rate-table change must not silently rewrite history or break reconciliation.
- **Append-only and immutable.** Derived views are rebuilt from it and are never authoritative.
- **`logical_request_id` plus an attempt sequence.** This is the resolution of A9's residue: cost
  aggregates over *attempts*; savings and escalation metrics aggregate over *logical requests*. A retry
  that crosses paths is one logical request and two priced attempts.
- **Scope, workload class, principal, surface, and capture-eligibility decision** on every row — so the
  corpus is joinable later without a backfill (§6.4).

### 5.3 Evidentiary tiers on every dollar

A10 is accepted as a permanent epistemic asymmetry rather than resolved. Frontier spend has a metered
ground truth; owned and rented capacity has a *modeled allocation*. Rather than pretend parity, label
it:

| Tier | Meaning | Applies to |
|---|---|---|
| **T1 — receipt** | A real invoice was paid for exactly this. Zero estimation. | Every request routed to a frontier path. |
| **T2 — calibrated estimate** | Anchored to **our own recently observed effective per-token price** for that model and path — never a static list rate card. | Cache hits, deduped requests. |
| **T3 — anchored projection** | Anchored to that exact workload class's real frontier cost immediately before it crossed its eval gate. | Post-migration local traffic. |

For owned hardware, the model's assumptions — depreciation schedule, power rate, cooling, utilization,
staff allocation — are **displayed as editable inputs**, so the CFO owns them rather than discovering
them. There is no neutral answer to capex amortization and pretending otherwise is what loses finance
teams.

**Factory cost never nets into savings.** Under rent-first plus intermediation, the early state is zero
owned capacity, all traffic frontier, corpus accumulating, and training compute **rented** — a cash
line item, not recovered waste. Routing/cache savings and factory cost are separate lines or a positive
number hides a factory that is not yet earning. "Idle capacity becomes training capacity" is true only
*after* owned hardware exists, and even then only with the marginal cost of filling the trough
(principally power) shown — idle capacity is free only if power is free, and on an 8×H200 it is not.

### 5.4 The seven-term decomposition

This is where the headline claim lives or dies, and the failure mode is specific and dangerous:

> **Routing and caching savings are STEP functions. The factory is a SLOPE. A savings report showing a
> large day-one step followed by a flat line has disproven the central claim while looking like a
> triumph.**

That is the same success/failure-indistinguishable pattern the deliberation flagged in round 1,
reappearing in a place where it would be read as vindication.

Every reporting period, decompose the spend delta into seven terms. Each is computed **holding the
other six at prior-period values**, in a **fixed, declared order that is never re-ordered to flatter a
result** — attribution of interaction terms depends on order, so fixing the order is the honesty
mechanism.

| # | Term | Cause | Shape | What it supports |
|---|---|---|---|---|
| 1 | `Δ_rate` | Provider rate-table changes | — | **Nothing.** Exogenous. |
| 2 | `Δ_volume` | Total request count | — | **Nothing.** Exogenous. |
| 3 | `Δ_mix` | Workload-class composition shift | — | **Nothing.** Exogenous. |
| 4 | `Δ_route` | Routing decisions among frontier paths and tiers | **STEP** | An engineering win. Not the thesis. |
| 5 | `Δ_cache` | Cache hits and in-flight dedup | **STEP** | An engineering win. Not the thesis. |
| 6 | `Δ_local` | Traffic served locally that would otherwise have gone to frontier | **SLOPE** | **The claim.** |
| 7 | `ε` | Residual | — | Must stay small. |

Rules, all enforceable in code:

- **Residual threshold.** `|ε| ≤ 5%` of `|ΔS|`. Above it, the model is wrong and the report says so
  rather than absorbing the difference.
- **`Δ_local` is displayed from day one, as `$0`.** The flat line must be visible from the beginning so
  the step-vs-slope confound cannot hide behind an absent series.
- **No netted headline without the terms.** The net figure and the stacked decomposition are the same
  panel. There is **no export path** that emits the net without the terms — asserted in the console
  conformance suite (§3.5).
- A bill that declines because the CFO cut headcount validates nothing; that is what terms 1–3 exist
  to strip out.

### 5.5 The primary instrument: escalation rate

`escalation_rate(class, window)` = fraction of logical requests in workload class `c` served by a
frontier path rather than local capacity, **excluding the anchor budget** (§6.3).

- **Windows are fixed request-volume deciles, not time.** Every window covers the same request count
  for that class. This removes traffic seasonality as a confound and makes windows comparable across
  classes and across customers. Use event counts or monotonic sequence positions throughout; no
  calendar anywhere in the evidence layer.
- Record it during the pre-local era, where it is `1.0` by construction. That establishes the baseline
  and makes the first decline meaningful rather than an artifact of when measurement started.
- Excluding the anchor budget is essential: an operator-set anchor floor would otherwise mask the
  metric it exists to protect.

**Falsification (C5):** escalation rate flat or rising across two consecutive windows after that class
cleared its eval gate, with mix controlled → the factory premise is false for that class.

### 5.6 The 90% rule

> **If `(Δ_route + Δ_cache)` remains ≥ 90% of cumulative savings indefinitely, the factory thesis is
> not supported. The product is a cost-routing proxy — a good product — and the report must say so in
> those words.**

This must be a rendered panel with the threshold pre-registered in the falsification ledger *before any
customer data exists*, not a footnote added after the data disappoints. See §9, C6.

### 5.7 Privacy exposure of the sharpest differentiator

"We see people, not `api_key_id`" is simultaneously the sharpest differentiator and the sharpest
privacy exposure. EU works councils and GDPR make per-person behavioral telemetry over employee work a
**procurement blocker in exactly the enterprises most likely to buy this.**

Required: per-person attribution is a **scoped, configurable capability** with a genuine
**aggregate-only mode** that degrades the dashboards rather than disabling them. Same scope model,
third consumer.

---

## 6. The factory

Deferred past v0.1. Specified here so that v0.1 does not calcify against it.

### 6.1 The loop

Behavioral signal → corpus → train → **eval gate with automatic rollback** → promote. No human in the
path. The gate is a DAP invariant test with supremacy: it sits outside the trainer's optimization loop
and the trainer cannot weaken it. This is promoted from an open question to a decided mechanism.

- **Behavioral signal is the foundation.** Accept/reject, regeneration, diff accepted, tests passed,
  commit stuck or reverted. First-party, ground-truthed, better than thumbs. Produced by **all**
  traffic including locally-served traffic. **The loop must work in an air-gapped deployment with no
  frontier tier at all.**
- **The escalation corpus is an accelerant, not the foundation.** Every request routed to a frontier
  model is paid for anyway; capturing the pair turns the API bill into a labeling budget. Under
  intermediation this applies to 100% of traffic from day one.
- **Realistic output** is small specialized models that beat big general ones on this customer's work —
  a 14B excellent at their ticket triage and code conventions. Nobody out-trains a frontier lab on one
  rack. That size class runs on commodity hardware, which is the point (§1.1).

Note for internal consumption only, and it should not enter the pitch: at the routing layer "frontier
is a tier, not the enemy" is straightforwardly true. At the **corpus** layer, the design converts
frontier providers into unwitting teachers for the models that displace them, financed by the
incumbent's own revenue. Keep the pitch neutral; do not let internal design documents believe the
pitch.

### 6.2 Paired shadow evaluation

Specified in §4.7. It is the factory's promotion mechanism as well as the router's.

### 6.3 The anchor budget — resolving A12

A12 was the deliberation's cleverest attack: the declining-bill metric and the improving-model metric
**cap each other.** Escalations are simultaneously the thing whose decline is sold as success *and* the
corpus supply for training. Success starves its own raw material.

The attack is half right, and the half that survives needs a mechanism.

What is wrong with it: the corpus is not only escalations. The factory's foundation is first-party
behavioral signal, produced by all traffic including local traffic. That supply *grows* as migration
proceeds.

What survives: the **frontier reference** supply — the paired-shadow anchor and the external quality
anchor against own-output degradation — does decay as the claim succeeds.

**Ruling: a declared anchor budget.** A fixed minimum fraction of traffic per class (default 1–5%,
operator-set) is routed to frontier **regardless of the eval verdict**, purely to maintain the reference
distribution. Consequences, all deliberate:

- It sets a **floor** on escalation rate rather than allowing it to go to zero. That floor is honest and
  should be stated up front; a product claiming zero frontier dependency is claiming it has stopped
  measuring itself.
- Its cost is a **named line** in the savings report ("anchor cost"), not a failure to save.
- Escalation rate is reported **excluding** the anchor budget (§5.5), so the floor never masks the metric.
- It is the standing answer to own-output degradation in any deployment that has a frontier tier.

### 6.4 Air-gap is a strictly degraded subset

Intermediation introduced an asymmetry that must be stated: air-gapped mode is **no longer co-equal**.
It has no path arbitrage, no shadow evaluation, no external anchor, no paired corpus, and no anchor
budget. **Every model-collapse concern in this design is concentrated there.**

Therefore, elevated from recommendation to decided: **a frozen, human-curated, held-out eval set the
factory can never touch or extend.** In air-gapped deployments it is the **only exogenous input in the
entire system.** Q4 is now exclusively an air-gap question.

Who curates it and how it is kept representative is an open gap (§11).

### 6.5 Serve-and-train concurrency

Q2 is deferred past v0.1 (no GPUs) and the design position is recorded so the utilization argument is
not asserted as settled:

- Training is a **strictly lower-priority partition** (MIG/MPS) that **yields the instant a serving
  request queues** — a background process that yields, not a batch job that claims the card.
- Checkpoint granularity must make preemption cheap. A long run that cannot be preempted collides with
  an 8am office start, and the choice between killing the night's compute and delaying morning
  inference is the one the design must never force.
- **No part of the design may assume saturation.** The 25–30% corporate duty cycle is an *input* to the
  buy-vs-rent model, not a constant, and it does not survive a multinational customer (A11b).

### 6.6 Data provenance and training scope

- **Scope is a contract precondition, not a policy document.** The Trainer contract's input signature
  **requires** `consented_scope` on every dataset; an implementation that ignores it fails conformance.
  Mixing scopes becomes a contract violation the suite catches, not a governance policy depending on an
  admin remembering a flag. This resolves the brief's apparent self-contradiction (A16): we ship the
  mechanism, the operator sets the policy. One model trained on all internal traffic redistributes data
  across departments *through weights* — a path that did not exist before — so the mechanism is
  required; imposing a particular policy is not our call.
- **Capture policy is per-provider-path and configurable, defaulting off for any path whose terms
  restrict training use.** The capture ledger records the provider path of every captured pair, so a
  customer's counsel can answer the question in **one query** rather than a forensic exercise. Cheap
  designed in, expensive retrofitted.
- For the record, since the operator has already ruled and the design does not depend on it: Anthropic's
  commercial terms §D.4 expressly prohibit using the Services to build a competing product "including to
  train competing AI models." Recorded, not litigated. The risk lands on the deploying customer, and the
  design's response is to make the situation **visible and configurable** rather than silent.
- **Verifiable collection is an L4 circuit breaker, not an audit feature.** The trainer cannot consume a
  dataset absent from the hash-chained ledger, enforced in code. Promote it to a headline product
  property alongside no-phone-home: it is the artifact that lets a customer prove what they trained on
  and from where.

### 6.7 What v0.1 must contain so the factory is not blocked later

Each item below is cheap now and expensive or impossible to backfill.

1. **Metering record carries scope, workload class, `logical_request_id`, provider path, and capture
   eligibility** — so the corpus is joinable without a migration.
2. **Capture ledger exists and runs**, hash-chained, with per-path policy, even though nothing trains.
3. **Signal schema and the git/CI outcome shim ship in v0.1**, so coding outcomes — the highest-quality
   training signal — accrue from day one.
4. **Paired references are stored.** On a sampled slice, v0.1 stores the frontier reference response and
   the human verdict. **This is the single most important non-calcification item**: without it, v0.2
   begins with zero eval data and the structural pull in §7.3 does not exist.
5. **The scope function has a trainer consumer stub**, and the dataset type already carries
   `consented_scope`.
6. **The router's path set can already contain local paths.** Adding local capacity is adding catalog
   entries, not changing the router.
7. **`Δ_local` is in the report from day one at `$0`** (§5.4).
8. **The workload-class taxonomy exists**, because escalation rate is per class.
9. **Anchor budget is a config knob**, default 100% frontier, meaningful in v0.2.

---

## 7. v0.1 scope, exactly

### 7.1 Step 0 — the savings-audit CLI

The read-only front of the funnel, and the first engineering.

An IT admin runs it against their own Anthropic Console/Admin API export and OpenAI usage export.
Entirely read-only. Nothing installed, no GPU, no gateway, no control plane, no data leaves the
building, no procurement approval needed. Output: the board-deck savings projection from their actual
last 90 days, with every figure tier-labeled (§5.3) and every term decomposed (§5.4) — including the
terms that are `$0` because nothing has been deployed.

Why it is in scope rather than marketing:

- It **is** the provider-usage-ingestion component the real system needs anyway. Build order and
  go-to-market collapse into one artifact; nothing is thrown away.
- It is self-demonstrating: the number it produces *is* the pitch.
- A founder with no funding and no phone-home telemetry has no conventional way to generate leads. A
  five-minute read-only on-premise tool is the only kind of artifact that can spread inside an
  enterprise that has never heard of us.
- The same codepath pointed at live traffic becomes the running capex countdown post-deployment.

It must handle the two ingestion caveats in §5.1 (Bedrock/Marketplace spend has no programmatic
endpoint; claude.ai seats use a separate API), and must degrade gracefully — reporting *what it could
not see* rather than silently under-counting.

### 7.2 Step 1 — the intermediation deploy

This is v0.1 proper. **No Serving, Compute, Trainer or Eval-harness contract needs to exist.** No GPU
risk, no quality risk, no migration risk. It is a pure finance-and-procurement decision that produces
the savings number as **reconciled fact rather than projection**, and it is a fully monetizable
standalone product — a customer can stop here forever and the pitch is still true.

**In scope:**

| Component | Specification |
|---|---|
| **Gateway** | OpenAI-compatible and Anthropic-native inbound. Sole token validator. Single quota/budget admission point. Meters every request per §5.2. Stateless, N ≥ 2. Latency budget per §4.6. True streaming pass-through. |
| **Identity** | Keycloak or Ory. OIDC/SAML/LDAP + SCIM. Token exchange → control-plane principal → minted, rotatable per-user virtual key. |
| **Virtual keys** | Minted against the customer's own upstream keys under their own contracts. Per-key/user/team/org budgets. Rotation and revocation. |
| **Model catalog & routing table** | Core. Price, per-path feature fingerprint, latency fingerprint, residency label, terms class, commitment state. |
| **Provider path adapters** | Anthropic direct, OpenAI direct, Bedrock, Vertex. Automated fingerprint probe on a schedule. |
| **Router** | Closed-loop controller per §4. Four actuation axes, four objective terms, hard scope filter outside the optimizer, hysteresis, commitment awareness, promotion gate. |
| **Cache** | Exact + semantic. Core-owned scope-partitioned key derivation. Valkey-backed. On for chat, off for coding by default. Adversarial harness. |
| **State** | Postgres, welded. Metering ledger, capture ledger, audit trail, falsification ledger, config snapshots. |
| **Capture ledger** | Hash-chained, append-only, per-path capture policy, paired-reference storage on a sampled slice. |
| **Signal** | Additive namespace on OTel GenAI semconv. `x_signal.outcome` enum. |
| **Git/CI outcome shim** | Agent-agnostic diff and test-outcome capture, verified against fixture repos. |
| **Provider usage ingestion + reconciliation** | Continuous. Reconciles to the cent against a real invoice. Redacted real-invoice fixtures in the repo. Doubles as the leak detector (§2.5). |
| **Console** | Perses over the core query API + a thin action API for what dashboards cannot do (enforce a budget stop, disable a user, gate a promotion, roll back). Parity panels (§5.1), decomposition panel (§5.4), 3am kit (§4.10). |
| **Chat surface** | LibreChat, SSO-delegated, catalog pushed by the control plane. |
| **Coding agent** | Base-URL redirect pass-through to the gateway. The customer keeps their existing tool. **Nothing about the coding experience changes — only the pipe.** Plus a *bundled* default for customers who have no existing tool: opencode in a browser terminal over ttyd, per the §3.6 ruling. Pass-through and bundled default are two answers to two different customers, not a change of position. |
| **Egress control** | Documented deployment precondition, three supported mechanisms, coverage metric. |
| **Breakglass** | Per §4.8, in full. Not deferrable — the egress mandate creates the failure mode on day one. |
| **Availability** | Per §4.9, in full. Same reasoning. |
| **Exit path** | A tested, CI-verified procedure: export the ledger, revoke virtual keys, restore direct keys, remove egress rules. This is the anti-lock-in mechanism and it answers NA2 in the only way a license cannot. |
| **Reference bundle** | One command. Everything above, plus fakes for every contract so the system is testable end to end without GPUs. |
| **Conformance** | Four generic harnesses (§3.2) + per-contract assertion sets + the five interaction suites + the known-gaps ledger. |

**Explicitly deferred:** all GPU serving, the compute contract, the trainer, the eval harness, the
factory loop, per-department *training* scopes (no training yet, so the risk does not exist yet —
though the *scope model itself* ships, because the router and telemetry need it), hostile-tenant
accelerator isolation, and multi-region HA.

**Sell the deferral explicitly.** S10's warning stands: the scope-creep pressure is "but where is my
coding agent" on day one. Under this scope the answer is good — the coding agent *is* in from day one
as a frontier-routed pass-through — but "where is my local model" is the real question and it must be
answered up front rather than discovered as a disappointment.

### 7.3 What forces v0.2

The purist argued this is now **structural rather than motivational**: the corpus accumulates, the eval
gate is the only thing between the customer and a lower bill, and therefore **the customer pulls v0.2.**
The cut line cannot quietly become the whole project because the customer will not let it.

**Verification of the argument: it holds, but conditionally, and the conditions are v0.1 build items.**

Three preconditions, each of which fails silently if not built:

1. **The corpus must actually accumulate.** The paired-reference corpus depends on capture policy, which
   defaults **off** for paths whose terms restrict training use (§6.6) — and those are the only paths in
   v0.1. Left there, capture defaults off, the corpus does not accumulate, and the pull disappears. What
   saves the argument is that the factory's foundation is **first-party behavioral signal** — the
   customer's own data about their own users — which accumulates unconditionally and needs no provider
   permission. The escalation corpus accumulates only where the operator's policy permits.
   **Requirement:** v0.1 must ship a **corpus depth and eligibility counter**, showing separately how
   much first-party signal and how much paired reference exists per workload class. If the customer
   cannot see the corpus growing, nothing pulls.

2. **The customer must see a number that says "you could pay less."** Corpus depth alone is not a
   demand signal. **Requirement:** a **per-workload-class frontier cost panel** — "class X is N% of your
   traffic and costs $Y per period at frontier" — so the size of the unclaimed prize is visible next to
   the corpus that would claim it.

3. **The paired references must be stored from day one** (§6.7 item 4). Without them the eval gate has
   nothing to gate with and the customer's pull hits a wall.

With those three built, the argument is sound: the customer sees a growing corpus, a priced
opportunity, and a gate. Without them it reverts to motivational, which is exactly what Q9 was guarding
against.

One honest counterweight: v0.1 is a fully monetizable standalone and some customers will stop there.
That does not break the argument. The claim is not "no customer stops," it is that a **standing demand
signal exists** and therefore the cut line does not become the whole project by default.

---

## 8. Verifiability under agent authorship

This is the newest idea in the design and it is the most consequential. It is not testing discipline
presented as good practice. It is the core mechanism of the thesis.

### 8.1 The claim, restated precisely

Generation was never the constraint. **Verification is.** The boring layer — SSO, RBAC, tenant
isolation, audit, concurrent quota, reconciliation — is expensive because agent output on auth,
isolation and money is reliably **plausible-and-wrong**, so every line costs human review. Human review
is the scarce input a solo founder cannot buy more of.

Therefore the design objective is not elegance and not brevity. It is:

> **Minimize human review minutes per unit of confidence.**

And because the moat is exactly the set of work that is founder-attention-gated rather than
fleet-throughput-gated, **every mechanism that converts attention-gated work into throughput-gated work
shrinks the moat directly.**

### 8.2 The general rule

**Replace judgment with execution.** Any property whose confirmation currently requires a human to read
code and reason about it should be restructured so that confirmation is a green/red from a mechanism
the human trusts *once* and thereafter never re-reads.

Corollary, and it is the one that actually decides design arguments:

> **Prefer designs where correctness is a structural property over designs where correctness is a
> behavioral property.** Structural means a human reads one small function or one table and is done.
> Behavioral means a human must reason about all call sites, forever.

### 8.3 The five mechanisms, and what each concretely demands

**(a) Invariant tests with supremacy over the implementation.**

A protected `invariants/` directory under CODEOWNERS, with an L3 CI gate rejecting any PR that touches
both `invariants/**` and `src/**`. Written from a threat catalog produced by a **different agent than
the implementer** — test plans written by implementing agents have the same blind spots as the
implementation.

*What changes shape:* the **scope model becomes a single pure function with no I/O**, because a pure
function is cheaply exhaustively testable — the cross product of (scope class × path × residency ×
terms class) can be enumerated in a table a human reads once. If scope checks are scattered across the
router, the cache, the trainer and the telemetry path, no such table exists. The purist reached "one
scope model, three consumers" by architectural economy; verifiability reaches it independently and for
a stronger reason.

**(b) Conformance suites that execute rather than describe.**

**A contract is defined by its suite. The prose is a comment on the suite.**

*What changes shape:* every contract has a **reference fake, and the fake is written first.** If the
contract cannot be expressed as a fake that passes its own suite, the contract is under-specified —
which makes the fake a *design-time detector*, not a testing chore. The brief already required fakes;
this promotes them from testing convenience to definition mechanism, and makes fake-before-real a rule.

**(c) Adversarial harnesses on the isolation boundary.**

The isolation boundaries in v0.1 are: virtual-key → identity binding; scope → cache key; scope →
capture ledger; tenant → budget counters. Each gets a hostile harness written by an **adversary agent,
not the implementer.**

*What changes shape:* the cache. The harness for scope → cache key must issue crafted near-duplicate
embeddings, timing probes and poisoning attempts across two disjoint-scope tenants and assert **zero
cross-scope hits and a bounded timing differential.** Writing that harness makes the design decision
obvious: a **physical namespace partition per scope hash** is verifiable by reading one key-derivation
function; a global cache with a similarity predicate is not verifiable at all, at any review budget.

**This is what decided NA3** (§3.4). The global cache lost — not because leaking is worse than saving,
though it is, but because the partitioned design is *confirmable* and the global one is only ever
*argued for*.

**(d) Mechanical reconciliation against a real invoice.**

The reconciliation job is **not a report. It is a test.** Given a provider invoice for a period and the
ledger for the same period, assert equality to the cent within a declared tolerance, and fail loudly.

*What changes shape:*
- The metering record must carry **every field the invoice is computed from, at request granularity** —
  you cannot reconcile a bill from aggregates (§5.2).
- Prices are **recorded as observed at request time**, not looked up later, or a rate-table change
  silently breaks reconciliation.
- **A redacted real-invoice fixture corpus is checked into the repo**, so the reconciler is testable
  without waiting a billing cycle. Nobody named this in the deliberation and it is a required build
  item: a reconciler you can only test once a month is a reconciler that is attention-gated forever.

**(e) Structural correctness over behavioral correctness — applied.**

| Property | Behavioral form (rejected) | Structural form (adopted) |
|---|---|---|
| Scope enforcement | Optimizer *should not* choose an excluded path | `PermittedPaths` returns a set; the optimizer's **input type** is the filtered set, so it *cannot* |
| Quota under concurrency | Checks at several layers; hammer with load tests | **One admission point**, one atomic operation, one store — and the store's operations sit behind an interface a test can drive with a **controlled interleaving schedule** |
| Token validation | Each component validates | **Only the gateway validates.** Components receive an already-validated principal |
| Trusted history | Mutable rows plus an audit table | **Append-only hash chain**; derived views rebuilt and never authoritative |
| Eval gate supremacy | Policy that the trainer must not weaken the gate | Gate runs in a **separate process with separate credentials**; the promoting actor has **no write path** to it |
| Breakglass expiry | Control plane revokes on schedule | **Network layer** enforces a TTL carried by the rule itself |

The quota row deserves emphasis because it collides with a standing rule. The conventional test for a
concurrent counter is "hammer it with many goroutines" — which is precisely the **flaky test** the
operating rules ban, and a flaky test in the one place where a race is a revenue leak is worse than no
test. So the architecture must expose a seam where the concurrency can be exercised **deterministically**
with a fixed schedule. That is an architectural demand generated purely by the verifiability objective,
and it would not appear in a design optimized for anything else.

The token-validation row is a decision that was previously made on chokepoint grounds and is now
**over-determined**: any component doing its own validation multiplies the review surface by the
component count, and token-exchange errors are a breach vector where subtly wrong code looks correct in
review.

### 8.4 Append-only as verification economy

An append-only hash-chained log is verifiable by a checker of roughly fifty lines that a human reads
once; thereafter every claim about history is a mechanical check. Mutable state requires reasoning about
every writer, forever.

**One primitive, four consumers:** the capture ledger (data provenance), the falsification ledger
(evidence integrity), the conformance-submission ledger (adoption), and the metering ledger (money). And
now a fifth: the breakglass record.

This is why §9's falsification table is an L4 circuit breaker on the evidence itself — **you cannot
retroactively move a goalpost that is in git with a hash.**

### 8.5 What this costs, and the failure mode

Stated honestly, because a design principle that claims to be free is not being examined:

- **More indirection, more code, more artifacts, some duplication** (a fake per contract), and a slower
  first commit on any feature that touches an invariant. The trade is accepted deliberately: the
  currency is human review minutes, not lines of code.
- **The failure mode: harnesses written by agents can be plausible-and-wrong too.** An agent can write a
  harness that looks rigorous and misses the regression it exists to catch — tests-pass-by-construction
  is the classic failure.

Mitigation, and it is the bootstrap that makes the whole thing work:

> For an attention-gated category, the harness is written by a **different agent** than the
> implementation, and the **harness itself is attention-gated on its first version** — the founder
> adversarially probes it once. After that, everything the harness governs is throughput-gated.
>
> **You pay attention once per mechanism, not once per line.**

That sentence is the engine of the thesis. Every section of this design that looks like extra
engineering is buying an instance of it.

### 8.6 Components that changed shape because of this principle

| Component | Change |
|---|---|
| Router | Hard filter is a **type boundary**, not a predicate. Decision function is pure given `(request, weights, path set, clock)`. Weight vector external and versioned. |
| Scope | **One pure function, no I/O, three consumers**, exhaustive table test, consumers forbidden from re-implementing. |
| Cache | **Physical namespace partition per scope.** Core-owned key derivation. Adversarial harness. Cross-scope dedup forfeited and the cost reported. |
| Gateway | **Sole token validator.** Single quota admission point behind a **deterministically schedulable** store interface. |
| Metering | **Immutable, priced-at-write, request-granular**, full invoice field set. Redacted real-invoice fixtures in repo. |
| Every contract | **Fake first.** Suite is the definition. Suite in a protected directory under an L3 gate. |
| All trusted state | **Append-only hash chain**, one checker, derived views rebuildable. |
| Eval gate | Separate process, separate credentials, **no write path from the promoting actor.** |
| Breakglass | Offline-verifiable activation; **network-enforced expiry** — correctness must not depend on the component being bypassed. |

---

## 9. Evidence and falsification

### 9.1 The primary instrument: the attention/throughput boundary

The operator introduced this after the last deliberation round and it now **outranks everything else**,
including the cost ledger. It measures the thesis directly rather than by proxy, needs no adopters and
no external data, and is observable from inside the project.

> **The moat is exactly the set of work that is founder-attention-gated rather than
> fleet-throughput-gated. Each model release either moves an item across that boundary or does not.**

**Recording, per work item.** Every rd item carries a required field `gate: attention | throughput`,
assigned **before work starts**, with a written rationale.

Classification criterion, stated mechanically rather than by feel — an item is **attention-gated** if a
plausible-looking wrong implementation would pass its own tests. Specifically, if **any** of:

1. The failure mode is **silent** (breach, leak, drift, slow financial divergence) rather than loud.
2. Correctness depends on **concurrency, adversarial input, or an external system of record**.
3. The test that would catch the defect is one **the implementing agent would also have written**.

Otherwise it is throughput-gated. **Default up** when uncertain — per DAP §4.8, the completion drive
gives an agent a structural incentive to under-classify, because a lower tier skips the gate.

Also recorded per item: model and tier used, human review minutes, **human-found defects after the agent
declared done**, whether the classification was revised, and in which direction.

**Measurement of boundary movement.** A single item is noise. The unit of measurement is the
**category**, and the current attention-gated categories are:

1. Auth chokepoint and token exchange
2. Quota enforcement under concurrency
3. Cost reconciliation to the cent
4. The eval gate with automatic rollback
5. Per-department scoping and tenant isolation

For each category, track **human-found defects per agent-declared-done**, labeled by model release. A
category is declared to have **crossed** when that rate reaches zero across a pre-declared number of
consecutive independent items, **at a review depth held constant** (recorded as a review-protocol
version).

**The moat's size at any moment** = the count of categories still attention-gated, plus their estimated
review-hours. That number is the headline of the evidence dashboard, and publishing the series labeled
by model release is the deliverable.

**Two corrections to the brief's framing, both necessary:**

1. **The measurement is not monotone even though the capability is.** Sample sizes are small and
   categories are heterogeneous. If a later item in a crossed category surfaces a human-found defect,
   the category moves **back**, and the record must show the reversal rather than smoothing it. Claiming
   monotonicity in the instrument is how the instrument stops being one.

2. **The instrument measures two causes and they must be separated.** A category's defect rate can fall
   because models improved *or* because §8's verification scaffolding landed on that category. These are
   different claims and only the first supports the thesis. **Requirement:** record whether a defect-rate
   drop coincided with a new invariant, conformance suite, or adversarial harness landing on that
   category. If it did, attribute the movement to §8, not to the model.

   This matters more than it may look: §8 is *deliberately designed* to move work across the boundary, so
   without this separation the project's own engineering would be misread as evidence that frontier
   models got better. That would be self-deception of exactly the kind §5.4 exists to prevent.

### 9.2 The falsification table

Pre-registered before commit one. Hash-chained, append-only. **Rows may be added, never edited.** A
row's outcome may be recorded once. **Scope drift voids a row rather than re-scoping it** — a void row
is an honest outcome; a re-scoped row is not. All windows are in **event counts or monotonic sequence
positions. No calendar anywhere.**

Rows C1–C10 were specified in the deliberation by rank and instrument; the row texts and thresholds
below are the architect's reconstruction and are **normative from here**.

| ID | Claim | Instrument | Falsification condition | Rank |
|---|---|---|---|---|
| **C0** | The moat is closing | Attention/throughput category boundary (§9.1) | No category crosses across a pre-declared number of model releases, with §8 attribution controlled | **PRIMARY** |
| **C1** | Production cost of the boring layer has collapsed | Cost ledger: $ per emerged feature, **frozen at the rate table in force on the commit date** | $/feature flat or rising across N consecutive features with complexity controlled | **PRIMARY** |
| **C2** | The conventional estimate is an overestimate | A **sealed** conventional estimate hashed at commit one and never edited | Actual ≥ sealed estimate. **Row VOIDED, not re-scoped, if scope drifts** from the sealed definition | **PRIMARY** |
| **C3** | The artifact stays agent-legible | Stranger's-agent series (§9.3) | Tokens-to-green rises across the series **at constant model tier** | **PRIMARY** |
| **C4** | Savings begin before local capacity exists | `Δ_route + Δ_cache` in period 1 | Net-zero or negative **after** the factory cost line | Supporting |
| **C5** | The bill declines because the system gets better at this customer's work | `escalation_rate(class, window)`, windows = request-volume deciles, anchor budget excluded | Flat or rising across two consecutive post-gate windows with mix controlled | Supporting |
| **C6** | Route+cache is not the whole story | `(Δ_route + Δ_cache) / ΔS_total` | **≥ 90% indefinitely → the factory thesis is not supported; the product is a cost-routing proxy, and the report says so** | Supporting |
| **C7** | The abstraction is at the right seam | Conformance pass vs field behavior | A component that passes its suite then breaks the control plane in the field | Supporting |
| **C8** | One control plane is real, not aspirational | Breakglass fraction; second-login count in the reference bundle | Breakglass minutes exceed the declared threshold over a rolling request-count window; **or** the bundle requires a second login | Supporting |
| **C9** | Small specialized models beat big general ones on this customer's work | Paired shadow eval win-rate per class, local candidate vs frontier reference on served-and-accepted traffic | **No class reaches non-inferiority within its declared margin after K windows of corpus accumulation** | Supporting |
| **C10** | The artifact mattered | Conformance-submission ledger; voluntary third-party badge claims | Zero third-party submissions after publication | **Necessary, not evidence** |

**C9 exists because it was the deliberation's most load-bearing unsupported claim.** "Small specialized
models beat big general ones on your work" carried zero evidence — no benchmark, no citation, just an
illustrative example — and the brief's own adjacent admission ("nobody out-trains a frontier lab on one
rack") is evidence against its generality. The specialized model must beat a *continually improving*
frontier baseline on ever-narrower slices, and that margin shrinks each time a lab ships a stronger
checkpoint. C9 is how we find out rather than assume.

### 9.3 The stranger's-agent protocol

Automated, scheduled, committed, and **published**.

- Hand a fresh agent the repository with **no repo-specific help**. Task set rotates: add a serving
  backend; add a provider path; upgrade a pinned upstream across a breaking change.
- Record: tokens-to-green, wall-clock, diff size, number of contract documents read, retry count.
- **Model tier is held constant** — pinned to a declared reference model.
- **When the reference model is retired**, run the new one **in parallel with the old for at least one
  cycle** to establish a conversion factor before switching. Otherwise the series breaks precisely when
  it becomes interesting. (Nobody addressed this in the deliberation; a series that silently changes its
  instrument is not a series.)
- The A20 correction: extending one contract says nothing about tracking twelve independent upstreams'
  breaking changes, which is why the rotating task set includes an upstream break, and why §9.4 counts
  upstream-break work as a ledgered feature.

Publishing it makes it simultaneously the project's self-audit and marketing collateral no incumbent
can produce — proof of the core claim generated as a side effect of doing the check honestly.

### 9.4 The evidence ranking fix

The brief said in one place that "adoption is the evidence" and in another that the carrying-cost ledger
is "the only clean test of the thesis." **Those two sentences contradict each other, and the second is
correct.**

The thesis is about **production cost**. Adoption is evidence that the artifact *mattered* — a different
and lesser claim. The ranking is therefore:

- **PRIMARY:** C0 (boundary), C1 (cost ledger), C2 (estimate delta), C3 (legibility series). These
  measure the actual thesis, require no adopters, and never leave 3DL.
- **SUPPORTING:** C4–C9. Product claims.
- **NECESSARY BUT NOT EVIDENCE:** C10 (adoption). A spoiler nobody installs proves nothing, so adoption
  is a precondition for the *result being interesting* — it is not a measurement of the claim.

**The cost-ledger unit must count maintenance as features.** A19's strongest form is that the
denominator (feature count, linear) is measured against a numerator (maintenance surface, combinatorial)
that grows faster, so the ledger reads flat while true carrying cost compounds. The fix is direct:
"**kept up with an upstream break**" is a ledgered feature with its own $/feature entry. If the work is
combinatorial, count the combinatorial work as work.

**Adoption without phone-home.** Anyone who runs a conformance suite against their own component has
adopted. Make conformance results voluntarily publishable and the contract layer becomes an adoption
ledger that **respects air-gap absolutely.** That is a design move, not a workaround, and it is why the
submission ledger is load-bearing rather than decorative.

### 9.5 Pre-commitment to publishing a negative result

Written into the repository at commit one, signed, hash-chained, in git:

> The falsification ledger is published on a fixed cadence, in the same place, **regardless of
> outcome.** A triggered falsification row is published within one reporting cycle, with the raw data.
> Rows are appended, never edited. Scope drift voids a row rather than re-scoping it.

Without this, C0–C10 are a table of thresholds that can be quietly abandoned when one of them fires,
which is the same class of failure the whole evidence layer exists to prevent.

---

## 10. Attack register

Every attack from both rounds. **RESOLVED** = the design changed and the attack no longer lands.
**ACCEPTED** = permanent constraint, with why. **OPEN** = live, with what would close it.

| ID | Attack | Disposition |
|---|---|---|
| **A1** | Migration-window ingestion assumes a window the customer wants to shorten; closed accounts cannot be backfilled. Mutated: intermediation makes provider-billing-API dependency **permanent**. | **RESOLVED** by precise restatement (§1.4): "no phone-home" = no telemetry to 3DL and no 3DL service in any data path. The customer's layer calling the customer's own providers is not phone-home. In air-gap there is no frontier tier, so nothing to reconcile. The only live case is hybrid restricted-egress, supported via manual invoice upload — the fixture path already exists (§8.3d). |
| **A2** | "OpenAI-compatible" covers the call shape only; lifecycle differs completely across the five engines. | **RESOLVED** by shrinking the contract. Engine contract = inference + health + capacity introspection. Lifecycle → Compute contract. Quantization and multi-LoRA explicitly out of contract, surfaced as capability flags in the known-gaps ledger (§3.3). |
| **A3** | Conformance-not-combinations is false wherever contracts interact (trainer × serving contention). | **ACCEPTED** as a permanent limit; **partially RESOLVED** by five enumerated interaction suites (§3.5). The methodology sentence in the brief is amended. The enumeration is incomplete by construction and that is on the record. |
| **A4** | Cline has no SSO; the bundle's own headline coding agent contradicts the "entirely top-tier" claim; nobody owns the shim. | **RESOLVED** three ways: contract rewritten to base-URL redirect + minted key; outcome capture moved to git/CI so tool cooperation is unnecessary; and the "entirely top-tier" claim is **retracted** and replaced with a per-component tier table (§3.6). The shim is owned: it is wiring, in scope, and a v0.1 line item. |
| **A5** | A State swap can pass functional CRUD conformance while silently dropping durability the audit trail needs. | **RESOLVED** for State by welding to Postgres. Generalized rule: **anything with a durability requirement lives on the welded component** — audit, capture, metering, falsification ledgers. Remaining swappable stores declare a durability class in the known-gaps ledger; the cache is required to be reconstructible. |
| **A6** | Eval is framed as a stateless function but must be a live, stateful, scoped, drift-sensitive pipeline. | **RESOLVED** by splitting harness (Job, swappable) from gate (core, stateful, separate process and credentials) — §3.4. |
| **A7** | Signal is the factory's foundation, has no reference implementation and no standard, and is buried in a table cell. | **RESOLVED**: additive namespace on OTel GenAI semconv with a ~6-value outcome enum, as a first-class versioned artifact with its own conformance assertion. Interop with every OTel-speaking backend comes free. |
| **A8** | "Quotas that actually stop overspend" conflicts with an advisory router; allow/hard-deny is blunter than incumbents' soft limits. | **RESOLVED**: the router decides (Q1 closed), so the quota system supports a **budget degradation ladder** — warn thresholds, grace, degrade-to-cheaper-path — not just allow/deny. The soft-limit behavior is *enabled by* the routing decision. |
| **A9** | Fallback escalation double-counts across two ledgers; nobody owns the reconciliation. | **SUBSTANTIALLY RESOLVED** by single-chokepoint metering — one record tagged by path, not two ledgers. Residue **RESOLVED** by `logical_request_id` + attempt sequence: cost aggregates over attempts, savings and escalation over logical requests (§5.2). |
| **A10** | "Reconciles to the invoice" has no owned-hardware analogue; the claim asserts epistemic parity across three legs where two cannot have it. | **ACCEPTED** as a permanent asymmetry, made visible: T1/T2/T3 tier labels on every dollar; owned-hardware model assumptions displayed as editable CFO inputs (§5.3). The parity claim is **retracted** and replaced with per-figure evidentiary labeling. |
| **A11** | "Idle capacity becomes training capacity" asserted as decided while Q2 asks whether serve+train concurrency works. | **RESOLVED for v0.1** by deferral (no GPUs). For v0.2 the position is recorded as conditional, not decided: preemptible lower-priority partition that yields to serving, cheap checkpointing, duty cycle as a model *input* (§6.5). Rented training compute is a cash line, never netted (§5.3). |
| **A11b** | 25–30% duty cycle assumes single-timezone idle windows; long runs collide with an 8am start. | **ACCEPTED** as a permanent constraint on factory economics; **OPEN** as an economic question for v0.2, which is precisely why v0.1 collects the utilization profile on someone else's hardware first. |
| **A12** | The declining-bill metric and the improving-model metric cap each other; success starves its own corpus. | **RESOLVED** by distinguishing the two supplies and adding the **anchor budget** (§6.3): first-party behavioral signal grows with migration; the frontier reference supply decays, so a declared operator-set floor of traffic routes to frontier regardless of verdict, priced as a named "anchor cost" line and excluded from the escalation metric. |
| **A13** | No reserved tier over the one demonstrably valuable layer means a better-capitalized incumbent forks it, absorbs adoption, and pre-empts both acceptable outcomes. | **ACCEPTED**, and it is not a failure mode under the stated objective function. If an incumbent forks it and it becomes ubiquitous, production cost has collapsed and the thesis is demonstrated. The only real loss is **re-closure**, which Apache 2.0 permits and which no license choice available to us prevents. Mitigations are trademark, the evidence series and the conformance ledger — not licensing. |
| **A14** | Network chokepoint assumes segmentable networking; flat trusted HPC networks make it a re-architecture. | **ACCEPTED**. Egress enforcement is a documented **deployment precondition** with three supported mechanisms and one unsupported case. **Mitigated** by making coverage *measured* rather than assumed: the invoice/ledger delta is a leak detector (§2.5), so a customer who cannot enforce can still quantify their non-coverage. |
| **A15** | "No phone-home, air-gap capable" stated absolutely alongside "must continuously ingest provider APIs." | **RESOLVED** — same restatement as A1 (§1.4). |
| **A16** | Self-contradiction: won't cripple a general pipeline to pre-empt someone's legal argument, then mandates scoping for structurally the same reason. | **RESOLVED** by making scope a **contract precondition** (`consented_scope` required on every dataset; a trainer ignoring it fails conformance). We ship the mechanism; the operator sets the policy. That is consistent with both statements (§6.6). |
| **A17** | — | **NOT RECORDED.** Round 1 reported 22 attacks; A17 appears nowhere in the deliberation record. It is not resolved, accepted or dismissed — it is missing. Recorded here rather than silently dropped. Closing it requires re-querying the adversary. |
| **A18** | Hostile-tenant GPU isolation defeats the multiplexing that makes utilization economics work. | **ACCEPTED**, with the claim narrowed. The enterprise-checklist line is **retracted at the accelerator layer** and retained at the control-plane/data layer (scope, cache, ledger, budget) — which is exactly where v0.1 lives. Supported accelerator model: trusted-tenant multiplexing within one org, with node/process isolation as a configurable option at a stated utilization cost. |
| **A19** | The cost function is **multiplicative**, not additive; the ledger reads flat while carrying cost compounds, at any execution speed. | **PARTIALLY RESOLVED; remainder OPEN and named the project's principal risk.** (1) Upstream-break work is a ledgered feature, so the numerator is counted (§9.4). (2) The multiplicative surface is bounded **by policy**: the supported set is exactly what passes conformance, the reference bundle is ONE configuration, and swap targets beyond it are community-certified — their carrying cost accrues to their authors. This converts the term from multiplicative to linear *for 3DL* while leaving it multiplicative for the ecosystem, which is why the conformance leaderboard is load-bearing. (3) **Contract count is capped at twelve**; a thirteenth requires the spec-change gate. **OPEN remainder:** provider fingerprint currency (see NA4). |
| **A20** | The legibility test measures extending one contract and says nothing about tracking twelve upstreams. | **RESOLVED** by (a) counting upstream-break work as ledgered features and (b) rotating the stranger's-agent task set to include an upstream break (§9.3). |
| **A21** | No falsification thresholds anywhere; the objective function can be claimed satisfied but never shown wrong. | **RESOLVED** by the C0–C10 table with pre-registered numeric thresholds, hash-chained and append-only, plus the publish-negative-result pre-commitment (§9). This was round 1's most damaging finding and the table is the answer. |
| **A22** | The Decided list's non-negotiable rhetoric forecloses the v0.1 cut line that Q9 asks for. | **RESOLVED** structurally: this document separates *decided for the architecture* from *in scope for v0.1*. Everything in the Decided list remains architecturally binding; §7 states which are implemented now and which are merely not foreclosed (§6.7). |
| **A23** | A permanent intermediary becomes a new **subprocessor** requiring a full vendor security/legal review — possibly a harder approval, not an easier one. | **RESOLVED** by the same-key/own-contract model: the customer's keys, the customer's contracts, software they run, no data to 3DL. There is no new processor. **Deliverable:** a shipped security review pack (data-flow diagram, no-egress attestation, SBOM, exit procedure). |
| **A24** | Pooled/proxied traffic across a shared account may collide with reseller and account-sharing terms. | **RESOLVED** by the same model — no pooling, no shared account, no resale. This is the pattern LiteLLM, Portkey, Kong and Cloudflare sell openly. Recorded, not eliminated. |
| **A25** | New defection vector: our mandatory hop adds latency/jitter on a fresh unhardened control plane, and a developer routing around it **breaks the universal-visibility premise the product is sold on.** | **ACCEPTED as a real risk** with a hard budget: added p50 ≤ 15 ms, p99 ≤ 50 ms, TTFT ≤ 25 ms, published live and asserted as a gateway conformance property; true streaming pass-through, never buffered for metering (§4.6). Plus provenance disclosure for attributability and the breakglass coverage metric for detection. |
| **A26** | Intermediating through a pooled account silently strips negotiated throughput/priority tiers. | **RESOLVED** by the same-key model — their key, their tier. Rate-limit **concentration** (S19a) survives as a procurement conversation, **ACCEPTED**, mitigated by the headroom axis (§4.5) and a headroom-utilization report line so procurement has data. |
| **A27** | **Committed-spend collision.** At ~$500k/yr, price is contractually fixed with minimums and true-up penalties. Routing away from the committed path both fails the minimum and abandons a discount better than any alternate path's list price. | **RESOLVED by retraction and replacement.** Route-on-price-across-vendors is retired (§1.4). Replaced by cheapest-already-contracted-path plus **commitment-aware routing**: per-path commitment state in the catalog and an explicit `shortfall_penalty` in the objective, so under-consuming a committed path has a visible price at decision time rather than at period close (§4.3). |
| **A28 / NA1** | HA is now day-one, not a hardening milestone; a bad deploy takes down chat, coding and all frontier access at once — strictly worse availability than direct multi-vendor access. | **ACCEPTED and moved into v0.1 scope**, honestly bounded: available, not multi-region HA. Stateless N≥2 gateway, replicated Postgres with tested failover, cache optional-by-design, and **fail-static policy caching** so a control-plane outage degrades to last-known-good rather than denying traffic (§4.9). Stated plainly in customer material that without these the posture is worse than direct access. |
| **A29** | Cloud marketplaces are priced at parity with direct to avoid cannibalization, so the arbitrage mechanism has near-zero margin. | **CONFIRMED** by S16 and folded into A27's resolution. |
| **A30** | Residency degenerates "cheapest path" into "the only legal path," and regulated buyers are over-represented in this segment. | **RESOLVED** — that is the specified behavior. Residency is a **filter term**, not a weight (§4.4). The degeneration is correct. |
| **A31** | Anthropic already offers native prompt caching, so our incremental cache value is narrower than stated. | **ACCEPTED and folded.** Our cache's value is (i) cross-surface exact/semantic dedup **within a scope**, (ii) in-flight dedup, and (iii) provider-cache **eligibility routing**, which is a router job and the larger of the three terms. The 40–80% headline is replaced by S20's blended 15–35% for general enterprise chat, reported as **measured per scope**, never assumed — and the scope partition (§3.4) will push it lower still. |
| **NA2** | Becoming the payment rail reconstructs a gatekeeper: customer continuity now depends on our commercial standing. A strange design for a project proving moats are not defensible. | **RESOLVED** by the same-key model (we are not in the payment path) plus a **tested, CI-verified exit path** — export the ledger, revoke virtual keys, restore direct keys, remove egress rules. An exit test is the anti-lock-in mechanism; a license is not. |
| **NA3** | Cross-surface cache collides with per-department scoping; global defeats segregation, partitioned guts the hit rate, and the brief picks neither horn. | **RESOLVED by picking the partitioned horn** (§3.4), decided on verifiability grounds (§8.3c). Cost stated explicitly: cross-scope dedup is forfeited, hit rate is reported per scope, and the savings model uses the lower number. |
| **NA4** | Provider feature drift is an unowned thirteenth conformance suite over external release cadences, yet the day-one savings depends on it being current at all times. | **RESOLVED IN PART** by giving it an owner: the Provider Path contract carries a feature/latency/price fingerprint, an **automated fingerprint probe** runs on a schedule against every configured path, and a diverged path is **automatically excluded from `PermittedPaths`** for requests requiring the diverged feature (fail-safe). **OPEN remainder:** an entirely new feature class nobody has written a probe for. Closing it fully would require a provider-published capability manifest, which does not exist. |

**Also recorded, from the unstated-assumptions finding:** the brief's claim that "nothing joins the
surfaces today" is corrected to **"a wiring layer exists commercially; it is paywalled in every
open-core competitor."** That is the honest claim and it is stronger, because it establishes the layer
is valuable rather than merely absent.

---

## 11. Known gaps and deferred decisions

Stated plainly rather than papered over. Each names what would close it.

1. **The human edge is the design's largest DAP hole (~15%).** Every signal in the contract — accept,
   reject, regenerate, test-pass, commit-stuck — is an agent-or-outcome signal. **Not one is a
   human-flow signal.** The design captures agent-optimants and tooled-problems and captures **zero
   human-optimants**. Candidate signal classes are specified but **none is validated**: session
   continuation depth and abandonment point; implicit reformulation-as-dissatisfaction (reuse the
   cache's existing embeddings to compare a follow-up against the same user's last-N in-session
   queries — a semantically close follow-up shortly after an answer is a silent "that did not work,"
   and it is free because the embeddings already exist); silence as a derived signal class;
   model-choice-as-revealed-preference; reformulation chain length; cost-weighted abandonment;
   cross-surface substitution. **Closes when:** these are instrumented and at least one is shown to
   predict a human outcome. Until then they are hypotheses.

2. **Asymmetric relay costs — a proposed DAP spec amendment, unvalidated.** DAP §6.3 says of the
   human-AI edge: "None. You have no data here. You never will." That is true of **content**. But one
   layer with one identity spanning chat, coding agent and internal apps makes the **shape** of the
   relay visible: how often a human bounces between surfaces, in which direction, carrying what, at
   what token and wall-clock cost. Proposed amendment: *"no content data on the third edge; relay
   frequency, direction and cost are observable wherever one layer spans both surfaces."* **This is a
   hypothesis, not a finding.** **Closes when:** v0.1's cross-surface data either exhibits measurable
   relay structure or does not.

3. **Aggregate-only mode is required but not designed.** §5.7 mandates it; how each dashboard degrades
   under it — which panels lose meaning, which become misleading, which are simply unavailable — is
   unspecified. **Closes when:** the panel set is enumerated with its aggregate-only behavior.

4. **Air-gap curation.** The frozen human-curated held-out eval set is the only exogenous input in an
   air-gapped deployment. **Who curates it, how it is kept representative as the customer's work drifts,
   and how staleness is detected are all unspecified.** This is the weakest point in the air-gap story
   and it is load-bearing there.

5. **Owned-hardware cost model has no neutral answer.** Depreciation schedule, power, cooling, staff
   allocation, burst rental. The design makes the assumptions visible and editable (§5.3) rather than
   resolving them, because they are not resolvable. A CFO may still reject the T3 figures.

6. **Non-inferiority margins per workload class are undefined** and cannot be defined without data. The
   paired-shadow mechanism is specified; the threshold it compares against is not. **Closes when:** the
   first classes accumulate enough paired samples to characterize variance.

7. **Chat-surface quality relative to claude.ai at an identical model.** Q5 narrowed usefully — defection
   as procurement removes the model-quality half — but what survives is real and untested: a user can
   find our *wrapper* worse than claude.ai at the same model (streaming feel, artifacts, projects, file
   handling, mobile). **No test is defined.** **Closes when:** a side-by-side protocol at identical
   model exists and is run.

8. **Contract evolution.** Twelve contracts with no amendment process is a frozen interface. §3.5
   specifies the *suite* amendment gate; it does not specify how a contract acquires new capability. The
   obvious driver is already present and unused: the **rejection stream** — capture what callers asked
   for and were refused (wrong endpoint, unsupported parameter), because that is the caller telling you
   what it expected. DAP §1.6 requires three verbs — **log it, score it, if it recurs pave it** — and
   this design currently specifies only *log* and *score*. **Paving criteria are undefined.** A captured
   rejection with no promotion path is a log file, not a desire path.

9. **Interaction-suite enumeration is incomplete by construction** (A3). Five seams are named. There is
   no argument that five is sufficient, only that these five are necessary.

10. **A17 is missing from the record** (§10). Closing it requires re-querying the adversary.

11. **Engine lifecycle is out of contract** — quantization, multi-LoRA, scale operations. Deliberately
    descoped in v0.1 because the five candidate engines share no standard. It will have to be faced when
    local serving lands, and there is no reason to expect a standard to appear first.

12. **The C2 seal needs a scope definition precise enough to detect drift.** A sealed estimate whose
    scope is loose can be satisfied by re-scoping, which is exactly what the void rule exists to
    prevent — but the void rule only works if the sealed scope is written tightly enough that drift is
    detectable. **That definition is unwritten and must be written before commit one**, because it
    cannot be added afterward without voiding the row it protects.

13. **3DL's compounding channels are named but unmeasured.** Zero data amplification is deliberate. The
    three substitutes — cost ledger as evidence base, conformance-submission ledger as a contract-error
    channel, legibility series as methodology evidence — are real but have no instrumentation and no
    thresholds. They compound reputation and methodology, not data, and that distinction should be
    stated publicly rather than left to read as a contradiction.

14. **Q8 (buy-versus-rent evidence a CFO will sign) is improved but not closed.** Intermediation means
    the customer learns their full workload profile — token mix, request shapes, peak/trough,
    cacheability, per-surface distribution — **on someone else's hardware before any capex**, which is
    the best possible input to a buy-vs-rent memo and was previously unobtainable without buying first.
    But the memo still rests on the T3 assumptions in gap 5 and the duty-cycle question in A11b.

---

## 12. The resident "Agents" surface

Full design record: **`docs/design/records/agents-surface.md`**. This section states the
binding contracts every downstream item consumes; the record carries the reasoning, the
losing arguments, and the two RESERVED rulings. Epic `enterpriseaiframework-da7`.

A fourth portal tab beside Chat and Code that lets a user fire up and manage named,
persistent **Hermes** agents — each a long-running **`hermes gateway run`** daemon
(NousResearch's Hermes Agent) on its own PVC that keeps working after the browser closes and
lives until intentional shutdown. It reuses the residency *chassis* of the Code/workspace
surface (PVC-backed, single-writer, scale-to-zero stop) but runs an autonomous agent, **not
opencode** — opencode is the Code/coding surface, and conflating the two was the original
error (see the retarget record). The console is a **terminal into the running agent**
(`hermes --tui`, exec-attach), the operator surface you drive it from and console in to when
chat goes sideways — not a coding IDE. This is a new surface, not a workspace flag.

Runtime + console detail: **`docs/design/records/agents-surface-hermes-retarget.md`**
(supersedes Contract 2 and the console half of Contracts 1/4 below; the chassis contracts
stand). Default runtime chart `jyje/hermes-agent`; image `nousresearch/hermes-agent`
(date-tagged); inference routes through our gateway at `http://gateway:4000/v1`.

**Hard invariant (outranks the rest of this section):** the Code/workspace surface stays
**byte-unchanged and green** — the camp runs on it 2026-08-11. Contract 6 makes that
mechanical.

The six binding contracts:

1. **Identity / alias.** An agent instance is `(<user>, <name>)`, `<name>` a slug under the
   workspace's existing `^[a-z0-9][a-z0-9-]{0,38}$`. k8s objects are `agent-<user>-<name>`;
   the owner-scoped console path is `/agents/<name>/` (user from auth, never the path). The
   metering alias is **`<username>::agents/<name>`** — one `::`, instance folded into the
   surface field with `/`, so it **round-trips unchanged** through both `gateway.parse_alias`
   (`rpartition`) and `metering.py`'s `split_part(_,1)/(_,2)`, and lands per-instance on
   `/admin/spend` with no query change. The only edit is an additive `agent_key_alias` +
   one `parse_alias` clause in `gateway.py`; `key_alias`/`SURFACES` are untouched.

2. **Residency.** A resident **`hermes gateway run`** daemon is the pod's main process; the
   console **exec-attaches** `hermes --tui` inside the pod (sharing the daemon's on-disk
   session `state.db`), and a disconnect never ends the daemon. Session state on the PVC at
   **`HERMES_HOME=/opt/data`**. Lifecycle: **created → running → stopped → deleted**, mechanised as
   Deployment `replicas: 1` (running) / **`replicas: 0`, PVC retained** (stopped) / delete
   Deployment+Service+Secret then PVC (deleted). *Stopped accrues no resident cost* because
   there is no pod to meter — not a "stopped rate".

3. **Two metering dimensions.** (a) **Inference tokens** ride the existing virtual-key path
   gateway→Forge, unchanged. (b) **Net-new resident-time + compute**: wall-clock from pod
   `status.startTime`, compute from **cAdvisor `container_cpu_usage_seconds_total`** (a
   monotonic counter — chosen over metrics-server's lossy gauge; kube-state-metrics only
   cross-checks the stopped state), attributed by `(user, agent)` pod labels, in a
   **separate control-plane ledger**. Cost basis: **RULED by Baron — meter USAGE, not cost.**
   Owned hardware is sunk cost and only inference has a real upstream bill, so this
   dimension is **quantities** (resident hours, CPU-core-hours, peak MB) with no rate and no
   currency; a cost multiplier is a FUTURE item for commodity cloud compute, not a gap. It
   surfaces beside inference spend in `/portal/api/spend` (a `by_agent` sibling) by
   endpoint-layer composition, leaving `metering.spend_by_user_and_surface` and
   `/admin/spend` byte-unchanged.

4. **Config: integrated key vs BYO.** Default is the integrated metered
   `<user>::agents/<name>` virtual key. **BYO** points the agent's `OPENAI_API_BASE`/`_KEY`
   at an external provider, routing inference **around the gateway** and producing **zero
   ledger rows by design** — allowed because it is per-user, own-credential, and made
   **visible** (`model_source: byo`, an "off-ledger by design" label, never a silent $0);
   provenance, not the finding-4 leak. The BYO secret is a per-agent k8s Secret,
   **set-once, never returned** (finding 2). Residency (Contract 3b) still meters BYO — it
   holds a PVC and burns our CPU; BYO removes the inference row, not the residency row.

5. **Email component — RESERVED to Baron.** Recommended default **Maddy (GPL-3.0)** — single
   no-tier binary, GPL-not-AGPL, standalone SMTP daemon (no linking). Alternatives:
   **Stalwart** (AGPL-3.0 + commercial — its enterprise tier disqualifies it as *default*
   under the open-core rule; documented swap) and **Postfix** (IPL-1.0, no tier, MTA-only,
   heaviest ops).

6. **Code-untouched invariant.** Frozen byte-identical: `deploy/workspace/*`,
   `deploy/k8s/60-workspace-common.yaml`, `deploy/k8s/61-workspace.template.yaml`,
   `deploy/bin/provision-workspace.sh`, `tests/test_workspace_shell.py`. Enforced
   mechanically by `tests/test_code_surface_frozen.py` (owned by `-055`) running
   **`git diff --exit-code`** over that path set against the pre-Agents baseline, fault-
   injected to prove it bites. The surface is built from new files beside the frozen set.

Downstream consumers: `-055` (1, 2, 6), `-627` (1, 2, 3, 4), `-0e7` (1, 2), `-39d` (4, 1),
`-914` (3), `-a4e` (5), `-ede` (all — especially 6). Map in the record.

This is an **architecture-change-cascade** item: the record is the design artifact, and the
seven items above are its build cascade.

---

## Appendix: decisions ruled in this document

For the implementer who needs the short list.

| Question | Ruling | Loser, and why it lost |
|---|---|---|
| Router decides or advises? | **Decides.** | Advisory lost because intermediation justifies deciding on day one with zero quality risk, so no phase exists in which advisory is the rational stopping point. An advisory router is a manual relay forever — a dashboard, not a metabolism. |
| Privacy scope: weight or filter? | **Filter, outside the optimizer, enforced as a type boundary.** | Weight lost because a router that trades scope against cost is correct by its own objective every time it leaks. No weight setting fixes a category error. |
| Global or partitioned cache? | **Partitioned, physical namespace per scope.** | Global lost on **verifiability**: partition-by-construction is confirmable by reading one function; a similarity predicate that never leaks across departments can only be argued for. Cross-scope dedup is forfeited and the cost is reported. |
| Is State a port? | **No. Welded to Postgres.** | Portable-SQL lost because it is the Rails critique landing on itself. Admitting one welded piece makes the other eleven credible. |
| Do trainer and eval collapse (same shape)? | **No.** | Collapsing lost because the seam between them is where the rollback gate lives; merging puts the gate inside the thing it governs. |
| Route on price? | **No — route to the cheapest already-contracted path, commitment-aware.** | Cross-vendor arbitrage lost to evidence: 0–10% path deltas dominated by a 15–30% committed discount the customer already holds. |
| Who owns conformance suites? | **Core, invariant, protected, L3-gated.** | Component-author amendment lost because a suite an implementer can amend to pass is worthless. |
| Primary evidence? | **The attention/throughput boundary, then the cost ledger.** Adoption is necessary but is not evidence. | "Adoption is the evidence" lost because the thesis is about production cost; adoption measures whether the artifact mattered, a different and lesser claim. |
| Which coding agent is the default? | **opencode (MIT) in a browser terminal over ttyd (MIT); aider kept as an installed fallback.** §3.6. | Cline lost on shape, not licence — an IDE extension cannot be the bundled default when the claim is one login to three surfaces and the user installs nothing. The pass-through contract for a customer's own tool is unchanged. aider lost to opencode on behaviour: it makes the user nominate files first, and finding 23 measured it silently dropping a completed edit. |
| What is v0.1? | **Step 0 audit CLI + the intermediation deploy.** No GPUs, no factory, no trainer. Plus breakglass, availability and the exit path, which are not deferrable. | Deferring breakglass and HA lost because the egress mandate creates a company-wide single point of failure on the first day it is enforced. |
