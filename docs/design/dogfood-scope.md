# v0.1-dogfood — scope

The founder is the first user. The use case is a Claude-like enterprise UX across three surfaces
behind one login, one bill, and one audit trail.

A strict subset of design §7.2. Each item is a checkable end state, proven by `make test`.

| # | Outcome | Status |
|---|---|---|
| 1 | A user authenticates once and reaches all three surfaces — web chat, IDE coding agent, terminal coding agent — without a second credential | done |
| 2 | No surface holds a provider API key; each holds a virtual key minted against the operator's own upstream credentials, revocable per user | done |
| 3 | Every request from all three surfaces transits one gateway speaking both OpenAI-compatible and Anthropic-native inbound, streaming without buffering | done |
| 4 | One metering ledger; a single query returns total spend by user and by surface across all three | done |
| 5 | One audit trail, surviving a restart of every component | done |
| 6 | Revoking a user in the identity provider stops their traffic on all three surfaces | done |
| 7 | A per-user budget stop enforced at the gateway — past the limit requests are refused, not merely recorded | done |
| 8 | The bundle starts from one command on a single host with no GPU, with fakes for upstream providers so it is testable with no provider account and no spend | done |
| 9 | A tested exit path: export the ledger, revoke virtual keys, restore direct provider keys, and every surface keeps working with the layer removed | not built |

## Out of scope

The closed-loop router and its four actuation axes; promotion gates and paired shadow evaluation;
semantic cache; provider-invoice reconciliation; the capture ledger and paired-reference storage;
the Perses console and the 3am kit; breakglass; multi-replica availability; egress control; the
conformance harness suite. Plus everything already deferred by §7.2 — GPU serving, the compute
contract, the trainer, the eval harness, the factory loop.

## Running it

```
make up      # one command, no GPU, no provider account
make test    # prove the items above
make spend   # the one bill, by user and surface
make audit   # verify the audit hash chain
make sync    # reconcile identity -> virtual keys (idempotent)
```
