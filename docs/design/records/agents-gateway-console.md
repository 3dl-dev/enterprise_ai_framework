# Design record — the Agents pillar is gateway agents with native consoles

**Status:** corrects `docs/design/records/agents-surface.md` for the resident-agent and console
model. That record built the "Agents" surface on **`opencode serve`**; that was a conflation of
two different pillars. This record supersedes its **Contract 2 (residency)** and its
**console-attach** model. Its Contracts **1 (alias), 3 (metering), 4 (integrated vs BYO), 6
(Code-untouched)** are pillar-agnostic and **carry over unchanged**.

**Referenced by:** `docs/design/design.md` §12 (which is corrected alongside this record).
**Epic:** `enterpriseaiframework-da7`. **Ground truth:** the live cluster (2026-08-13), where the
resident agents run `nousresearch/hermes-agent` — not opencode.

---

## The one correction, stated first

There are **two pillars**, and they are different functions that must never be mapped onto each
other:

- **Code pillar → opencode.** A **coding** harness. It is a terminal tool: ttyd spawns
  `opencode` per websocket, and the process dies on disconnect (finding 43). This is correct for
  Code, where the agent is a tool a person drives while looking at it. **opencode is not the
  Agents pillar and appears nowhere in this record.**
- **Agents pillar → gateway agents.** Long-lived, multi-channel agents (Discord/Slack/email,
  memory, kanban, cron, skills) that keep working with nobody watching. The incumbent is
  **hermes** (`nousresearch/hermes-agent`, `hermes gateway run`); **openclaw**
  (`openclaw/openclaw`, `openclaw gateway`) is the sibling being added. hermes is openclaw's
  successor — same family; `hermes claw migrate` imports a `~/.openclaw` setup.

Each Agents-pillar agent has its **own native web management console**. The Agents surface
**surfaces that native console** — it does not wrap opencode's SPA and it does not "dump the agent
into a terminal." The prior record's `opencode serve` + ttyd-attach model was the wrong harness;
everything below replaces it.

---

## Contract A — the agent-type dimension

An agent instance gains a **type** chosen at creation: `type ∈ {hermes, openclaw}`. It is carried
as the pod label **`agent.enterprise-ai/type`**, mirroring how `agent.enterprise-ai/model-source`
is already carried (agents-surface Contract 4). Default is **`hermes`** (the incumbent). The type
selects the image, the run command, the native-console port, and the console/model API shape;
everything else (identity, alias, metering, integrated-vs-BYO, the frozen-Code invariant) is
type-agnostic and inherited unchanged.

`opencode` is deliberately **not** a value here — it is the Code pillar. If a future coding-style
resident is ever wanted in the Agents pillar it is a new type, not a reinterpretation of these
two.

---

## Contract B — residency: a gateway agent plus its native console (supersedes Contract 2)

An Agents-pillar pod runs **two containers sharing one PVC**, because a gateway agent and its
console are two processes over one state directory, and the state volume is ReadWriteOnce so both
must live in the same pod:

| Container | hermes | openclaw |
|---|---|---|
| **agent** (the gateway) | `hermes gateway run` | `openclaw gateway` |
| **console** (native web UI) | `hermes dashboard --host 0.0.0.0` (:9119) | *served by the gateway itself* on :18789 |
| **shared state PVC** | `/opt/data` (`HERMES_HOME`) | `~/.openclaw` (+ `~/.config/openclaw`) |

