# Behavioral analytics — transcript sources and the normalized record

> SPIKE output for `enterpriseaiframework-1d0`, gating the normalizer (`-1a8`) and the
> ledger join (`-0e90`). This record maps every surface's session store to one
> harness-agnostic turn/session schema, and names what each source is missing. It is the
> reference the extraction path (design.md notwithstanding) is built against.

## What we are building, in one line

The DAP practice kit scrapes Claude Code's local JSONL to produce
`3dl.dev/coding-vs-orchestration.html`. This feature does the same measurement *inside the
control plane*, over the product's own surfaces, and — its one advantage over the public
page — prices it from the **real billed ledger**, not an estimated rate card.

## The three sources, and the fourth that is not one

| Surface | Alias/surface tag | Transcript store | Holds content? | Holds cost? |
|---|---|---|---|---|
| `terminal` / `ide` (opencode over ttyd) | `<user>::terminal`, `<user>::ide` | **SQLite** on the per-user PVC | yes | yes (also in ledger) |
| `chat` (LibreChat) | `chat-surface::chat`, disambiguated by `end_user` | **MongoDB** `librechat` on `chatdb` | yes | tokenCount only |
| `agents/<name>` (openclaw/hermes) | `<user>::agents/<name>` | **none in these manifests** | no | ledger + agent_usage |
| gateway ledger + audit | — | Postgres `SpendLogs`, `audit.jsonl` | **no** | yes (authoritative) |

The gateway is **content-free by design**: `bundle/litellm/config.base.yaml` sets only
`success_callback: [postgres]` (a spend row per request) — no prompt/response body storage,
no redaction pipeline, because there is nothing to redact. So the ledger supplies
*model + surface + user + tokens + real spend + timestamp + cache_hit* per request, and
transcript **content** (turn structure, tool calls, prose, edits) lives only in each
surface's own store. The two are joined, not merged.

The `agents` surface has **no transcript store** in the deploy manifests — its behavior is
not measurable from a transcript, only its spend (ledger) and residency (`agent_usage`). It
is out of scope for this feature's content metrics; note it and move on.

---

## 1. opencode — SQLite (`terminal` and `ide`)

**Location.** `XDG_DATA_HOME=/workspace/.agent-state` (set in
`deploy/k8s/61-workspace.template.yaml`), so the db is
`/workspace/.agent-state/opencode/opencode.db` on the per-user PVC `ws-<user>`. The redirect
off `$HOME` (an emptyDir) onto the PVC is finding 30 — without it, "resume" was wiped every
restart. opencode is pinned to **1.18.7**; the schema below is that version, dumped from a
real db.

**Schema** (drizzle-migrated SQLite):

- `session(id, project_id, parent_id, slug, directory, title, agent, model, cost,
  tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write,
  time_created, time_updated, metadata, ...)`
  - `parent_id` → **subagent hierarchy**: an opencode subagent is a *child session*, not an
    `Agent` tool call. This is the biggest divergence from Claude Code and the normalizer
    must handle it (see "dispatch" below).
  - `model`, `cost`, `tokens_*` are opencode's *own* accounting — used for attribution and
    cross-checked against the ledger, never trusted for the bill.
- `message(id, session_id, time_created, time_updated, data)` — `data` is a JSON blob:
  `{role: "user"|"assistant", agent, modelID, providerID, cost, tokens:{input,output,
  reasoning,cache:{read,write}}, time:{created,completed}, finish}`.
- `part(id, message_id, session_id, time_created, data)` — `data.type` ∈
  **`text` · `reasoning` · `step-start` · `step-finish` · `tool` · `patch`**:
  - `tool` → `{tool: "<name>", callID, state:{status: "completed"|"error", input, output,
    metadata:{exit, truncated}}}` — **tool-call counts, failures (`status=error` or nonzero
    `exit`), test-runner detection (bash `command`)**.
  - `patch` → `{hash, files:[<path>...]}`. **Correction (item -1a8, verified against a
    live db):** `patch` parts are *sparse and not one-per-edit* — the real coding signal is
    the `edit`/`write` **tool** part, which carries the written content, target path and
    status. The normalizer counts edits from tool parts, not patch parts. This record's
    earlier "patch is authoritative" was the plan; the implementation corrects it.
  - `text` / `reasoning` → assistant prose and thinking (prose metrics run on the final
    `text` part of the turn).

**Sample obtained:** local `~/.local/share/opencode/opencode.db` (opencode 1.18.7, identical
to the workspace image). Dev workspace pods `ws-baron|ws-claire|ws-student` carry the same
db on their PVCs.

## 2. LibreChat — MongoDB (`chat`)

**Location.** `mongodb://chatdb:27017/librechat` — StatefulSet `chatdb` (`mongo:7`,
`deploy/k8s/11-data.yaml`), db `librechat`. Meilisearch is a *derived* index over this Mongo
(the `mongoMeili` plugin), not a second source of truth; Valkey is response cache only (no
PVC). So the one source is Mongo.

**Schema** (fields from the live dev `chatdb`):

