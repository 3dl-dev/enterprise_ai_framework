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

### 17. A public-funnel origin breaks the in-cluster OIDC backchannel

The most expensive failure of the deployment, and it presented as "login worked in my
test, then failed for the user."

The chat surface completes its OIDC token exchange **from inside the pod**, not in the
browser. With the issuer set to the Tailscale Funnel URL, the pod — which is not on the
tailnet — resolved that hostname to the *public* funnel ingress over IPv6 and got
`ECONNRESET`. The browser half of the flow worked perfectly, so a client-side test passed
while real logins failed with "An unknown error occurred".

**Fixed by** serving the same hostname and port on the gateway VM's LAN interface with a
real certificate (`tailscale cert`, which issues a genuine Let's Encrypt cert for the
tailnet name), and a `hostAliases` entry pinning that name to the VM's LAN address inside
the chat pod. Browser and backchannel now use one identical issuer URL by two different
routes. `tailscaled` binds `:8443` only on tailnet addresses, so the LAN listener does not
conflict.

**The lesson that generalises:** an OIDC integration has two independent network paths,
and a test that drives only the browser path proves only half of it. Any future check must
assert reachability *from the pod*, not just from the developer's machine.

### 18. The chat surface hides a rejected gateway key

When its virtual key is rejected, LibreChat does not surface the failure — it silently
falls back to the hardcoded `default` model list in `librechat.yaml`. The UI looks
healthy and offers models it cannot reach.

Found because the cluster was deployed with a virtual key minted against the *local*
compose gateway, which is a different database and therefore invalid. The surface showed
exactly the three fallback models instead of the eleven the gateway serves.

`deploy/bin/post-deploy.sh` now validates the key against the cluster gateway and mints a
new one if it is rejected. The general hazard remains: a healthy-looking model list is not
evidence that the surface can reach the gateway. Compare it against the gateway's own
catalogue, which is what the item-1 regression test does.

### 19. The chat route rejects non-browser clients

`/api/agents/chat` runs a User-Agent check and answers `Illegal request` to anything it
does not recognise as a browser. Legitimate anti-bot hardening, and worth knowing before
concluding a chat integration is broken: any non-browser test must present a browser
User-Agent or it never reaches the model at all.

Login rate limiting bit the same way — LibreChat allows 7 attempts per 5 minutes by
default, and a test run trips it, after which every login looks broken rather than
throttled. The cluster now sets a higher limit without disabling the protection.

### 20. Egress rules are outside the exit path

If the operator restricted egress so that only the gateway could reach providers, then
after the exit their surfaces will be blocked by the network rather than by this
software. Egress control is out of scope for this row, so the exit cannot undo it — the
generated README says so explicitly rather than leaving it to be discovered.


### 21. ttyd on loopback cannot be probed with `tcpSocket`, and the pod CrashLoops

The workspace pod runs ttyd bound to `127.0.0.1` so that the only route to the shell is
the oauth2-proxy sidecar. The kubelet dials the **pod IP**, so a `tcpSocket` probe on
7681 is refused every single time, and the container is killed and restarted at exactly
`failureThreshold x periodSeconds` — forever — while ttyd is in fact perfectly healthy.

The symptom is a pod stuck at `1/2 Running` with a rising restart count and container
logs that show a clean startup and an exit code of 0.

**Fixed by** an `exec` probe that opens the socket from inside the container
(`bash -c 'exec 3<>/dev/tcp/127.0.0.1/7681'`). It tests the same thing and it tests it
from the only place the listener exists.

### 22. An ingress rule with no `from` silently grants pod-to-pod egress

Measured on this cluster's CNI (kube-router, the k3s default). The workspace
NetworkPolicy denied egress to the whole pod CIDR, and one workspace could still reach
another workspace's oauth2-proxy on 4180.

The reason is that the CNI accepts a packet as soon as the **destination** pod's ingress
rules allow it, without also consulting the **source** pod's egress rules. Writing the
front-door rule the natural way — "port 4180, no `from`, anyone may knock" — therefore
punched a hole straight through the egress section that was supposed to be the isolation.

Two changes were needed, and neither works without the other:

- the ingress rule names its allowed sources (the LAN and the tailnet) instead of leaving
  `from` empty;
- the Service sets `externalTrafficPolicy: Local`. With the default `Cluster`, kube-proxy
  masquerades every arriving packet to the node's own address, so a workspace pod could
  simply dial the NodePort and arrive looking like the node — inside the allow-list.

**The lesson that generalises:** a NetworkPolicy's egress section is not self-enforcing.
Whether it holds depends on the ingress rules of everything it points at and on how the
Service rewrites source addresses. It is only isolation if you tried it from inside the
pod, which is what `tests-live/test_workspace.py::test_workspace_cannot_reach_the_cluster`
does.

### 23. aider drops a completed edit when the model names a file that is not in the chat

The load-bearing measurement for the coding-camp plan: can aider hold an edit format
against models that arrive as a generic OpenAI endpoint on our gateway, with no
model-specific tuning in aider's table?

Harness: `tests-live/aider_editformat_probe.sh`, run inside a real workspace pod against
the real gateway. Three repetitions of two tasks per cell — a single-file bug fix and a
two-file addition. A cell passes only if the project's own tests pass afterwards, never
because aider said it succeeded. Streaming on, which is what a person at the terminal
gets.

| model | `whole` | `diff` | `udiff` |
|---|---|---|---|
| `glm-5.2@deepinfra` | 6/6 | **3/6** | 6/6 |
| `glm-4.7@deepinfra` | 6/6 | 6/6 | 6/6 |

The headline is good: **both open models hold a real edit format through the gateway with
no aider-side model support at all**, and `whole` — the untuned fallback — never failed.
The coding-camp plan is not blocked on edit format.

The one failure is worth the whole exercise. Every `glm-5.2` + `diff` failure was the
same single-file task, all three of them, and every one failed **silently**: aider
emitted no format complaint, printed a plausible answer, and left the file untouched.

Isolating it: the failure disappears when the second file is already in the chat (3/3
pass), and it is not about the model's ability to write SEARCH/REPLACE — the block is
emitted correctly. GLM ends its reply by suggesting `python -m pytest test_app.py`. That
trips aider's file-mention heuristic; with `--yes-always` aider adds `test_app.py` and
re-prompts, and the edit from the first reply is discarded. The model's second reply then
asks for `app.py` to be added, because it is no longer in the chat.

