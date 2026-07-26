# CLAUDE.md — Enterprise AI Framework

> OS-level rules (continuation identity, session protocol, rd workflow, model routing) are
> inherited from `~/.claude/CLAUDE.md`. This file is project-specific.

## Read this first

You are waking from sleep, not being born. Do not re-derive strategy. The first question is
**what was I executing**, and the answer is `rd ready` plus the execution pointer on the milestone
item.

## What this is

An open, self-hosted enterprise AI platform. One layer that all of a company's AI traffic routes
through: one login, one bill, one audit trail across every provider — then local models on owned
or rented hardware where the evidence supports moving them.

It is the layer that four separate open-source projects independently chose to paywall
(Open WebUI, Onyx, LiteLLM, Cline). The bet is that this layer is now cheap enough to build that
it no longer needs a paywall to exist.

## ⛔ Current milestone: VALIDATION. Do not write implementation code.

The milestone is **"does anyone care."**

Building it is not in doubt and is not interesting. The failure mode being avoided is a good tool
shipped to an empty room. **No production code until the validation threshold is met or provably
missed.** Both outcomes are results; the negative one gets published.

If asked to build a feature, check the milestone first and say so.

In scope right now: the pre-registration, the sealed estimate, distribution, responding to
inbound, and correcting the design when someone shows it is wrong.

## Source of truth, in order

1. `docs/design/design.md` — the architecture. Every ruling with the losing argument stated, the
   attack register, and 14 known gaps. ~1500 lines; §7 is the buildable part.
2. `docs/design/brief.md` — the requirement and the reasoning that produced it.
3. `docs/evidence/` — the pre-registration and the sealed estimate. **Write-once. Read the
   warnings in those files before touching them.**
4. `rd` items — what is actually being worked.

If a downstream artifact contradicts the design, flag the conflict explicitly rather than
silently adopting different numbers.

## Standing constraints

Decided and irreversible. Do not relitigate; if one looks wrong, raise an rd item rather than
quietly deviating.

- **Apache 2.0, no CLA, no enterprise tier, no feature held back.** Irreversible by design — no
  CLA means we cannot relicense either, and that is the point.
- **No telemetry to 3DL and no 3DL-operated service in any data path.** Air-gap capable. This is
  the precise form. "No phone-home" read as an absolute is wrong: the customer's layer calling
  the customer's own providers with the customer's own credentials is not phone-home.
- **Integrate, do not reimplement.** We never write an inference engine, a chat UI, a coding
  agent, a GPU price catalogue, or a trainer.
- **Component defaults must be OSI-approved with no user-count, seat, revenue or feature
  trigger.** Source-available and open-core are disqualifying *as defaults*; fine as documented
  swap options. A component whose *contracted capability* sits behind a paid tier fails the rule
  even if its core is permissive.
- **One control plane.** Twelve contracts must never become twelve consoles. Every conformant
  component delegates authentication, emits usage and signal in the standard shape, and is
  administered through the control plane rather than its own console.
- **Optimize for verifiability under agent authorship.** The binding constraint is human review
  bandwidth, not code generation. Mechanisms that make correctness mechanically checkable —
  invariant tests that outrank the implementation, executable conformance suites, adversarial
  harnesses on the isolation boundary, reconciliation against redacted real-invoice fixtures —
  convert attention-gated work into throughput-gated work. That is not hygiene, it is the
  mechanism of the thesis.
- **Nothing is specialized to a particular model.** A new open model is a serving-port
  implementation and a trainer target, never a rewrite.

## Component defaults (decided, with the traps that produced them)

LibreChat · Cline **base only** — its enterprise tier is out of scope, do not integrate it as a
convenience · vLLM · **Valkey not Redis** — Redis is tri-licensed since 8.0 and its default
packaging steers to non-OSI · **Perses not Grafana** — Grafana is AGPL; if swapped in, configure
via `auth.proxy`, never patch, or our integration code becomes AGPL inside an Apache bundle ·
Keycloak or Ory · **Postgres, welded** — not a swap port · Axolotl · SkyPilot · **LiteLLM MIT
core only** — its `enterprise/` directory is out of scope.

## Two deadlines that have already effectively arrived

1. **The sealed conventional-cost estimate** (`docs/evidence/conventional-estimate.md`) must be
   written **before the first implementation commit**. It cannot be added afterward without
   voiding the row it protects.
2. **The validation threshold** (`docs/evidence/preregistration.md`) must be written **before any
   outreach**, or every outcome can be narrated as vindication.

## What we will not do

Reimplement components that exist and are good. Run a waitlist, an email funnel, or a landing
page. Measure success by stars.