The distinction from Code is the same one Contract 2 drew, but the mechanism is the harness's own:
the gateway is resident and keeps running with nothing connected; the console **attaches** to it
(hermes: the dashboard drives the gateway over `/api/gateway/*` and the shared config; openclaw:
the Control UI is the gateway's own port). A browser disconnect is a disconnect, never a shutdown.

**openclaw is the simpler case** — its gateway serves the Control UI on one port (:18789) with a
`controlUi.basePath` and a WebSocket on the same port. **hermes splits them** — `gateway run` has
**no** web port; the dashboard is a **separate** `hermes dashboard` process on :9119. This split is
exactly why the live agents (which run `gateway run` alone) have **no console today**, and adding
the dashboard container is what gives them one.

### Config persistence — the seed must not clobber (the defect this record fixes)

State lives on the PVC (hermes: `/opt/data` — `config.yaml`, `sessions/`, `memories/`, kanban DB;
openclaw: `~/.openclaw` sqlite). **The agent manages its own settings, and they must survive a
restart.** The live hermes deployment seeds config with an init container that runs
`cp /seed/config.yaml /opt/data/config.yaml` **unconditionally on every boot** — which wipes every
setting the agent (or its dashboard) persisted. That is a defect. The rule:

> **Seed on first boot only.** The init container writes the seed config **iff the PVC has none**
> (`[ -f <config> ] || cp …`). After first boot the agent's own config is authoritative and is
> never overwritten by the platform.

Operator control over settings does **not** come from re-seeding; it comes from the native console
API (Contract D), so agent-persistence and operator-authority coexist instead of fighting over one
file.

Lifecycle (created → running → stopped → deleted) and its k8s mechanics (`replicas: 1/0`, PVC
retained on stop, PVC deleted last) are **unchanged** from agents-surface Contract 2.

---

## Contract C — the console: proxy the agent's native web UI, owner-scoped

The Agents tab's "open console" proxies the agent's **own** web console at **`/agents/<name>/`**,
owner-scoped exactly as agents-surface Contract 1 specifies (user from the authenticated portal
identity, **never** from the path; a request for an agent the caller does not own is a 404). The
console port is admitted **from the control-plane pod alone** (NetworkPolicy, no NodePort) — the
same "the proxy is the only door" posture the prior console had, now pointed at the native port
(hermes :9119, openclaw :18789) instead of opencode's :4096.

**No SPA URL-rewrite shim.** The prior console injected a shim because opencode's bundle assumed it
was served at origin root. Both gateway consoles support **native base-path mounting**, so the
prefix is handled by the app, not patched at the edge:

- **hermes dashboard:** honors `X-Forwarded-Host`/`-Proto`/`-Prefix` (`proxy_headers=True`) and a
  configured public URL (`HERMES_DASHBOARD_PUBLIC_URL` / `dashboard.public_url`). Set the public
  URL to the `/agents/<name>/` mount and the SPA resolves under it.
- **openclaw Control UI:** `gateway.controlUi.basePath = /agents/<name>` + `auth.mode:
  trusted-proxy`; WebSocket rides the same port, so the proxy must forward WS upgrades.

**Auth is delegated to the platform Keycloak — one control plane.** The native consoles are not
left on their own login:

- **hermes:** self-hosted OIDC (`HERMES_DASHBOARD_OIDC_ISSUER`, `HERMES_DASHBOARD_OIDC_CLIENT_ID`)
  pointed at the platform realm; or the control-plane's oauth2-proxy fronts it in `trusted-proxy`
  fashion. Binding non-loopback engages hermes's fail-closed auth gate automatically, so an
  exposed dashboard is never unauthenticated by accident.
- **openclaw:** `auth.mode: trusted-proxy` with the control-plane identity fronting it, plus the
  gateway token so a leaked path cannot drive it.

The opencode-specific console module (`control-plane/app/agent_console.py`, the SSE relay + entry
rewrite + shim) is **not** reused for gateway agents; it belongs to the conflated model. The
gateway console is a per-type proxy adapter.

---

## Contract D — model and settings: through the native console API, not a file clobber, not exec

The control plane changes an agent's model (**`enterpriseaiframework-840`**) and other settings
**through the agent's own console API**, over the same owner-scoped, control-plane-only path the
console uses. This needs **no `pods/exec`** (the control-plane SA does not have it and should not),
and it does **not** overwrite the agent's config file.

- **hermes:** the dashboard exposes `GET /api/model/options` (the selectable list) and
  `POST /api/model/set`; broader config via `GET/PUT /api/config`, `GET/PUT/DELETE /api/env`, and
  `POST /api/gateway/restart`. The portal model picker reads `options` and calls `set`. hermes
  writes the change into `config.yaml` through its own writer, so it **persists** and coexists with
  everything else the agent manages.
- **openclaw:** the analogous Control-UI/config path (its `openclaw.json`
  `agents.defaults.model.primary`, set through the Control UI's config channel).

The model **list** is the EAI gateway catalogue — the same set `control-plane/app/agents.py:
allowed_models()` already exposes and that the gateway serves at `/v1/models` (hermes discovers it
via `providers.gateway.discover_models: true`). The picker offers that list; the agent's provider
is the integrated `<user>::agents/<name>` virtual key at `http://gateway:4000/v1`, so inference
stays on the one bill (agents-surface Contract 3, unchanged).

**Why this is the rescue path.** The bug that motivated `-840`: an agent set itself to a braindead
model and could not set it back from inside. Because the operator path is the console API — reached
by the control plane, not by asking the agent to act — a bricked agent is always recoverable, and
because the seed no longer clobbers (Contract B), the recovery persists. The agent may still change
its own model too; last writer wins on the one key; neither party wipes the other's settings.

---

## Carried over unchanged from `agents-surface.md`

These are pillar-agnostic and are **not** re-decided here:

- **Contract 1 — identity & alias.** `(<user>, <name>)`, k8s objects `agent-<user>-<name>`,
  console path `/agents/<name>/`, metering alias `<username>::agents/<name>` (one `::`). The live
  hermes agents already use this — same key Secret `agent-<user>-<name>-key`, same
  `agent.enterprise-ai/{user,name,model-source}` labels — so metering and one-bill attribution
  already work for them.
- **Contract 3 — two metering dimensions.** Inference tokens on the virtual-key path; resident
  time + compute in the control-plane ledger, keyed on `(user, name)` pod labels. Type-agnostic.
- **Contract 4 — integrated vs BYO.** Integrated `<user>::agents/<name>` default; BYO points the
  agent's provider off-gateway, visible and never a silent $0.
- **Contract 6 — Code-untouched invariant.** `deploy/workspace/*` and the frozen set stay
  byte-identical; `tests/test_code_surface_frozen.py` enforces it. The Agents pillar is built from
  **new** files beside the frozen set — and since the Agents pillar is now hermes/openclaw, it does
  not even share the opencode image, so the separation is cleaner than before.

---

## Deployment shape (concrete, for the provisioner)

Per agent, matching the live hermes agents' naming and extending it:

- **PVC** `agent-<user>-<name>` — hermes at `/opt/data`, openclaw at `~/.openclaw`.
- **Secret** `agent-<user>-<name>-key` — `OPENAI_API_KEY` = the integrated virtual key (reused,
  already one-bill). Plus a console auth secret (OIDC client secret / dashboard basic-auth / gateway
  token) as the type requires.
- **ConfigMap** `agent-<user>-<name>-config` — the **first-boot** seed only (Contract B).
- **initContainer** `config-seed` — `[ -f <config> ] || cp /seed/<config> <config>`; never
  unconditional.
- **Deployment**, `strategy: Recreate`, `replicas: 1`, **two containers** (agent gateway + native
  console) sharing the PVC; `agent.enterprise-ai/type` + the existing labels.
- **Service** `agent-<user>-<name>` exposing the console port (hermes 9119 / openclaw 18789).
- **NetworkPolicy** admitting that port from the control-plane pod only; egress to gateway:4000,
  kube-dns, and the tenant's own messaging/tool endpoints — no NodePort.
- **New files** beside the frozen set: a `65-agent-hermes.template.yaml` / `66-agent-openclaw.template.yaml`
  (or one parametrised template), a hermes/openclaw provisioner, new portal proxy adapters. The
  opencode-based `64-agent.template.yaml` / `provision-agent.sh` / `agent_console.py` are the
  conflated path and are not extended for gateway agents.

---

## Verify against the real binaries before manifests land (ground-source gates)

Docs-grounded; the following are binary-/tag-specific and are confirmed in the build step against a
**throwaway** test pod (never the live agents):

1. **hermes v2026.8.3:** `hermes dashboard --help` — the `--host`/`--port` flags and default :9119;
   that the dashboard binds `0.0.0.0` and engages the auth gate; the exact `/api/model/*`,
   `/api/config`, `/api/gateway/*` routes and their request shapes; the OIDC env keys.
2. **hermes config:** that `config.yaml` model persistence is targeted (a `set` preserves other
   keys) and that the dashboard's `/api/model/set` reloads the gateway without a full pod restart
   (or that `POST /api/gateway/restart` is the intended reload).
3. **openclaw:** `openclaw gateway` port 18789, `controlUi.basePath`, `trusted-proxy`, and the
   config path for the model, against the real `ghcr.io/openclaw/openclaw` image.
4. **`HERMES_HOME`:** the live pod sets `HERMES_HOME=/opt/data` and it resolves there; keep using
   it rather than the docs' `~/.hermes`, which is the same directory by another name.

---

## Downstream consumers

| Item | Consumes |
|---|---|
| `-840` model picker | Contract D (native console model API), C (the proxy path), B (persistence) |
| `-268` agent-type / openclaw design | Contract A (the type dimension), B, C, D for both types |
| console build (new item) | Contract C (native console proxy, Keycloak OIDC, NetworkPolicy) |
| hermes provisioner (new item) | Deployment shape, Contract B (first-boot seed), reuses Contracts 1/3/4/6 |
| openclaw provisioner (new item) | Contract A + the openclaw column of B/C/D |