Two contributing factors, both worth knowing:

- **`--yes-always` is not a neutral convenience.** It turns "shall I add this file?" into
  an automatic re-prompt that can throw away work already done.
- **`--no-stream` makes it worse.** With streaming off the same cell failed 4/4 rather
  than 3/3-of-6; the probe defaults to streaming for that reason.

**Acted on by** defaulting `glm-5.2@deepinfra` to `udiff` and `glm-4.7@deepinfra` to
`diff` in `deploy/workspace/model-settings.yml`, with the measurements recorded in the
file next to the values they justify.

**Postscript, 2026-07-29.** This finding contributed to replacing aider as the default
terminal agent with **opencode**, which explores the repo itself rather than requiring
files to be nominated first — so the failure mode above (a re-prompt to add a file
discarding the edit already made) has no equivalent. aider stays installed as a fallback
and these edit-format settings still apply to it. The ruling is in design §3.6. The
measurement itself stands and was not about the camp, despite the sentence above: it asked
whether an untuned open model can hold an edit format through this gateway, which is a
platform question with a platform answer — both models can.

### 24. GLM reasoning tokens are charged against the same output budget as the answer

`glm-5.2` and `glm-4.7` return `reasoning_content` alongside `content`, and the reasoning
counts as output tokens. A request with `max_tokens` set small enough comes back
`finish_reason: length` with a full reasoning trace and an **empty** answer — observed at
`max_tokens: 20`, where 98 tokens of reasoning were needed before the model would emit
the two characters it had been asked for.

The failure surfaces as the tool doing nothing rather than as an error. Aider's default
output allowance for an unknown model is small, so the workspace image pins
`max_tokens: 16384` for both GLM entries.

### 25. Revoking a key erased the bill for everything it had already spent

The most serious defect this row has produced, and it was silent in the direction that
matters: the number got smaller and nothing said so.

`GET /admin/spend` attributed spend by joining `LiteLLM_SpendLogs.api_key` to
`LiteLLM_VerificationToken`, which is the gateway's table of **live** keys. Three
supported operations delete from that table — revoking a disabled user's keys (item 6),
the exit path's revoke-all (item 9), and rotating a key when a surface is reprovisioned.
Every historical spend row belonging to a deleted key then joined to NULL and fell into
`(unattributed)/(unknown)`.

Found on the cluster while building the IDE surface. After a handful of workspace
reprovisions the bill read:

```
(unattributed) / (unknown)    requests=116   spend=0.082114
         baron / ide          requests=4     spend=0.002115
```

88% of all money spent, detached from the person who spent it, with no error anywhere.
Scope item 6 — "disabling a user in the IdP pulls every surface key" — was therefore
*destroying the audit value of item 4* every time it worked correctly.

**Fixed by** attributing from the alias LiteLLM stamps onto each spend row at request
time (`metadata->>'user_api_key_alias'`), which no later revocation touches, and keeping
the token join only as a fallback for rows written before that metadata existed. The same
bill after the fix, over the same data:

```
         baron / ide          requests=103   spend=0.075958
       student / ide          requests=7     spend=0.006080
(unattributed) / (unknown)    requests=14    spend=0.002190
```

The 14 that remain are genuinely unattributable — calls made with the gateway master key
during setup, which belong to no principal.

**Regression test:**
`TestItem6RevocationPropagates::test_revocation_does_not_erase_the_bill` — spend a
recorded request through a key, revoke the key, assert the request is still on the bill.

**The lesson that generalises:** a ledger must not be joined to mutable operational
state. Attribution has to be written down at the moment of the event, because everything
the event referred to is allowed to be deleted afterwards.

### 26. "Nothing to delete" is not an error, and treating it as one broke first provision

Small, but it is the shape of bug that survives review. `POST /admin/keys/issue` rotates
by deleting the old key before minting the new one, and the gateway answers **404** to a
delete matching no alias. That 404 escaped as a 500, so issuing a key worked only for a
surface that already had one — failing in precisely the two states it exists for: the
first provision of a surface, and reprovisioning after a revocation.

It stayed hidden because `provision-workspace.sh` calls `/admin/sync` first, which mints
any missing key, so the happy path always had something to rotate.

**Fixed by** an explicit `missing_ok` on `gateway.delete_by_aliases`, set only by the
rotation path. Revocation still treats a 404 as worth surfacing, because there it means
the ledger and the gateway disagree about what exists.

**Regression test:**
`TestItem2VirtualKeys::test_issue_works_when_the_gateway_holds_no_key_yet`.

### 27. The bill believes the caller about who spent the money

Attribution in `metering.py` ranks `LiteLLM_SpendLogs.end_user` above the username encoded
in the key alias. `end_user` is whatever the client put in the request body's `user`
field. It is not authenticated and it is not checked against the key.

So any holder of any virtual key can write any name onto their own spend. From a
workspace pod, with nothing but the key that pod is issued:

    curl http://gateway:4000/v1/chat/completions \
      -H "Authorization: Bearer $OPENAI_API_KEY" \
      -d '{"model":"fake-large","messages":[...],"user":"someone-else"}'

and `someone-else` appears on `GET /admin/spend` as a principal. One user can charge their
spend to another user, or to a name that belongs to nobody. Both surfaces that hold
per-user keys — the IDE workspace and the terminal agent — run code the user typed, so
this is reachable by design rather than by compromise.

**This contradicts finding 13**, which closes with "the ledger query already prefers
`end_user` where present, so this becomes correct the moment the surface is confirmed to
forward it". That reasoning holds only for a *trusted* surface forwarding an identity it
authenticated. It is the wrong rule for a surface the user has a shell on. The fix has to
distinguish the two — an alias-derived username is an assertion by the control plane, an
`end_user` is an assertion by the caller, and only the first is evidence. Ranking is not
the mechanism; provenance is.

**Not fixed here.** Filed as `enterpriseaiframework-522`.

**Regression test:**
`tests-live/test_workspace.py::test_spend_is_attributed_to_the_key_that_paid_for_it`,
written to the real claim and marked `xfail(strict=True)`. It fails today, deliberately
and visibly, rather than being softened into the presence check it replaced (a
`(user, 'ide')` row exists — true while the money lands on a different name). The strict
marker turns the suite red the moment 522 is fixed and the marker is left behind.