- `messages`: `messageId, conversationId, parentMessageId, isCreatedByUser, sender, text,
  model, endpoint, tokenCount, user, createdAt, unfinished, error`.
  - **role** = `isCreatedByUser` (there is no assistant/user enum; the flag is it).
  - **turn threading** = `parentMessageId` chain (not positional order).
  - **prose** = `text`; **model** per message; **tokenCount** (not cost).
  - `user` is LibreChat's internal ObjectId — the same hex `chat_identity.attribute` already
    translates to a username for the bill. Reuse that mapping; do not re-derive.
- `conversations`: `conversationId, title, model, endpoint, user, messages[], createdAt`.
- `toolcalls`: tool invocations live in their **own collection**, keyed by conversation/
  message — chat tool-use is not inline in `messages.text`.

**Chat is a prose surface, not a coding surface.** No patches, no file edits. Its
measurable families are **prose**, **escalation** (question/permission/hedge density), and
**tokens** — not `code`. The metric extractor must emit per-family `null`/absent for a
surface that cannot produce that family, never a misleading zero.

## 3. Gateway ledger + audit — the cost join, not a transcript

- `LiteLLM_SpendLogs` (gateway Postgres): per **request** — `model, spend, prompt_tokens,
  completion_tokens, total_tokens, startTime, api_key(hashed), end_user, cache_hit,
  metadata->>'user_api_key_alias'`. Read read-only by `control-plane/app/metering.py`.
  Attribution rule lives in exactly one place, `metering.ledger_attribution_sql` —
  **reuse it**, do not re-implement the alias/end_user precedence.
- Alias grammar: `<principal>::<surface>` where surface ∈ `chat|ide|terminal` or
  `agents/<name>` (`control-plane/app/gateway.py`). This is the join key.
- `audit.jsonl` — hash-chained, rows carry `surface`; useful for corroborating a session's
  surface/time window, not for content.
- Timing caveat: spend rows are batch-flushed (7–13 s), plus a shutdown flush
  (`deploy/gateway/flush_spend_on_shutdown.py`). A ledger join over a session window must
  tolerate a few seconds of skew at the tail.

---

## The normalized record (harness-agnostic)

Two record kinds, mirroring the practice kit's `model-behavior.py` so the metric extractor
and the published page's `metrics.json` shape carry over. **Content-free**: labels and
counts only, never user text — this is what lets the analytics respect tenant isolation and
stay air-gap-clean.

```jsonc
// one per human→assistant turn on the main thread
{ "k":"turn", "tenant":"<id>", "surface":"chat|ide|terminal", "session_id":"…",
  "principal":"<user>", "model":"<id>", "effort":"<low|high|null>", "ts":"<iso>",
  "config_fingerprint":"<hash of system-prompt+toolset+effort, see below>",
  "interrupted":false, "next_human_nudge":false,
  "tools":{"bash":3,"edit":2,…}, "n_tool_calls":9,
  "dispatched":true, "work_after_last_dispatch":4, "output_tokens":1234,
  "final_text":{ "chars":812,"words_per_sentence":18.4,"unique_word_ratio":0.62,
    "bullets_per_1k":2.1,"headers_per_1k":0.0,"bold_per_1k":1.2,"table_rows_per_1k":0,
    "permission":0,"limitation":1,"caveat":0,"hedge":2 } }

// one per session, with subagent fan-out census + coding aggregate (main + subagents)
{ "k":"session", "tenant":"<id>", "surface":"…", "session_id":"…", "principal":"<user>",
  "model":"<id>", "n_subagents":3, "n_workflow_runs":0, "agents_per_wave":3.0,
  "subagent_output_tokens":9000,
  "code":{ "edits":14,"chars_written":4200,"edit_failures":1,"tool_errors":2,
    "rework":3,"tests":5,"commits":2,"reverts":0 },
  "cost":{ "ledger_spend_usd":0.42,"source":"ledger","tokens":123456 } }
```

### Per-surface mapping (the normalizer's job)

| Normalized field | opencode | LibreChat |
|---|---|---|
| turn boundary | `message.role` transitions (user→assistant→…→next user) | `parentMessageId` chain + `isCreatedByUser` |
| model | `message.data.modelID` (fallback `session.model`) | `messages.model` |
| tool calls | `part.type='tool'` → `.tool` | `toolcalls` collection |
| dispatch | **child `session` via `parent_id`** (+ `task` tool) | n/a (chat rarely dispatches) |
| edits / code | `part.type='patch'` `.files` | none (emit `code=null`) |
| prose | last `part.type='text'` of the turn | `messages.text` where `!isCreatedByUser` |
| tool failure | `state.status='error'` or `metadata.exit≠0` | `toolcalls` error / `messages.error` |
| tests / commits | bash `part` command regex | n/a |
| tokens/cost | `session.tokens_*`,`cost` → **overridden by ledger** | `tokenCount` → **overridden by ledger** |
| effort | `message.data` / request metadata | request metadata |

**Dispatch, restated:** the practice kit's "work after last dispatch / cold-stop" metric
keys on `Agent`/`Workflow`/`Task` tool calls. opencode has no such tool call — a delegated
unit is a child session (`session.parent_id`) and, in newer opencode, a `task` tool part.
The normalizer resolves dispatch to *"a child session was created / a task tool ran during
this turn"* so the orchestration family is comparable across harnesses.

