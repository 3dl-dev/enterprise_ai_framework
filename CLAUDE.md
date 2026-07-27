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

## Current milestone: v0.1-dogfood. Build it.

The founder is the first user. The use case is a Claude-like enterprise UX across three
surfaces — web chat, an IDE coding agent, and a terminal coding agent — behind one login, one
bill, and one audit trail.

Build. Do not gate work on validation artifacts, pre-registration, or cost estimates; that thread
is closed and is not to be reopened.

Progress is tracked by the nine outcomes in `docs/design/dogfood-scope.md` and proven by
`make test`.

## Source of truth, in order

1. `docs/design/design.md` — the architecture. Every ruling with the losing argument stated, the
   attack register, and 14 known gaps. ~1500 lines; §7 is the buildable part.
2. `docs/design/brief.md` — the requirement and the reasoning that produced it.
3. `docs/design/dogfood-scope.md` — the nine outcomes v0.1-dogfood must demonstrate.
4. `docs/design/dogfood-findings.md` — defects found by running it, and open behavior.
5. `rd` items — what is actually being worked.

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

## What we will not do

Reimplement components that exist and are good. Run a waitlist, an email funnel, or a landing
page. Measure success by stars.