**The lesson that generalises:** a presence check is not an attribution check. "There is a
row for this user" and "this user's spend is on this row" are different claims, and the
first one passes for years while the second is false.

### 28. A sandbox is an authority boundary, not a CPU budget

The preview iframe is sandboxed without `allow-same-origin`, which stops a generated page
scripting, navigating or reading the Workshop around it. It does not stop that page taking
the main thread and never giving it back.

A voxel game the agent wrote — ~20,000 cells redrawn per frame — froze the entire tab,
terminal included, and Chrome offered repeatedly to kill the page. Reproduced twice
against the real file, once directly and once inside the sandboxed frame.

No in-page control can rescue this, because the thread a "Stop" handler would run on is
the thread being starved. A watchdog has the same problem. The only thing that works is
not starting the page without being asked: the drawer still reveals the moment something
exists, but it reveals an offer — *Run it here* or *Open in its own tab* — rather than a
running app. `/api/pulse` reports whether the page keeps running after load
(`requestAnimationFrame`, `setInterval`, `while(true)`, a `<canvas>`) so an animating one
is steered to its own tab, where the same bug costs one tab instead of the workshop.

### 29. ttyd sizes its terminal once, and only a WINDOW resize makes it re-measure

Reported as "the input box is off screen after reconnecting", reproduced exactly: 50 rows
in a pane that fits 38, viewport unchanged.

ttyd fits its terminal from whatever the frame measured when its client started. On a
reconnect — a project switch, New chat, a reload of that frame — it starts before the
surrounding layout settles, measures a taller box than it ends up with, and keeps a size
it no longer has. Nothing signals it afterwards, because only the ELEMENT changed size,
never the window. The frame is nudged to re-measure after every connect.

The same shape bites a hidden tab: a frame laid out while its tab is hidden measures zero.

### 30. opencode pins the model to the session, so a "model" setting alone does nothing

Once the terminal resumed its last session, changing the model in Settings wrote the
config, reconnected, and came back on the old model. Even the explicit flag loses:
`opencode --continue --model glm-4.7` paints "GLM 4.7" and then "GLM 5.2" as the session
loads over it.

There is no version of this that keeps both, so switching models now ends the session and
the UI says so first. Related: opencode keeps sessions in an sqlite db under
`XDG_DATA_HOME`, which defaulted to `$HOME` — an emptyDir — so "resume" was erased by
every pod restart until it was pointed at the PVC.

### 31. An unpriced model was never metering at $0 — the rule that assumed so hid 140 models

The catalogue generator excluded any model Forge had not explicitly priced, on the stated
grounds that it "meters at $0, so budgets never trip and the bill under-reports". That was
asserted confidently, in the generator's own docstring, and was wrong.

Forge charges request-time cost and draws budgets down for models with no PriceRecord —
demonstrated by running a real counter down with `kimi-k2-thinking`, which has none. Only
the rollups reported $0, and that was a Forge bug, since fixed. The catalogue now reads
`/v1/pricing/effective`, which prices every model and reports whether the number came from
a human (`default`) or the provider's own rate card (`catalog`). 148 models exposed, up
from 8.

What survives is narrower: a model quoted at exactly $0 is still excluded, because no
budget can bind on zero. That is a cap that cannot be expressed, not a price that is
missing.

### 32. Cache pricing belongs in `model_info`; in `litellm_params` it bills cached tokens at zero

deepinfra's prompt caching works and arrives intact — `deepseek-v3.2@deepinfra` reported
20,992 of 21,020 prompt tokens cached on a repeat call, through Forge and the gateway.
The gap was ours: the generated catalogue carried no cache pricing, so the ledger billed
cached tokens at the full input rate and the bill overstated by the whole discount,
worst exactly when caching worked best.

Putting `cache_read_input_token_cost` in `litellm_params` made it worse in the opposite
direction — cached tokens billed at ZERO — and only measuring caught it. In `model_info`
an identical 21,020-token call bills `0.00546710` cold and `0.00273814` cached, matching
`28*in + 20992*cache_read + 5*out` exactly.

Not every model caches: `glm-5.2@deepinfra`, the default coding model, reports no cached
tokens and Forge does not list `prompt_caching` among its supported params.

### 33. Green unit tests said the product worked on a day when opening a tab froze the browser

Every suite before this one was HTTP-level: status codes, bodies, JSON shapes. That proves
a file is served and says nothing about whether its JavaScript runs — and both surfaces
are almost entirely JavaScript.

`make test-browser` drives a real Chromium and fails on any console error; `make test-e2e`
does the whole journey with a real account and real money: one login, type at the agent,
watch a file appear, run it, publish it, then fetch that link with NO session at all.
Between them they caught a permanently-open settings dialog, a terminal that resized
itself, a preview that froze the tab, and a model picker that did nothing.

Three failures along the way were the TESTS, not the product: a context leak that piled up
a dozen agents in a 1-CPU pod until it answered 429, layout assertions that passed alone
and failed in a suite because the drawer opens by itself once a project has content, and
an assertion on the "Ask anything" placeholder that a resumed session correctly replaces.

### 34. The console and the CLI disagreed about who spent the money

`GET /admin/spend` on the cluster, 2026-07-29:

```
baron                     / ide      223 req   $1.2256
6a67b18069dba4d1126fef44  / chat     135 req   $0.2247
(unattributed)            / (unknown) 42 req   $0.1133
student                   / ide       38 req   $0.0532
6a680dd2d6a3e58bd5596392  / chat      21 req   $0.0034
```

The chat surface — the one most people use — reads as a column of hex. Not because
attribution is broken: `end_user` carries a per-user value, and
`control-plane/app/chat_identity.py` translates LibreChat's Mongo ObjectId to a username
correctly. Run inside the pod it loads its map and resolves
`6a67b18069dba4d1126fef44 → baron` on the first try.

The translation is applied in `portal.py`, at two call sites, and **nowhere else**.
`main.py` builds `/admin/spend` straight from `metering.spend_by_user_and_surface()`. So
the web console shows names and `make spend` shows ObjectIds, from the same query, over
the same money — and `make spend` is the query scope item 4 actually names.

Two things generalise, and the second is the expensive one:

