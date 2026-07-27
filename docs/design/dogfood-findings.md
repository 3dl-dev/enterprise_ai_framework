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

### 4. The chat surface shipped five routes around the gateway

LibreChat enables its built-in `openAI`, `anthropic`, `google`, `azureOpenAI` and
`bedrock` endpoints by default. Each talks to a provider directly. With no keys set they
fail, so nothing looks wrong — but the moment anyone supplies a key (the UI invites it),
that traffic leaves the building with no virtual key, no metering, no budget and no audit
entry. It is precisely the leak the one-control-plane constraint exists to prevent, and it
was on by default.

**Fixed by:** `ENDPOINTS=custom`, leaving exactly one selectable endpoint pointed at the
gateway, with `userProvide: false` so users cannot substitute their own key.

**Regression test:** `TestItem1OneLogin::test_only_the_gateway_endpoint_is_reachable_from_chat`,
which asserts the selectable set is exactly `{"Enterprise AI"}`.

### 5. OIDC forced TLS on the identity provider, and one issuer URL for everyone

Three problems in sequence, each hidden behind the last:

1. The chat surface's OIDC client refuses discovery over plaintext HTTP and exposes no
   override, so identity had to serve TLS. This is the right posture anyway.
2. With TLS on, Keycloak moves its management/health port to HTTPS too — so a plaintext
   health probe reports a perfectly healthy server as unhealthy.
3. The browser and the chat container must reach identity at the *same* URL, because the
   client validates that the discovered issuer equals the one it requested. Keycloak's
   `hostname-backchannel-dynamic` is designed to serve different URLs per channel and
   therefore *causes* this failure rather than solving it.

**Fixed by:** a self-signed certificate covering `localhost`, `identity` and the host's
routable IP; a health probe against the realm endpoint on the plain-HTTP port; and a
single issuer URL built from the host IP, which the browser reaches directly and
containers reach because published ports bind on the host.

### 6. An incomplete user record looks exactly like a broken SSO integration

Keycloak's Verify Profile required action halts the login flow at a "complete your
account" form when a user has no first or last name. The redirect chain simply stops
partway with no error. Provisioning now always sets a complete profile.

### 7. The unpriced-model detector counted cache hits as unpriced

A consequence of finding 7: a cache hit is tokens-counted at $0, which is exactly the
shape the unpriced-model detector looks for. It fired on healthy traffic during the Forge
integration.

A detector that cries wolf is one people stop reading, and that is precisely how a
genuinely unpriced model would later slip through unnoticed. Cache hits are now excluded
from the query.

### 8. Out-of-band keys would have survived the exit

The chat surface's virtual key is minted directly against the gateway by
`provision-chat-key.sh`, not by the identity reconcile — the surface is a shared client,
so there is no single user to mint it for. A revoke-all that walked only the control
plane's own `virtual_key` table would therefore have left it alive: an exit that leaves a
working credential behind is not an exit.

`/admin/exit/revoke-all` now takes the union of what the control plane recorded and what
the gateway actually holds, so anything minted out of band is caught too.


## Verified — results worth keeping

### 9. Our bill and Forge's agree to the cent

Worth recording as a positive result because it is the number the whole layer exists to
produce. Our gateway prices from Forge's published rate card; Forge meters independently.
For the same request (39 input, 27 output on `claude-haiku-4-5`) both compute
`$0.000184875`. Asserted every run by
`TestMoneyIsCorrect::test_our_computed_cost_matches_what_forge_billed`, which reconciles a
live request against Forge's own usage record rather than trusting either side.


## Open — behaviour to know about, not yet decided

### 10. A cache hit bypasses budget enforcement

With the exact-match cache on, an identical request is served from cache. Measured
behaviour, from a two-key probe (same prompt, different virtual keys):

| key | `cache_hit` | spend | tokens |
|---|---|---|---|
| A — populated the cache | — | $0.000189 | 19 |
| B — hit the cache | `True` | $0 | 19 |

So cache hits **do** write a spend row, **correctly attributed to the requesting key**,
with tokens counted and cost zero. Cost accounting is right: no upstream call was made.

> **Correction.** An earlier revision of this document claimed cache hits produce no
> spend row at all. That was wrong — it was inferred from a test that failed to find a
> row, rather than from the ledger. The table above is measured. The practical difference
> matters: usage counts are *not* under-reported, so the bill is trustworthy.

What remains true and unresolved:

- An over-budget key is **still served** from cache, because the budget is not consulted
  on a hit. No money is spent, so this is defensible — but "the budget stopped them" is
  not quite true, and an operator who disables someone expects silence.
- Any test touching metering or enforcement must use unique content. Fixed prompts made
  three separate tests pass once and then fail forever after, which reads as flakiness
  rather than as a cache hit.

**Not yet decided:** whether budget refusal should precede the cache lookup.

### 11. Control-plane admin auth is a shared bearer token

