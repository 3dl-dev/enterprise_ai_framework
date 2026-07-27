# Deferred work

Recorded here rather than in `rd`, which currently accepts writes but cannot read them
back (see the note at the bottom).

---

## Local-model cost accounting + SkyPilot — "run your own model" with an honest bill

**Deferred by the founder, 2026-07-27. Do not start without direction.**

### Problem

The ledger assumes per-token pricing. A self-hosted model has no per-token price at all —
it has a $/hour GPU. Two things are blocked today:

1. Forge's four `local`-path models (`whisper-large-v3-turbo`, `flux-1-schnell`, `kokoro`,
   `orpheus`) are quoted at `$0`. That zero is **real** — owned hardware, zero marginal
   token cost — but it is indistinguishable from a missing price when looking at the
   ledger, so `bundle/bin/render-gateway-config.py` excludes them.
2. Anything self-hosted via SkyPilot, including **Kimi K3**, which is not in Forge's
   catalog at all — self-hosting is the only route to it.

### Done condition

A model served from owned or rented hardware appears in `make spend` with a cost derived
from GPU-hours rather than tokens; the unpriced-model detector does **not** flag it; and
the four Forge local models are re-included in the generated catalogue.

### Constraints

- **Do not make the detector ignore `$0` blindly.** It exists because an unpriced model
  meters `$0` and budgets then never trip (finding 1). "Priced at zero on purpose" and
  "no price known" must be distinguished explicitly, not by heuristic.
- Integrate, do not reimplement: SkyPilot for provisioning, vLLM for serving.
- The bundle must still come up with no GPU and no provider account (scope item 8).

### Prior art in this repo

- `docs/design/dogfood-findings.md` findings 1 and 14 (Forge prices only 12 of 68 models)
- `docs/design/design.md` §6 — the factory and compute contract, deferred from v0.1 by §7.2

### Cheaper adjacent win — separate work, not this item

An operator price-override file would unblock the 56 unpriced Forge models — `kimi-k2.5`,
`kimi-k2-thinking`, `glm-5`, `gpt-oss-120b`, `deepseek-v3.2`, the Llama and Qwen families —
with none of the above. Roughly an hour. It extends the same code path this item would
build on.

---

> **`rd` is not usable in this repo right now.** It accepts `rd create` and returns an ID,
> and `.ready/nostr-log.jsonl` grows, but the projection only ever returns
> `switchboard-b6f`. Every item created on 2026-07-27 is invisible to `rd show` and
> `rd list`. Until that is fixed, deferred work lives in this file.