- **A capability proven on one rendering of the evidence is not proven.** "The bill
  attributes chat spend to the right person" was true and false simultaneously, depending
  on which of two views you opened. This is the same shape as finding 27 — a presence
  check is not an attribution check — one level up: a *correct* check, applied to one of
  two readers.
- **It survived every test** because `make test` runs against the compose bundle, where
  chat identities are not LibreChat ObjectIds, so the code path that needs translating is
  never exercised. The fixture was more uniform than production.

**Not fixed here.** Filed as `enterpriseaiframework-f8c`, which requires a test asserting
the two renderings agree — the duplication, not the lookup, is the defect.

### 35. A migration moved where work is published; the server kept serving the old place

`GET /listing/baron/` and `GET /live/baron/` on the cluster, 2026-07-29 — both returned:

```
HTTP/1.1 200 OK
Content-Type: text/html
Content-Length: 5164
Last-Modified: Tue, 28 Jul 2026 04:39:46 GMT
```

5164 bytes of a game called "Pink Unicorn". Nothing on the cluster can regenerate it.
`publish(1)` writes to `/live/<user>/<project>/`; the publisher that wrote
`/live/<user>/index.html` was replaced when each project got its own path, precisely so a
second game would stop overwriting the link a parent had already been sent. The migration
changed the writer and left the reader alone, and nginx's default `index index.html`
kept serving the residue at 200.

`/live/baron/my-first-project/` — the shape the current code emits — returned 404 at the
same moment. So the only URL that answered was the one no longer generated.

Two things generalise:

- **A migration has a serving side, and it is the side nobody diffs.** The publisher was
  reviewed, tested and correct. Nothing was wrong with the code that writes. The defect
  was entirely in what the old data still meant to a reader that had not been told the
  layout moved — and the reader here is a config file, which is not where anyone looks
  for stale-data bugs.
- **It survived every test because the tests asserted URL shape.**
  `tests-live/test_portal.py::test_published_list_is_scoped` checks that
  `/live/<user>/` is a substring of each returned link. That assertion is true of a link
  serving anything at all, including four-month-old bytes, forever. The same shape as
  finding 27 and finding 34: the check was on the *form* of the evidence, never on what
  came back when you followed it.

There was a second, quieter half. `portal.py::my_published` does `resp.json()` on
`/listing/<user>/`, and that path was returning HTML, so the parse raised into a bare
`except: pass` whose comment reads "Nothing published yet is the common case". A user
with residue was reported as having published nothing — the failure dressed as the empty
state.

**Fixed** in `enterpriseaiframework-7bc`. At the user level, `index` is pointed at a name
nothing creates so a directory request always renders the freshly-generated project list,
and any request resolving to a regular *file* at that depth returns 410 Gone — by an `-f`
test rather than a filename heuristic, so extensionless residue is refused and a project
directory with dots in its name is not. Project depth is untouched: that is where an
`index.html` is exactly what `publish(1)` puts and what a parent is meant to open.
`tests/test_published_layout.py` runs the pinned nginx image against the config parsed out
of `62-published.yaml` over a tree holding both layouts, and asserts the returned bytes —
including for a user who has residue *and* a live project, which is the case an
over-broad fix silently takes offline.

### 36. The gateway's root credential was also a valid inference credential, so money could be spent by nobody

**Found by:** bisecting `GET /admin/spend?since=` against the live cluster, then reading
the raw ledger. Not by reasoning; the reasoning available at the time said this was
history.

`(unattributed)` held 42 requests and $0.113342, and finding 25 explained that bucket as
rows written before the alias metadata existed — harmless residue. Bisecting the window
said otherwise:

```
since -48h   unattributed = 42 req  $0.113342     (all of it)
since -24h   unattributed = 25 req  $0.110581     (98% of the money)
since -18h   unattributed = 25 req
since -16h   unattributed =  0 req
```

25 requests — 98% of the unattributed money — fell in a two-hour window on 2026-07-28
between 21:31Z and 23:31Z, on a cluster deployed some 44 hours before the measurement.
Not setup traffic. `/admin/export/spend` over the same window showed `glm-5.2@deepinfra`
at 16k–55k tokens a request, `deepseek-v3.2@deepinfra`, `glm-4.7-flash@deepinfra`, and a
burst of one-shot model probes. Somebody was working, and nobody was named against it.

**The mechanism.** Every one of those rows carries the same `api_key`:

```
api_key                       = c8c29e12b4e50a3139fc805172e0a5e2091f4cb62f9cc16cb3aea8b7eef4a560
metadata.user_api_key_alias   = null
metadata.user_api_key_user_id = "default_user_id"
```

That hash is `sha256(GATEWAY_MASTER_KEY)`. The gateway's *administrative root credential*
was also a valid credential on `/v1/chat/completions` and `/v1/messages`. It has no
`LiteLLM_VerificationToken` row to join to and stamps no alias on the spend row, so the
bill had nothing to name — through any rendering, ever.

Two consequences, the second worse than the first:

- **Attribution.** Real money lands on the one bill belonging to nobody.
- **Budgets.** Budgets bind to virtual keys. A credential with no key row has no budget
  and can never exhaust one, so the single admission point admits it without limit. The
  budget-stop outcome was never false, but it was never *reachable* on this path.

This is a third distinct defect in the same column, and the distinction matters because
the first two were fixed and did not cover it. Finding 25: a principal that exists, whose
rows were orphaned when revocation deleted the key. Finding 34: a principal that exists,
named differently by the console and the CLI. Finding 36: no principal at all.

**Fixed by** `deploy/gateway/require_principal.py`, a LiteLLM `async_pre_call_hook` that
refuses any inference request whose credential carries no key alias — the master key, and
equally any virtual key minted without one, since `key_alias` is optional on
`/key/generate` and an alias-less key produces exactly the same unnamable rows. Refusal
rather than a better label: attributing this spend to a synthetic `(root)` principal would
have made the number attributable-*looking* while naming none of the processes holding the
key, and would have restored neither budget enforcement nor revocation. The hook fires
only on the inference call types, so the master key keeps its actual job — minting,
revoking, and serving `/v1/models` — and merely stops being able to buy tokens.

Our own hermetic suite was one of the callers. Four tests drove `/v1/chat/completions`
with `master_headers` because it was the credential at hand, which is precisely how a
scripted path ends up holding root. They now mint a `username::surface` key like a real
caller (`named_key_headers`), per test rather than per session, because the exit-path test
revokes every key in the deployment.