`CONTROL_PLANE_ADMIN_TOKEN` is a single static secret, not OIDC-delegated admin identity.
Acceptable for a single-operator dogfood; it must not survive the row. Every admin action
is at least attributed in the audit trail — but attributed to `admin`, which is one
principal by construction.

### 12. The chat surface is not behind TLS, and its session cookies are Secure

LibreChat marks `refreshToken` and `token_provider` as Secure. Browsers make a standard
exception for `localhost`, so signing in at `http://localhost:3080` works — but any
deployment on another host over plain HTTP will set the session cookies and then never
receive them back, presenting as "login succeeds, then I am logged out."

The bundle needs TLS terminated in front of the chat surface before it is reachable at
anything other than localhost. Not done.

The test suite emulates the browser's localhost exception explicitly rather than
weakening the surface's cookie flags to suit a stricter HTTP client.

### 13. Chat spend attributes to the surface, not to the person

The chat surface is a shared client holding one virtual key, so its traffic lands under
`chat-surface / chat` rather than under the signed-in user. The coding agents, which hold
per-user keys, attribute correctly.

Forwarding the user via `addParams: {user: "{{LIBRECHAT_USER_ID}}"}` was tried and
removed: that substitution is only demonstrably supported for headers, and an
unsubstituted placeholder writes the literal string `{{LIBRECHAT_USER_ID}}` into the
ledger as a username — a corrupted bill is worse than a coarse one. The ledger query
already prefers `end_user` where present, so this becomes correct the moment the surface
is confirmed to forward it.

### 14. Forge prices only 12 of its 68 models, and the unpriced ones are the interesting ones

Wiring Forge as the real upstream ran straight back into finding 1, this time with real
money and at scale. `GET /v1/models` returns 68 models and carries **no pricing**;
`GET /v1/pricing` (admin-gated) quotes only 12.

Of those 12, four are quoted at exactly `$0` — `whisper-large-v3-turbo`, `flux-1-schnell`,
`kokoro`, `orpheus` — all on the `local` path. That zero is real rather than missing:
they run on hardware already owned, so marginal token cost genuinely is zero. Our
unpriced-model detector cannot tell that apart from a missing price by looking at the
ledger, so those four are excluded until local-model accounting exists (capex, not a
token price).

That leaves **8 usable models**: six Claude and two GLM via DeepInfra.

The other 56 — every open-weight model, including `kimi-k2.5`, `glm-5`, `gpt-oss-120b`,
`deepseek-v3.2`, the whole Llama and Qwen families — have no price. They are excluded
from the generated catalogue, loudly. Including them would meter every request at $0:
budgets would not apply, the bill would under-report, and nothing would error.

This directly blocks "run your own model" on anything but the six Claude models until
Forge quotes prices or we add an operator-supplied price override.

**Mechanism:** `bundle/bin/render-gateway-config.py` generates the gateway catalogue from
Forge's live catalog joined to its rate card, and refuses to emit an unpriced entry.

### 15. Sovereignty pinning does not survive our gateway

Forge enforces sovereignty per request via an `X-Forge-Sovereignty` header, refusing
violations with a list of compliant alternatives. Our gateway does not forward caller
headers, so **a caller behind our layer cannot pin a stricter floor than their key's
default**. That is a capability regression introduced by intermediating.

It matters for the stated use case: handling data that must not touch foreign-origin
models is exactly when a per-request pin is wanted, and today the answer is to mint a
separate key with a stricter floor instead. Recorded as `xfail` in the live suite rather
than hidden, so the day the gateway forwards the header the test flips to passing.

Two smaller discrepancies against Forge's consumer quickstart, both worth reporting
upstream:

- A sovereignty violation returns **451**, not the documented **403**.
- `/v1/usage` time filters (`since`/`until`) return an empty list for any range. This
  fails closed and silently — an empty result reads as "no spend" rather than an error —
  so nothing here uses them. Forge tracks it as `forge-f22`.

### 16. Unexplained single hermetic-suite failure — watch

The hermetic suite failed once immediately after a live-suite run, during the direnv
work. The output was not captured, and it has not reproduced across six subsequent runs
including three full alternating live/hermetic rounds.

Recorded rather than dismissed: a failure seen once and not explained is not a failure
that has been fixed. If it recurs, capture the output before rerunning — the rerun is
what destroys the evidence.

A known coupling *was* found and fixed in the same area: the live suite used to borrow
the chat surface's virtual key from `.env`, which the hermetic exit-path test revokes and
re-mints. The live suite now mints its own dedicated key and revokes it afterwards.

### 17. Egress rules are outside the exit path

If the operator restricted egress so that only the gateway could reach providers, then
after the exit their surfaces will be blocked by the network rather than by this
software. Egress control is out of scope for this row, so the exit cannot undo it — the
generated README says so explicitly rather than leaving it to be discovered.

