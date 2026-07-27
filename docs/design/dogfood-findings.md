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

### 7. The chat surface is not behind TLS, and its session cookies are Secure

LibreChat marks `refreshToken` and `token_provider` as Secure. Browsers make a standard
exception for `localhost`, so signing in at `http://localhost:3080` works — but any
deployment on another host over plain HTTP will set the session cookies and then never
receive them back, presenting as "login succeeds, then I am logged out."

The bundle needs TLS terminated in front of the chat surface before it is reachable at
anything other than localhost. Not done.

The test suite emulates the browser's localhost exception explicitly rather than
weakening the surface's cookie flags to suit a stricter HTTP client.

### 8. Chat spend attributes to the surface, not to the person

The chat surface is a shared client holding one virtual key, so its traffic lands under
`chat-surface / chat` rather than under the signed-in user. The coding agents, which hold
per-user keys, attribute correctly.

Forwarding the user via `addParams: {user: "{{LIBRECHAT_USER_ID}}"}` was tried and
removed: that substitution is only demonstrably supported for headers, and an
unsubstituted placeholder writes the literal string `{{LIBRECHAT_USER_ID}}` into the
ledger as a username — a corrupted bill is worse than a coarse one. The ledger query
already prefers `end_user` where present, so this becomes correct the moment the surface
is confirmed to forward it.

### 9. Scope item 9 is not built

The tested exit path — export the ledger, revoke virtual keys, restore direct provider
keys, confirm every surface still works with the layer removed — has no implementation
and no test. Item 1 is now done; items 2-8 were already done.