**Regression test:** `TestAttributableSpendOnly`, five tests. Two of them assert the paths
this change did *not* touch — the master key still mints, lists, deletes and reads the
catalogue; a named caller's spend still arrives under their own name and surface — because
a refusal rule is easy to get right by refusing too much, and that fix would still look
green if only the refusal were tested.

**A trap in the assertion, worth stating.** "The `(unattributed)` bucket is empty" is the
wrong check and would be red on a healthy system. LiteLLM writes a `status = 'failure'`
row for a request its pre-call hook rejected, with zero spend, zero tokens and — by
construction — no alias. So refusals keep landing in that bucket, correctly: they cost
nobody anything and name nobody. What must hold is that nothing in the bucket ever
*consumed* anything, and that is what the test asserts, over a window it also proves is
non-empty and attributed. Measured with the hook disabled, a single master-key request put
`$0.000198` and 22 tokens into that bucket, so the assertion is a real detector rather than
a restatement of the 403 above it.
### 37. The workspace told its own agent it had no internet, and it was wrong

Five places in `deploy/workspace/` asserted, as fact, that the workspace pod has no
network: `AGENTS.md` rule 1 ("There is no internet here… Anything loaded from the web
arrives as nothing"), rule 5 ("There is no egress. Both hang and then fail"), the Stuck?
panel a child reads ("This room has no internet"), the Ribbon phrase ("That page needs the
internet, and this room has none"), and the `OFFLINE_REF` comment that justifies the whole
remote-reference counter.

Measured from inside a running workspace pod on 2026-07-29, not read off the YAML —
kube-router resolves a packet on the *destination's* ingress rules without consulting the
source's egress, so an egress rule alone proves nothing in either direction:

```
$ kubectl -n enterprise-ai exec ws-student-… -c ttyd -- \
    curl -sS -o /dev/null -m 10 -w 'http_code=%{http_code} remote=%{remote_ip}:%{remote_port}\n' \
    https://registry.npmjs.org/
http_code=200 remote=104.16.4.34:443

$ … https://pypi.org/simple/
http_code=200 remote=151.101.128.223:443
```

The live `workspace-isolation` NetworkPolicy's egress section is structurally identical to
what is checked in at `60-workspace-common.yaml`, so this is not deploy drift. The
`0.0.0.0/0`-minus-private rule carries no port restriction, is captioned "The internet, for
pip / npm / git clone", and has been there since the commit that created the surface. The
policy is deliberate. **The text is what was wrong.**

Two things generalise:

- **A false capability claim is worse than a missing one.** An agent told it has no network
  does not try, and then explains a blank page to a nine-year-old with a mechanism that
  does not exist. The claim is unfalsifiable from inside the conversation: nobody ever
  runs the command that would disprove it, because the rules say not to.
- **The claim was false for a second reason nobody had noticed, and that one survives any
  policy change.** The preview is `<iframe src="preview/">` — *the child's browser* fetches
  the page and every subresource named in it, not the pod. `shell-server.py` sends no
  `Content-Security-Policy` and the iframe's sandbox does not restrict subresource loading,
  so a CDN `<script src>` in a preview loads over the child's own connection. Even a
  fully-closed pod would not have made "anything loaded from the web arrives as nothing"
  true. Measured against the real server in
  `test_the_preview_does_not_block_remote_subresources`.

**Fixed** in `enterpriseaiframework-644`. Both house rules are kept — inline everything, do
not run installers — because they are good camp rules; only their false justification is
replaced, with the true one (a remote reference is the piece of a page that works at the
desk and is missing at the demo) and an explicit "this is a camp rule, not a limit of the
machine". The remote-reference counter is kept and re-documented as a house-rule check
rather than a capability check; its field name in `/api/pulse` stays `offline_refs`,
because that response shape is frozen.

`tests/test_workspace_network_claims.py` **derives** the obligation from the NetworkPolicy
instead of hard-coding it, in both directions: while the policy allows general internet
egress the text must not deny it, and if the policy is ever narrowed the agent's rules must
say so — otherwise an agent that does not know it is offline burns a user's turn on an
install that hangs. Both branches are fault-injected (the retired text is replayed verbatim
through the checker and must be caught; a synthetic restrictive spec must flip the
complaint), so neither the check nor the branch it does not currently take can rot green.

Still open, and attention-gated: the policy's *breadth*. Unrestricted egress to any host on
any port, from a pod that runs agent-written code next to a spendable virtual key, is a
deliberate setting that had never actually been chosen. Closing it entirely is the wrong
answer — the product is not the camp, and an enterprise's developers need `pip`, `npm` and
`git clone` — but narrowing the rule to TCP 80/443 costs only public `git+ssh` and buys the
loss of arbitrary-port outbound. Note that no CIDR-based egress policy can prevent key
exfiltration while package installs are permitted; the registry is a fine exfil channel.
That is a founder decision, not an agent one.

### 38. The archive the customer leaves with was the worst-attributed rendering of the ledger, and only because of the order the exit runs in

`GET /admin/export/spend` on the cluster, 2026-07-29: of 477 exported rows, **265 carried
an empty `key_alias` and an empty `end_user`** — no principal at all. The bill over the
same ledger, at the same moment, put only 42 requests in `(unattributed)`. So roughly half
the export was anonymous while the bill named it correctly.

The cause was already written down. `control-plane/app/export.py::spend_csv` attributed
every row through

```sql
LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = s.api_key
... COALESCE(v.key_alias, '') AS key_alias
```

which is exactly the join finding 25 established is wrong, because three supported
operations DELETE from that table. Finding 25 fixed `metering.spend_by_user_and_surface`
and left `export.py` on the old query. This is the third rendering of the finding 34 shape
— bill, portal, export — and the last one to be found.

What makes it worse than the column count suggests is the **order the exit path runs in**.
`bundle/bin/exit.sh full` revokes every virtual key and then exports. Revocation empties
`LiteLLM_VerificationToken`. So the export was at its least attributed at precisely the
moment it became the only record that outlives the deployment: the customer walks away
with a per-request ledger that cannot say who spent the money, and nothing left running to
ask.

Two things generalise:

- **A defect's severity depends on the sequence it is reached in, not on the code.** The
  same query is nearly correct when run casually and nearly useless when run as step three
  of leaving. Nothing in `export.py` said which one it was for. The blast radius lived in a
  shell script fifty lines long, in another directory.
- **It survived every test because the exit-path suite asserted `spend.csv` exists.** A
  presence check, again — finding 27 and finding 35 in a third costume. The file was
  always there; nobody had ever looked inside it after the revocation the same script had
  just performed.

**Fixed** in `enterpriseaiframework-37a`. There is now exactly ONE attribution expression,
`metering.ledger_attribution_sql`, and both the bill and the export build their SQL from
it; the bill's generated SQL is byte-for-byte what it was, so this is a share, not a
rewrite. The CSV gains a `principal` column — who the row is billed to, by the same rule
the bill uses, with a chat ObjectId translated to a username — while `end_user` stays
verbatim, because the export is evidence and a rewritten column cannot be reconciled
against a provider invoice.

`tests/test_scope_items.py::TestItem9ExitPath::test_the_export_still_names_the_spender_after_the_exit_revokes_every_key`
exports, revokes through the real `/admin/exit/revoke-all`, and exports again. It asserts
the named row survives, that the two exports agree byte-for-byte on every column the change
did not touch, and — the population-wide form of the cluster measurement — that revocation
strips the alias from **no** row that had one a moment earlier. Shown to have teeth by
reintroducing only the old join behind the new columns: the test then fails with
`exported with key_alias '' after the exit revoked exitattr-c3b6cbb6::ide`.

Measured on the bundle afterwards, over a ledger where every key had been revoked: the old
join blanks 4 of 4 rows, the shared expression blanks 1 — and that one is a request LiteLLM
*refused*, logged with `user_api_key_alias: null` and $0 spend, which is genuinely
unattributable and correctly labelled.

### 39. A request was served, charged upstream, and never billed — because the gateway shut down politely

`enterpriseaiframework-3f3` had stood open since 2026-07-28: one request, HTTP 200 from a
workspace pod on its own `baron::ide` key at 02:10:50Z, and no spend row. Two explanations
were disproved before this one, and both were disproved by measurement rather than argument:

- **Not a cache hit** (the theory this item was parked on for a day). Cache hits write
  their own ledger row — 1 miss + 1 hit + 1 refusal produced 3 rows on the bundle.
- **Not the master key** (finding 36's bucket). Master-key requests *do* land in the
  ledger, as `(unattributed)`. Reading the cluster's own export for the hour in question:
  69 rows, every one of them carrying `chat-surface::chat`, and **zero** rows with no
  alias. There was nothing to attribute, orphan, or mislabel. The row was never written.

The mechanism, reproduced on the compose bundle in three steps:

1. `POST /v1/chat/completions` → **HTTP 200**, 19 tokens, real content handed to the caller.
2. `docker compose stop gateway` — an ordinary SIGTERM; the container was down in 2.8s.
3. Bring it back and wait past every flush interval. **The row is never written.** A control
   request in the same run, with no restart, bills normally.

LiteLLM does not write the spend row inline with the response. Its logging callback appends
it to `prisma_client.spend_log_transactions`, a Python list, and a scheduled job commits the
batch every `random.randint(PROXY_BATCH_WRITE_AT - 3, + 3)` = **7–13 seconds**. Its
`proxy_shutdown_event` then disconnects Prisma, closes the cache, and flushes *Langfuse* —
and never drains that list. Read from the vendor source inside the running container, not
from documentation.

So this was never a crash-only edge case. **Every ordinary redeploy, rollout or `restart` of
the gateway silently discards up to thirteen seconds of billing for requests it has already
served and the provider has already charged for.** The gateway is restarted by every
deployment; the cluster's own ledger shows a 4m23s hole between 02:07:23 and 02:11:46 on the
night in question, with the unaccounted request sitting inside it.

Three things generalise:

- **The bill is not wrong here, it is short.** Nothing errors, no row is mislabelled, no
  total fails to add up. Findings 25, 34 and 38 were all about a row that existed and named
  the wrong person; this is the first one where the money leaves no trace at all, and it is
  correspondingly invisible — there is no `(unattributed)` bucket to notice.
- **"Served" and "recorded" were never the same event, and nothing in the system said so.**
  A 200 was treated as proof of a ledger row for the entire life of the project.
- **Two disproved hypotheses cost a day each** because both were plausible and neither was
  measured. The cache theory and the master-key theory were each written into the item as
  the likely answer. What resolved it was standing the bundle up and stopping a container.

**Fixed** in `enterpriseaiframework-3f3` by `deploy/gateway/flush_spend_on_shutdown.py`,
mounted alongside the other gateway callbacks: it wraps `proxy_shutdown_event` so LiteLLM's
own `update_spend` — the function its scheduler calls — runs *before* the database goes
away. We do not write the row and we do not reimplement metering; we make the vendor's flush
happen at the moment it forgot to. A FastAPI `on_event("shutdown")` handler was not an
option: LiteLLM builds its app with a custom `lifespan`, and Starlette ignores registered
shutdown handlers when one is supplied, so such a handler would look right and never run.

`tests/test_spend_survives_restart.py` holds it in two halves, because the end-to-end test
alone can pass for the wrong reason if the scheduler happens to tick before the stop. The
integration test serves three requests, proves at least one was still buffered, stops the
gateway, and asserts every row is on the bill **while the gateway is still down** — with the
right token count, non-zero spend, and the principal and surface from the key alias, because
a row existing is not a row being right. The ordering test carries no timing at all: it
replaces both the flush and the vendor shutdown with recorders inside the real gateway
container and asserts the order is flush-then-shutdown, which is the entire defect. All
three fail before the fix; the integration one fails with `3 of 3 requests were served with
HTTP 200 and are not on the bill`.

**Still open, deliberately.** SIGKILL, an OOM kill and node loss still discard the buffer —
measured, same three steps with `docker kill -s KILL`, same result. Surviving those needs an
inline write, which means owning the metering path we deliberately do not own. What is
closed is the frequent, operator-caused case. The live cluster is **not** yet fixed: it runs
a gateway config that predates this change (and predates finding 36's `require_principal`),
so it needs a `deploy/bin/deploy.sh` run to pick either of them up.
### 40. Two upgrade defects that a healthy container reports as healthy

Upgrading the chat surface from LibreChat v0.8.0 to v0.8.7 (`enterpriseaiframework-f50`)
against the unchanged `bundle/librechat/librechat.yaml`. The container came up, passed its
`/health` check, served the sign-in page, answered `/api/config` with our model specs, and
was wrong in two ways that nothing user-facing reported:

```
warn:  [memory] Agent config detected without explicit `enabled: true`.
       Automatic memory extraction is now opt-in.
       Add `memory.agent.enabled: true` to keep automatic memory updates.
error: [MCPServersRegistry] Failed to inspect server "echo":
       Domain "http://mcp-echo:8080" is not allowed
info:  [MCP] Initialized with 1 configured server and 0 tools.
```

**Memory.** From v0.8.6, `packages/data-schemas/src/app/memory.ts#isMemoryAgentEnabled`
returns false unless `memory.agent.enabled === true`. v0.8.0 extracted unconditionally.
The identical config file therefore means "extract" on one version and "never extract" on
the other. The memories panel still renders, `/api/memories` still answers, stored
memories still read back — the only thing that stops is anything new being learned. There
is no user-visible error at any point.

**MCP.** v0.8.6+ blocks MCP servers whose host resolves into private IP space
(`packages/api/src/auth/domain.ts#isMCPDomainAllowed` → `isSSRFTarget` /
`resolveHostnameSSRF`) unless exempted. Every MCP server we will ever run is on a private
network by construction — `mcp-echo:8080` is a compose service name and a k8s Service
name. One line at boot, then nothing: the tool simply is not in the list the model is
offered, so the model "chooses not to call it", which is indistinguishable from a model
being unhelpful. The fix is `mcpSettings.allowedAddresses: ["mcp-echo:8080"]` — host:port
exemptions, not `mcpSettings.allowedDomains`, because `MCPServerInspector.inspect` sets
`useSSRFProtection = !allowedDomains?.length` and an `allowedDomains` list therefore turns
SSRF protection off wholesale for MCP.

Neither is in the changelog. Both were found by reading the container's own boot log.

**The upgrade also has a coupling that is worse than either defect.** The bundle and the
cluster read the SAME `bundle/librechat/librechat.yaml` — `deploy/bin/deploy.sh` renders
that exact file into the cluster's `chat-config` ConfigMap. `mcpSettings` does not exist in
v0.8.0's `configSchema`, which is parsed with `.strict()` at the top level, and v0.8.0's
`loadCustomConfig` responds to a rejected config by logging and `return null`. A v0.8.7
config deployed against a v0.8.0 image therefore leaves the surface with **no custom
configuration at all** — no gateway endpoint, no modelSpecs, no memory, no MCP — while
`/health` keeps answering 200 and the sign-in page keeps rendering. v0.8.7 replaced that
with `process.exit(1)`, which is a real improvement, but only for the version that has it.
The image pin and the config file must land together.

**A third change, which is not a defect but breaks every client we have.**
`POST /api/agents/chat/<endpoint>` no longer streams. v0.8.0 answered `text/event-stream`
and the POST body *was* the stream; v0.8.7 answers `application/json` with
`{"streamId", "conversationId", "status": "started"}` and the answer arrives on a separate
`GET /api/agents/chat/stream/<streamId>`. A v0.8.0 client parses the JSON handle as SSE,
finds zero frames, and reports "stream ended with no terminal event" — blaming the model,
the gateway, or the credentials, none of which are involved. The stream endpoint is also
not a place to read an answer after the fact: against a fast upstream the job completes
and is reaped first, and the GET returns 404 for a turn that succeeded.

`tests/chat_turn.py` handles both shapes by not reading the answer off a stream at all —
it reads the persisted assistant message from `GET /api/messages/<conversationId>`, which
works on both versions and is the stronger claim anyway. `tests-live/test_mcp_echo.py` and
`tests-live/test_memory.py` still use the v0.8.0 shape and will break the moment the
cluster is upgraded; both carry a warning at the helper that says so.

**Assertions rather than eyeballs** live in `tests/test_chat_surface_version.py`: the tool
list the surface fetched from our own MCP server, the memory-extraction agent observed
running on a real turn, the model specs the browser is served, the agents endpoint's
capability list (`skills`, `subagents`, `execute_code`, `artifacts`), the config schema
version read out of the running image, and the titling call named with the model
`librechat.yaml` configures.

One incidental correction while there: `interface.artifacts: true` in
`bundle/librechat/librechat.yaml` has never done anything. The key is absent from
`interfaceSchema` in **both** v0.8.0 and v0.8.7, so zod strips it, and
`packages/data-schemas/src/app/interface.ts` never reads it — confirmed by its absence
from the served `/api/config`. What actually turns artifacts on is `preset.artifacts` on
each model spec. The key is left in place with a corrected comment and a test that fails
if a future release starts honouring it.

### 41. The test suites signed in as a real person and mutated their workspace session

**Found by:** a review of `enterpriseaiframework-68e`'s first attempt, which was rejected
for producing its evidence this way. Filed as `enterpriseaiframework-cf5`.

Four live suites resolved their identity out of `secret/workspace-user-student`:

```python
(_secret("workspace-user-student", "USERNAME"), _secret("workspace-user-student", "PASSWORD"))
```

`student` is a person. This was not a read. Opening the Code tab drives that account's own
pod — switching to it starts a session, rewrites `.meta/<project>.session` and `rm -f`s the
`.new-session` flag — and a measured run on the previous attempt changed both, which can
silently convert somebody's session. `tests-live/test_workspace.py` went further: it typed
into two people's shells (the second principal being `BOOTSTRAP_USER`, the founder), ran
`make test` there, deleted `.pytest_cache`, wrote `/workspace/OWNER.txt`, and let aider
rewrite and commit `app.py`. `tests-live/test_memory.py` planted durable memory in a
person's chat account and restarted the shared `chat` Deployment.

**The mechanism failure is the interesting part.** Every one of those files already carried
a comment saying not to run it while anybody was signed in. The warning was there and a run
happened anyway. `make test-browser`, `make test-e2e` and `make test-workspace` were
therefore unrunnable in practice, which blocked the done-conditions of items that needed
them.

**What it was NOT fixed with:** a throwaway cluster identity and a repointed fixture. That
relocates the hazard rather than removing it, and leaves the browser suite needing a cluster.

**Fixed by hosting the product instead.** `tests-live/portal_harness.py` (from 68e) already
served the shipped portal page, `require_user`, the `me()` payload, `workshop_proxy` and the
real `deploy/workspace/shell-server.py` on loopback over a throwaway projects root, minting
its own per-process token rather than reading the shared per-deployment secret. It was
extended to serve the settings sheet's handlers too — `my_spend`, `my_keys`, `my_published`,
`rotate_my_key` — with the stand-ins pushed out to the four OUTER data sources (Postgres,
LiteLLM's key list, chat_identity's Mongo lookup, the published volume's listing). Every line
of this repo's own filtering, translating, projecting and sorting still runs.

**Ten of twelve browser tests now need no account, no cluster and no credential**, and run in
about 9 seconds. The two that remain need ttyd's own xterm.js reporting what IT fitted to and
a real opencode resolving a real model; a stand-in for either makes the test a test of the
stand-in.

**The converted tests assert MORE than the live ones did, which is the unexpected result.**
Against the cluster, `test_portal_panels_actually_populate` could only check `total != "—"`,
`rows > 0`, `keys > 0` — presence checks, because a live deployment's data cannot be relied
on to contain a second account's spend row, a key carrying a secret, or a chat row keyed by a
LibreChat ObjectId. A fixture can. Five mutations of the shipped handlers were applied and
run: dropping the per-user spend filter (bill became `$7.51`), filtering before translating
the chat id (`$0.00421`, the chat row silently gone), dropping the key-owner filter, leaking
the key `token` into the alias, and letting a file in the published listing become a project.
**All five are caught now; the old suite would have missed all five** — `$7.51` and `$0.00421`
both satisfy "starts with `$`". Likewise the rotation test's "Decline. Nothing should be
rotated by a test run." was an unverifiable comment; the harness records every rotate call, so
declining is now a claim with a check behind it.

**Two gates, because either alone has a hole.** `tests-live/live_identity.py` is the only
place a human identity may be resolved:

1. `EAI_LIVE_TEST_USER` must name the account — no default, ever — and
   `tests-live/conftest.py` deselects marked tests at COLLECTION when it is unset. Collection,
   not setup: a session-scoped credential fixture is armed by collecting the file, so running
   one unrelated test in it was enough.
2. That account's Secret must carry `THROWAWAY`, which `deploy/bin/ensure-second-user.sh
   --throwaway` writes and a person's account does not have. Verified against the live
   cluster: `EAI_LIVE_TEST_USER=student` is refused, and refused *before* the password is
   read. The env var alone would leave the hazard one impatient command away.

`ensure-second-user.sh` also stopped repointing `secret/workspace-test-user` ("the account
tests-live signs in as"). Nothing had to be wrong for that indirection to aim the suites at a
camper — it named `student` — and nothing reads it now.

**Two defects in the guard itself, both found by running it rather than by reading it**, and
both worth recording because they fail in opposite directions:

* `request.node.get_closest_marker(...)` is the obvious way to ask "is this test marked", and
  it is wrong for any fixture broader than function scope: `request.node` is the Session or
  the Module, neither of which carries the test's markers. It returned None for a perfectly
  marked test and refused everything — failing CLOSED, which is the dangerous kind, because
  the marked suites become permanently unrunnable and the pressure goes on deleting the guard.
  `request._pyfuncitem` is the triggering item at every scope.
* The marker check inside the credential function cannot see CACHED reuse. A session-scoped
  fixture is set up once, so an unmarked test sharing it with a marked one gets the credential
  with no check at all and pytest never re-enters the fixture body. The first backstop watched
  a counter of credentials served and the probe walked straight through it, because nothing is
  served on a cache hit. What survives caching is the fixture CLOSURE: `item.fixturenames`
  names the fixture for every consumer. That probe is now
  `test_an_unmarked_test_cannot_reuse_a_cached_credential`.

**Guarded by `tests/test_live_suite_identity.py`**, in the hermetic suite so it runs on every
`make test` with no cluster and no credential — a guard that only runs in the suite it guards
is not a guard. Three of its twelve tests fail on the pre-fix tree (verified by restoring the
originals and running it). It pins the credential read to one module, drives pytest's own
collection to prove the gate holds in both directions, and pins the hermetic browser count so
that coverage cannot be traded for hermeticity — which is the failure mode this item existed
to avoid.

**Residual hazards, recorded rather than narrowed:**

* `tests-live/test_memory.py`'s `creds_a` still reads `BOOTSTRAP_USER`/`BOOTSTRAP_PASSWORD`
  from `bundle/.env` and signs in with them against the CLUSTER. If those are the same
  credential as the deployed realm's operator, that is still a login as the founder. Not
  changed here: the second half of its isolation claim needs an identity the throwaway pair
  cannot supply, and swapping it blind would change what the suite proves. The module is
  gated on both markers so it cannot run unattended.
* The three operator-console tests in `tests-live/test_portal.py` genuinely need a member of
  `PORTAL_ADMINS`, and the only operator on the deployment is a person. A throwaway account
  cannot become one without a control-plane configuration change, so
  `live_identity.operator_account()` requires `EAI_LIVE_TEST_OPERATOR` to name them
  explicitly every run instead of taking the founder's credential silently.
  `test_the_operator_sees_everyone` still asserts on the literal names `baron` and `student`;
  that is a real claim about the deployment's population and was left alone.
* No throwaway account exists on the cluster yet, so the gated suites currently fail loudly
  with the provisioning command rather than running. That is strictly better than the prior
  state, where they ran against a person, but it is not "green" — provisioning is a cluster
  write and needs a maintenance window.
* `tests-live/test_workspace_instructions.py` still creates ConfigMaps and Pods and runs
  `provision-workspace.sh` from several UNGATED tests. Every object it makes carries a uuid
  suffix and is deleted in a fixture teardown, and the accounts it drives (`cbftest`,
  `cbftest2`) were already throwaway — so it is not the identity hazard this finding is
  about, and only its two `workspace-tenant-instructions` tests were gated (that ConfigMap is
  the one every real pod mounts). Whether a suite that writes throwaway cluster objects should
  also need a maintenance window is a separate question and is not decided here. Deliberately
  not over-marked: gating a test makes it invisible, and this finding is partly about not
  trading coverage for safety.
