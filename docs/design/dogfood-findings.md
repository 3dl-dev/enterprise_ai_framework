# v0.1-dogfood — findings from the first build

Things learned by running the thing, not by reasoning about it. Each one is either fixed
with a regression test, or open and recorded here so it is not rediscovered.

## Fixed

### 1. A model missing from the price map meters at $0, and budgets silently never trip

**Found by:** the budget-stop test failing while everything looked healthy.

The gateway prices requests from a model cost map. A model absent from it still serves
traffic and still counts tokens — it just records `spend = 0`. Nothing errors. The
consequences compound quietly:

- Per-key budgets are evaluated against accumulated spend, so a `$0` model can never
  exhaust any budget. Enforcement is vacuous rather than failed.
- The bill under-reports by exactly the traffic on unpriced models.

This is not a fake-provider artifact. Any real model the operator adds without pricing —
a new release, a fine-tune, a self-hosted endpoint — lands in the same state.

**Fixed by:** explicit `input_cost_per_token` / `output_cost_per_token` on every entry in
`bundle/litellm/config.yaml`, plus an unpriced-model detector at
`GET /admin/unpriced`. The warning is also inlined into `GET /admin/spend`, so the caveat
travels with the number instead of living on a page nobody opens.

**Regression test:** `TestPricingIntegrity::test_configured_models_record_nonzero_spend`.

### 2. The control plane stored raw virtual keys at rest

The first implementation persisted the `sk-` key returned by `/key/generate`, because
revocation appeared to require the key value. It does not: the gateway accepts
`key_aliases` on `/key/delete`, and accepts its own token *hash* wherever it documents
`key` for `/key/update`.

**Fixed by:** storing only the gateway's SHA-256 token hash, which is a join column
against the spend ledger and not a credential. `db.py` carries an idempotent migration
that renames the column and nulls any raw key left behind, so an existing deployment does
not keep a live credential at rest after upgrading. `sync` backfills unknown hashes from
the gateway rather than re-minting keys that are in active use.

### 3. `/key/list` rejects `size > 100` instead of truncating

Requesting a larger page returns a validation error, and code that reads `.get("keys",
[])` from the error body sees an empty list — so an unpaginated read looks like "the
gateway holds no keys" rather than like a failure. That is the worst possible shape for a
revocation check.

**Fixed by:** real pagination in `gateway.list_keys()`, and `raise_for_status()` on every
gateway call so a validation error can never be read as an empty result.

## Open — behavior to know about, not yet decided

### 4. A cache hit bypasses budget enforcement and writes no spend row

With the exact-match cache on, an identical request is served from cache. Observed
consequences:

- An over-budget key is **still served** from cache. No upstream cost is incurred, so
  this is arguably correct — but "the budget stopped them" is then not quite true, and an
  operator who disables a user expects silence.
- Cache hits produce **no spend row**, so the request count in the bill excludes them.
  Usage is under-reported even though cost is not.

This bit the test suite three separate times: fixed prompts made tests pass once and then
fail forever after, in a way that reads as flakiness rather than as a cache hit. Any test
touching metering or enforcement must use unique content.

**Not yet decided:** whether budget refusal should precede the cache lookup, and whether
cache hits should write a zero-cost ledger row so usage counts stay honest. Both are
plausible; the design doc does not currently rule on either.

### 5. Control-plane admin auth is a shared bearer token

`CONTROL_PLANE_ADMIN_TOKEN` is a single static secret, not OIDC-delegated admin identity.
Acceptable for a single-operator dogfood; it must not survive the row. Every admin action
is at least attributed in the audit trail — but attributed to `admin`, which is one
principal by construction.

### 6. Scope items 1 and 9 are not yet built

Item 1 (one login reaching all three surfaces) and item 9 (tested exit path) have no
implementation and no test. The spine they depend on — identity, virtual keys, metering,
audit, budgets — is built and tested. Until both are demonstrated, the row is incomplete.
