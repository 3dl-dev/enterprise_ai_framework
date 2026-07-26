# Enterprise AI Framework — CLAUDE.md

> Project instructions. OS-level rules inherited from ~/.claude/CLAUDE.md.

## What this is

A control plane for enterprise AI spend. One layer everything routes through: one login, one bill,
one audit trail across every provider — then local models where the evidence supports it.

## Current milestone: VALIDATION, not construction

No production code is written until demand is demonstrated. The question is "does anyone care",
not "can we build it". Building it is not in doubt and is not interesting.

Do not start implementation work. If asked to build a feature, check the milestone first.

## Source of truth

1. `docs/design/design.md` — the architecture. Every ruling, the attack register, 14 known gaps.
2. `docs/design/brief.md` — the requirement and the reasoning behind it.
3. `rd` items — what is actually being worked.

## Standing constraints

- **Apache 2.0, no CLA, no enterprise tier, no feature held back.** Irreversible by design.
- **No telemetry to 3DL and no 3DL-operated service in any data path.** Air-gap capable.
- **Integrate, do not reimplement.** No inference engine, chat UI, coding agent, price catalog or trainer.
- **Component defaults must be OSI-approved with no user/seat/revenue/feature trigger.**
  Source-available and open-core are disqualifying as defaults; fine as documented swaps.
- **One control plane.** Twelve contracts must never become twelve consoles.
- **Optimize for verifiability under agent authorship.** The constraint is human review bandwidth,
  not code generation. Mechanisms that make correctness mechanically checkable are the point.
