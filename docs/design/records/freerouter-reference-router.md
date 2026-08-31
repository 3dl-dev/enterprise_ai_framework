# Design record — freerouter is EAF's inference spoke (no proxy)

**Status:** settled design (operator, 2026-08-31). Supersedes this file's earlier framings
("freerouter behind LiteLLM"; "EAF capture tee") — **both were proxies, and the proxy is the
impedance.** freerouter is a bundled spoke that owns inference end-to-end; EAF integrates,
authenticates, and administers it, and never sits in the inference path.
**Consumes rulings from:** `-129` (no 3DL service in any recipient's data path), `-dee` (capture
everything, operator-store) — retargeted here onto freerouter.
**Realizes** freerouter/CLAUDE.md's "EAF consumes freerouter, reimplements neither." Forge's
residual role is untouched (operator's separate reconciliation).
**Two asks to the freerouter project** (its build + architecture-lock call; EAF files the
contract, does not implement): §A1 native Anthropic inbound, §A2 per-key/tenant text logging.

## The principle

EAF is a meta-project: it bundles spokes (chat, opencode, SSO, …) into one experience. **freerouter
is the inference spoke.** The API key and the API endpoint *are* freerouter's. Put nothing in front
of it — a proxy is where impedance enters (add a model to the source → re-expose it in a middle
layer → re-teach every spoke). Kill the middle layer and that chain collapses into one move.

## Contracts

**C1 — freerouter is a bundled spoke; EAF provisions, never proxies.** freerouter ships in the EAF
bundle (default `personal-gateway` profile). After SSO, EAF's control plane mints a freerouter
key scoped to the user's project/tenant and hands it over; the user (or a spoke) then talks to
freerouter **directly**. EAF is in the provisioning/admin path, never the inference path. "One
control plane": the control plane is the identity broker and the admin surface; freerouter stays
stateless-bearer and is administered *through* its APIs, not via a second console.

**C2 — model discovery replaces catalog plumbing.** Every consumer (chat, opencode, aider, agents,
and any user's own tool) discovers models live from freerouter's `/v1/models` (modality-tagged;
`author/slug/endpoints` for price). A model appearing in freerouter is immediately usable by
everyone — **no hand-maintained lists, no render step, no re-wiring.** (Epic `-7b5`.)

**C3 — freerouter is the one meter, and usage is unified in and out of EAF.** Because there is no
proxy and one gateway, an admin sees the same usage whether a request came from an EAF spoke or a
user's own script hitting the self-hosted freerouter with their EAF-issued key. Billing, portal,
and analytics read freerouter's `/v1/usage`, `/v1/generation`, `/v1/credits`. Per-key budgets are
freerouter's (402/429, in-path). This structurally moots `-d58` (freerouter reserves before
egress; the cache-hit bypass class cannot exist).

**C4 — prompt/response text logging is a freerouter parent-tenant policy, inherited down the
subtree** (`-dee`; operator-approved on the freerouter side 2026-08-31). Not a per-key opt-in: a
tenant, at provisioning, enables logging and sets a retention duration; every user beneath a
logging-enabled parent is captured and cannot opt out; off unless a parent enables it. **EAF is the
parent tenant** — it provisions keys via SSO, so it enables logging once on its subtree with its
retention window and every user beneath is captured *structurally by the nested tenant tree* — no
per-user cooperation, no layer inside EAF, and it covers out-of-EAF direct calls the same as
metering. Arch-lock: the flag + retention + content sink live in freerouter's product tree
(per-account setting + pluggable durable sink); `metering/` stays content-free (queried only for
the account parent chain). It is §A2. EAF's analytics/training/eval consume that store
(source-agnostic extraction already built).

**C5 — marketplace is an operator opt-in, default OFF.** The bundled freerouter can be flipped to
`operated-marketplace` (top-up, payouts, provider self-signup) for an operator who wants a metered
storefront. Out of the box EAF is an enterprise gateway, not a storefront.

## Asks to the freerouter project

**A1 — native Anthropic `/v1/messages` inbound (REQUIRED).** No proxy is allowed to translate, so
the spoke must speak Anthropic itself for Claude Code / the terminal harness. Contract: streaming
SSE; bearer/`x-api-key` → freerouter key; `anthropic-version` honored; `cache_control`
prompt-caching passthrough; tool use. Verified gap: freerouter's inbound today is OpenAI-shaped
only (`/v1/chat/completions`, `/completions`, `/responses`, `/embeddings`), no `/v1/messages`.

**A2 — parent-tenant prompt+response text logging, inherited down the subtree, with retention
(REQUIRED for `-dee`).** See C4. A per-account policy (enable + retention) that binds every child
beneath it; freerouter persists request+response content to a pluggable operator sink; `metering/`
untouched.

Both are freerouter's to build; filed as **freerouter-887** (A1) and **freerouter-6a7** (A2). A2's
confidentiality-posture gate is **operator-approved**; it is buildable, sequenced behind
freerouter's pricebook cutover. A1 is unblocked and on their docket.

## Guardrails

**G1 — `router.3dl.one` peering is 3DL-overlay config only, never a shipped default.** 3DL's
bundled freerouter holds no vendor keys and peers (buyer-side federation) with `router.3dl.one`,
which holds the real keys and settles — key consolidation; 3DL→3DL is compliant. If that peering
leaked into a non-3DL recipient's artifact, their inference would transit a 3DL service on 3DL
keys — `-129` reborn. Shipped default: the operator's own keys, or peering with the operator's own
upstream. Done-condition like `-8f9`: `grep -i router.3dl` of the shipped tree returns only
templates + this record.

**G2 — air-gap capable.** The default (own keys, marketplace off, no peering) runs with no 3DL
service in the path. Every 3DL convenience (peering, a shared upstream) is overlay-only.

## Transition

1. Bundle freerouter (C1); point the OpenAI-compat spokes (chat, opencode, aider, agents) at it;
   discovery from `/v1/models` (C2); meter/usage from freerouter (C3).
2. A1 lands → Claude Code / terminal point at freerouter directly → **LiteLLM removed from EAF
   entirely** (deliberately drops the "LiteLLM MIT core" default; recorded, not silent). No shim —
   a shim is a proxy.
3. A2 lands → per-key/tenant text logging on → `-dee` satisfied in and out of EAF.
4. Wire G1 as 3DL-overlay-only from the start.

## Not in scope

Changing forge. Implementing A1/A2 (freerouter's lane). Any EAF-side proxy, tee, shim, or rendered
middle catalog. Enabling the marketplace or the `router.3dl.one` peer by default.
