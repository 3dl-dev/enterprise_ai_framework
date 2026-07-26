# Sealed conventional-cost estimate

> ## ⛔ WRITE-ONCE, SEALED
>
> This file must be completed and committed **before the first implementation commit**, and never
> edited afterward. Its commit hash is cited in every published result.
>
> It cannot be added later. The whole point is that it was written before anyone knew what the
> build would actually cost.
>
> **Status: NOT YET WRITTEN.** Implementation is blocked until it is.

---

## What this is for

The project's central claim is that production cost has collapsed far enough that a commodity
layer can be built outside a funded company. Testing that claim needs a comparator, and we cannot
see any funded company's internal costs.

This is the substitute: a written record of what a conventional funded team would have needed for
**this exact scope**, sealed before the work starts. The published result is the delta between
this estimate and what actually happened.

## The property that makes it work, and the way it fails

**The scope description must be tight enough that later drift is detectable.**

A loose scope can be satisfied by quietly building something smaller and declaring victory. That
is exactly what this document exists to prevent, and the prevention only works if a reader can
tell, afterward, whether what got built matches what was sealed.

**The void rule:** if the scope drifts, the row is marked void and says so. A void row is an
honest outcome. A re-scoped row is not a result at all — it is a story.

## To be filled in before the first implementation commit

**Scope, described tightly enough that drift is detectable:**
> _(enumerate the capabilities, not the components. "One login across chat, coding agent and API"
> is checkable; "identity integration" is not. Reference §7 of the design doc for the v0.1
> boundary and be specific about what is in and what is out)_

**Conventional team composition:**
> _(headcount and roles a funded company would assign to this scope)_

**Conventional duration:**
> _(with the reasoning, briefly)_

**Conventional cost:**
> _(loaded engineering cost for that team over that duration)_

**How the actual will be measured against it:**
> _(what counts as reaching equivalent scope; who adjudicates; what evidence gets attached)_

## Measurement rules that apply to the actual side

- **Freeze token costs at the rate table in force on the commit date.** Prices fall. Recomputing
  history at current prices destroys the series and manufactures the declining-cost result the
  thesis is trying to test.
- **Record which category each unit of work fell into** — founder-attention-gated (correctness,
  security, money, isolation: where agent output is reliably plausible-and-wrong and must be
  human-verified) versus fleet-throughput-gated (mechanical, verifiable by tests).
- **That boundary is the primary instrument.** The moat is exactly the attention-gated set. Each
  model release either moves an item across the line or does not. It is not monotone — reversals
  must be shown — and it measures two causes at once: frontier model improvement and our own
  verification scaffolding. Separate them, or the success of the scaffolding gets misread as
  frontier progress.

---

## Seal

Sealed at commit: _(hash, filled on the sealing commit)_
Date: _(date)_