### config_fingerprint — the "harness config" axis

The user's ask is to compare *harness configs*, not just models. A config is the
`(system-prompt/instructions, tool set, default model, effort profile)` a session ran under.
opencode's is `deploy/workspace/opencode.json` + the tenant instruction/skill ConfigMaps
mounted at `/etc/opencode/*`; LibreChat's is its endpoint/agent config. The normalizer
stamps each session with a stable `config_fingerprint` (hash of the resolved config) so the
slicing item (`-2df`) can group sessions by config. **Open question deferred to the
normalizer:** whether the running config is recoverable from the transcript alone or must be
captured from the pod's mounted ConfigMaps at ingest time — resolve when building fixtures.

## The ledger join (item `-0e90`)

Per normalized session (and optionally per turn): sum `SpendLogs.spend` for
`alias = <principal>::<surface>` over `[session.start, session.end + skew]`, via
`metering.ledger_attribution_sql`. Yields real `cost_per_edit`, `cost_per_turn`,
`cost_per_session` — the product's edge. A session with **no** matching ledger rows is
`cost: {source: "none"}` → rendered "cost unknown", **never** a silent `$0` (the
`unpriced_models` / finding-4 lesson: a bare zero hides an unmetered path).

## Constraints the ingestion design must honour

- **Content-free.** Normalized records carry counts + labels, never transcript text. Metric
  regexes (permission/hedge/…) run at ingest and only their *counts* persist. This is what
  keeps the feature tenant-safe and air-gap-clean (no 3DL in any path).
- **Tenant isolation.** Every record is tenant + principal + surface labelled; a slice never
  crosses tenants. opencode dbs are per-user PVCs (natural isolation); LibreChat is one Mongo
  keyed by `user` — the slice must filter, and reuse `chat_identity` for the hex→name map.
- **Durability reality (corrects "everything persistent on k3s").** Workspace PVCs and
  `chatdb`/Meili PVCs are `local-path` (node-local): opencode sessions survive a **pod
  restart** but **not** a k3s-worker rebuild (`61-workspace.template.yaml` L18-22;
  `01-tank-pvs.yaml` staged, unapplied, and does **not** cover workspaces). So ingest must
  be **incremental and pull-on-a-schedule** — a corpus is whatever survives on the PVCs at
  scrape time; treat missing history as expected, not an error. This argues for periodically
  extracting normalized records into a durable control-plane store rather than reading the
  live sqlite/Mongo at report time.
- **Read-only at the source.** opencode's sqlite is live and WAL-mode; open it
  `mode=ro`/`immutable` or over a copy, never write. Mongo reads are read-only by convention
  like `metering.py`.

## Gap vs Claude Code JSONL (what changed)

| Claude Code JSONL assumption | Product reality |
|---|---|
| one JSONL file per session, line-per-record | opencode = SQLite blobs; LibreChat = Mongo docs |
| `isSidechain`/`isMeta`/`<system-reminder>` filter human turns | opencode `role`; LibreChat `isCreatedByUser` + `parentMessageId` threading |
| subagents = `subagents/**/*.jsonl` under the session | opencode subagents = child `session` rows (`parent_id`) |
| dispatch = `Agent`/`Workflow`/`Task` tool call | opencode = child session / `task` tool part |
| cost = estimated from a price table | **real `SpendLogs.spend`, joined by alias** |
| single machine, transcripts rotate ~30d | per-user PVCs, node-local, non-durable → incremental pull |

## Implementation status

Built and tested under `control-plane/app/analytics/` + `control-plane/tests/test_analytics_*`:

- **`-1a8` Normalizer** — DONE. `measure.py` (prose/escalation primitives), `schema.py` (turn
  close + coding aggregate), `opencode.py` (SQLite), `librechat.py` (Mongo). Content-free.
- **`-e32c` Metric extraction** — DONE. `metrics.py` → the five-array `metrics.json` + persist
  & code indices; `build_metrics(key=…)` groups by any dimension.
- **`-0e90` Ledger join** — DONE. `ledger.py` stamps real billed cost by alias + turn window,
  reusing `metering.ledger_attribution_sql`; no-match → `source:"none"`, never $0.
- **`-2df` Slicing** — DONE. `slicing.py` — model / surface / tenant / principal / effort,
  tenant-isolated. `config` wired but pending the fingerprint (below).
- **`-130` Golden + adversarial fixtures** — DONE, wired into `make test`.
- **`-d27` In-product report surface** — DONE. `report.py` + portal routes
  `/portal/analytics` (page) and `/portal/api/analytics` (data), operator-only;
  `portal_static/analytics.html` renders the comparison with a dimension switch.

Follow-ups (filed):

- **`-5de` Ingestion collector** — the scheduled job that reads the live opencode PVC sqlite +
  LibreChat Mongo, normalizes, joins the ledger, and writes the durable records store the
  report reads. Until it lands the page shows "No data yet".
- **`-6b1` config_fingerprint** — stamp the harness config on records so the `config` slice has
  data (needs the pod's mounted config at ingest).
