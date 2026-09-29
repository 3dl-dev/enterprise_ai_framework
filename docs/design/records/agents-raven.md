# Design record: Raven, a per-user host agent that manages the user's own agents, with voice

**Status:** design record, proposed 2026-09-29. It **extends**
`docs/design/records/agents-gateway-console.md` and adds a third value to its Contract A type
dimension. It does not correct that record or `agents-surface.md`. Contracts **1 (alias), 3
(metering), 4 (integrated vs BYO), 6 (Code-untouched)** of `agents-surface.md` and Contracts
**A–D** of `agents-gateway-console.md` stay normative and are **inherited unchanged**. The new
contracts continue the lettering: **E** (the Raven runtime type), **F** (the owner-scoped
agent-manager token), **G** (Raven attaching to Hermes/OpenClaw), and **H** (voice).

**Operator decisions recorded here as DECIDED (Baron, 2026-09-29):**

1. **Add EverMind Raven as an EAF agent type.** It is per-user, it has voice, and it can create
   Hermes and OpenClaw instances on EAF's k3s.
2. **Raven creates and stops agents with an owner-scoped agent-manager token.** The token is a
   credential bound to one user. It can list, create, start, stop, delete and set-model only that
   user's own agents, and only through the control plane. It never touches the Kubernetes API
   directly and never reaches another user's agents. One token is issued per Raven instance, it is
   revocable, and an owner-scoping test in the shape of `control-plane/tests/test_portal_agents.py`
   proves it.
3. **No per-user resource quotas.** This was explicitly skipped. Do not add one as a side effect of
   this work (see *Security analysis*, runaway creation).

**Referenced by:** `docs/design/design.md` §12, the Agents surface, which now points here. Its
body is not otherwise changed. **Depends on:** `enterpriseaiframework-ff7`
(openclaw provisioner), `-e7f`/`-a24` (deploy pipeline and a green `make test`), `-f39` (the
control-plane write-authority posture decision), and freerouter's audio routes. See
*Dependencies*.

**Source read:** EverMind-AI/Raven at `e6c0344` (v0.2.3, Apache-2.0, pre-alpha). It was read
from source and has not been installed or run. EAF was read at `988cf18`. Raven file:line
references below come from that read (research notes, *Voice agent team switchboard /
evermind_raven.md*). EAF references were checked in this tree. Every claim not checked against a
running binary is marked **UNVERIFIED** and collected in *Could not verify*.

---

## Context and goals

The Agents pillar today is **gateway agents**: hermes (deployed) and openclaw (being added by
`-ff7`). Each is a resident pod with its own native console, proxied owner-scoped at
`/agents/<name>/`. A user creates, stops and deletes them by hand in the portal's Agents tab.

Raven is a **host agent**, which EverMind calls a "harness of harnesses". It is a Python runtime
with a WebUI (:18793) that delegates work to a roster of sub-agents. Hermes and OpenClaw can be
attached over ACP, and any OpenAI-compatible endpoint through `providers.custom` or a
`kind: openai` roster row. It adds cron/heartbeat/Raven-Oncall proactivity, twelve messaging
channels, direct one-on-one turns to a single sub-agent (`turn.send` with a `target`), and an A2A
server and client.

**Goals:**

- **G1.** A user can create a Raven from the Agents tab, the same way they create a hermes agent.
  It is resident and metered, and its native console is proxied, owner-scoped and one of the
  user's agents like any other.
- **G2.** That Raven can list, create, start, stop, delete and re-model **its owner's** Hermes and
  OpenClaw agents on EAF's k3s. It does this through the control plane and nothing else.
- **G3.** That Raven can **talk to** its owner's agents: send turns and read replies. This does
  not break the rule that agents cannot reach each other.
- **G4.** The user can **speak** to Raven, and to an agent through Raven, and hear a voice pinned
  to each agent.
- **G5.** Every token Raven spends, and every token spent by an agent Raven creates, lands on the
  **owner's** bill under Contract 1's per-agent alias. There is no platform-absorbed spend and no
  unattributed line.

**Non-goals:** per-user quotas (decision 3). Raven as a Code-pillar surface: Raven-Code is a
Raven sub-agent, not opencode, and it is not wired to ttyd. The Agents view stays console-level
control of long-lived agents (see Contract E). Multi-human voice rooms. Raven's Curator/Evolver
self-modification (disabled, see *Security*).

---

## Architecture

### Components

| Component | What it is | New or existing |
|---|---|---|
| **Raven agent unit** | `agent-<user>-<name>` Deployment, Service, PVC, `-key` Secret and `-config` ConfigMap, labelled `agent.enterprise-ai/type: raven`. It runs the Raven host runtime and WebUI (:18793). EverOS memory runs in-pod on loopback (:18791). | new type, existing shape |
| **Control plane** | Existing. It gains (a) a Raven provisioner branch, (b) the **agent-manager API** `/agent-manager/v1/*`, authenticated by the Contract F token, and (c) an **agent relay** that forwards owner-scoped turns to an agent's own API (Contract G). | existing, extended |
| **Hermes / OpenClaw units** | Existing agent units. A Raven-created one has exactly the same objects plus one provenance label, `agent.enterprise-ai/created-by`. | existing |
| **Gateway** (LiteLLM, or freerouter in the operated profile) | The single inference route. It gains `/v1/audio/transcriptions` and `/v1/audio/speech` for P3. | existing, extended in P3 |
| **LiveKit server** | Apache-2.0 WebRTC SFU on k3s, the voice front end. | new (P3) |
| **Voice worker** | A LiveKit Agents (Apache-2.0) worker: VAD → STT → LLM node (Raven, through the relay) → TTS. It holds no user credential. | new (P3) |
| **Local speech models** (optional) | An OSI-licensed STT (Whisper via an OpenAI-compatible server) and TTS (Qwen3-TTS, Apache-2.0) served behind the gateway. | new (P3, optional) |

### Data paths

```
                    browser (portal session via oauth2-proxy)
                      │  /agents/<raven>/  (native console, owner-scoped)      WebRTC (P3)
                      ▼                                                          │
 ┌──────────────── control-plane pod (app: control-plane) ────────────────┐      ▼
 │ portal (loopback identity)  agent console proxy (C)                    │  ┌──────────┐
 │ /agent-manager/v1/*  ◄── Bearer <agent-manager token> (F)              │  │ LiveKit  │
 │ /agent-manager/v1/agents/<n>/relay/*  ── agent relay (G)               │  │ server   │
 │ /voice/v1/*  (session token, P3) (H)                                   │  └────┬─────┘
 └──┬──────────────┬─────────────────────┬───────────────────────▲────────┘       │
    │ k8s API      │ :18793 console      │ :8642 / :18789 API    │ :8000          ▼
    │ (existing    │ (66-, ingress from  │ (66-, ingress from    │ (NEW egress,  ┌───────────┐
    │  Role)       │  control-plane)     │  control-plane only)  │  raven only)  │ voice     │
    ▼              ▼                     ▼                       │               │ worker    │
 Deployments   ┌────────────────┐   ┌──────────────────┐         │               └─────┬─────┘
 Secrets …     │ agent-<u>-raven│   │ agent-<u>-hermesX│         │                     │ :8000
               │ Raven + EverOS ├───┼──────────────────┼─────────┘                     │ (P3)
               │ key Secret:    │   │ key: <u>::agents/│   Raven → control plane ONLY; ◄┘
               │  OPENAI_API_KEY│   │      hermesX     │   never Raven → hermes directly
               │  EAF_AGENT_MGR_│   └────────┬─────────┘
               │  TOKEN         │            │ :4000
               └───────┬────────┘            ▼
                       │ :4000          ┌─────────┐
                       └───────────────►│ gateway │──► providers (tenant's own creds)
                     <u>::agents/raven  └─────────┘    one bill, per-agent lines
```

The shape rests on four rules. They are restated as contracts below.

- **Raven's only new route inside the cluster is the control plane.** It does not reach other
  agents and does not reach the Kubernetes API. The one added NetworkPolicy rule is Raven-pods →
  control-plane:8000.
- **Every per-user decision is made in `control-plane/app/agents.py`.** The agent-manager API
  resolves the owner from the token record and then calls the **same** functions the portal
  calls. There is no second owner check to drift.
- **Inference spend always rides a Contract 1 alias of the owner.** Raven's host model uses
  `<user>::agents/<raven>`. A child agent uses `<user>::agents/<child>`, minted by the same
  `agents.create`. Voice audio uses the Raven's alias.
- **The Agents view stays console-level.** Raven's WebUI is its native console under Contract C,
  and it is not the opencode coding UX.

---

## Contract E: the Raven runtime type (mirrors Contract A/B)

`type ∈ {hermes, openclaw, raven}`. `raven` is added to `agent_usage.AGENT_TYPES`
(`control-plane/app/agent_usage.py:120`) and carried as `agent.enterprise-ai/type: raven`.
**The default stays `hermes`.** Everything type-agnostic is inherited as is: identity, the
`agent-<user>-<name>` names, the `<user>::agents/<name>` alias, both metering dimensions,
integrated vs BYO, the Code-untouched invariant, and the lifecycle (created → running → stopped
→ deleted as `replicas` 1/0 with the PVC retained, then delete).

| | hermes | openclaw | **raven** |
|---|---|---|---|
| container | `hermes gateway run` + s6 dashboard :9119 | `openclaw gateway run` + Control UI :18789 | **`raven web`** (host runtime + WebUI) on **:18793**. EverOS on loopback :18791, same container |
| state PVC | `/opt/data` | `~/.openclaw` | **Raven's home dir** (config, sessions, roster, EverOS store); exact path **UNVERIFIED** |
| console | dashboard basic-auth from `-key` Secret | `trusted-proxy` + gateway token | **`X-Raven-Token`** from the `-key` Secret, presented by the proxy. `RAVEN_AUTO_LOGIN=0` |
| model | `providers.gateway` in seeded `config.yaml` | `providers.<id>.baseUrl` | **`providers.custom`**: `api_base = AGENT_GATEWAY_BASE` (`agents.py:86`), key = `OPENAI_API_KEY` |

**Provisioning.** There is a new template `deploy/k8s/67-agent-raven.template.yaml`, a sibling of
`65-agent-hermes.template.yaml` with the same object set: PVC 5Gi `local-path`, Service, a
Deployment with Recreate and `replicas: 1`, the `-key` Secret and a first-boot-only `-config` seed
ConfigMap. `create()` gains a `raven` branch beside `if agent_type == "hermes":`
(`agents.py:1198`) that calls a new `_provision_raven` modelled on `_provision_hermes`
(`agents.py:1054`) and a `render_raven` modelled on `render_hermes` (`agents.py:787`). The
template is added to the `agent-assets` ConfigMap in `deploy/bin/deploy.sh` (`:196`), and
`tests/test_agent_assets.py` keeps the lists in step. **`deploy/bin/provision-agent.sh` is not
used or extended for Raven.** It renders the opencode template with the workspace image, so
re-running it against a gateway agent would swap its image. Repointing a live agent is a Secret
patch plus a config write through the agent's own API (Contract D), never a re-run of the script.
Raven's reconfiguration of its children follows the same rule (Contract F `set-model`).

**The `-key` Secret for a raven agent carries:** `OPENAI_API_KEY` (the integrated
`<user>::agents/<name>` key, minted before the pod exists, as today), `RAVEN_CONSOLE_TOKEN` (the
WebUI token the proxy injects), and `EAF_AGENT_MANAGER_TOKEN` (Contract F). The seed ConfigMap
carries **no credential**, only `providers.custom` pointing at the gateway by env reference, the
hosted-mode switches below, and the roster rows for Contract G.

**Hosted-mode configuration, seeded and asserted by render tests:**

- `providers.custom` is the gateway. No OpenRouter/Anthropic default. Raven's shipped default is
  `anthropic/claude-opus-4-5` (`raven/config/schema.py:176`), which would bill a key we do not
  hold or fail.
- EverOS `[llm]` is pointed at the gateway with the **same** Raven key, so memory inference is on
  the Raven's line and there is no second key to issue.
- The built-in sub-agents (Raven-Code/-Design/-Research/-Oncall) run on the **host LLM**, not
  their `recommendedLlm` OpenRouter defaults.
- **Local third-party ACP presets are off.** The Hermes/OpenClaw/Codex/Claude Code rows would
  spawn those harnesses **inside the Raven pod** on Raven's key, which bypasses per-agent
  instances and per-agent billing. Remote agents attach only through Contract G.
- SkillHub (`https://skillhub.evermind.ai`, `raven/skill_hub/hub.py:51`) and the update notice are
  **off**. This follows the air-gap rule: a hosted Raven makes no call to EverMind services.
- The A2A inbound server stays **off** (its default, empty token). The Curator and Evolver are
  **not installed** (see *Security*).
- `RAVEN_AUTO_LOGIN=0`.

**Console (Contract C, extended).** `/agents/<raven>/` is proxied by the gateway-console adapter
with a raven entry: port 18793, and the `X-Raven-Token` header injected server-side, so the
browser never holds the token. That is the same posture as the hermes basic-auth login the proxy
performs today. The WebSocket (Raven's UI is RPC over WS) is forwarded like openclaw's. `66-agent-console-common.yaml`
gains port **18793** in its control-plane-only ingress.

> **UNVERIFIED, blocks P1:** Raven's WebUI binds `127.0.0.1` (`raven/cli/serve_commands.py:448`).
> If there is no host/bind flag or env, the pod cannot be proxied as is. The fallback is the
> smallest one that keeps "the proxy is the only door": a documented upstream flag first,
> otherwise a one-line in-pod TCP forwarder bound to the pod IP. P1's first task checks this
> against the real image, as `-2ba` did for hermes and openclaw. The image name and tag are also
> **UNVERIFIED** (Raven ships Docker Compose; whether it publishes an image is unconfirmed). The
> image is `AGENT_RAVEN_IMAGE` with an agnostic default, never a 3DL registry literal
> (hoistable invariant #1).

---

## Contract F: the owner-scoped agent-manager token

### What it is, and what it is not

It is a **control-plane credential**. It authorizes calls to `/agent-manager/v1/*` on the control
plane, **as one user, for that user's agents only**. It is **not** a gateway virtual key. It
cannot run inference, and the gateway does not know it exists.

**Contrast with `<user>::api` (PR #54, `c8c`).** That key is the precedent for *how* a
self-service credential is shaped, and the counter-example for *what* this one is:

| | `<user>::api` (PR #54) | agent-manager token (this contract) |
|---|---|---|
| Authorizes | inference at the gateway, capped and billed on its own alias | control-plane agent lifecycle calls |
| Stored by | the gateway (LiteLLM/freerouter), hash in the control-plane ledger | control-plane Postgres, **hash only** |
| Owner | never from the body; always the authenticated caller | never from the body; always the token record's `owner` |
| Minted | on demand, shown once to a person | at Raven create, **never shown to any person**, written only to the Raven's `-key` Secret |
| Auto-minted by IdP sync? | no (deliberately not in `SURFACES`) | no; only Raven creation mints one |

Reusing the gateway key machinery (`issuance.issue`) would put an authorization credential in a
spend ledger and let a gateway-side leak become an infrastructure-control leak. So the token gets
its own table and its own verifier. It copies the precedent's two rules: the owner comes from
the credential and never from a parameter, and the secret is shown once or not at all.

### Issuance

- Minted by the control plane inside `create()` for `type == raven`, **after** the owner and
  collision checks and **before** the pod exists. This is the same ordering as the virtual key,
  for the same reason: the pod never starts without a credential and then fails with nothing on
  screen to say why.
- Form: `eafam_` + 32 random bytes, base64url. It is stored as
  `agent_manager_token(token_hash sha256 PK, owner, raven_name, created_at, revoked_at,
  last_used_at)`, with a UNIQUE partial index on `(owner, raven_name) WHERE revoked_at IS NULL`,
  so there is **exactly one live token per Raven instance**.
- The plaintext is written **only** to `agent-<user>-<raven>-key` as `EAF_AGENT_MANAGER_TOKEN`
  and is never returned by any endpoint, including to the owner. The portal shows only whether
  manager power is on and when the token was last used.

### Scope, which is exactly decision 2

| Verb | Endpoint | Calls, unchanged | Extra rule |
|---|---|---|---|
| list | `GET /agent-manager/v1/agents` | `agents.list_agents(owner)` | none |
| create | `POST /agent-manager/v1/agents` `{name, type, model}` | `agents.create(owner, …)` | `type ∈ {hermes, openclaw}` only. **A Raven cannot create a Raven** (no recursion, no token minting token). Stamps `agent.enterprise-ai/created-by: <raven-name>` |
| start / stop | `POST …/agents/<n>/start` · `/stop` | `agents.scale(owner, n, 1/0)` | refuses `n == raven_name` (it cannot stop or start itself; the owner can) |
| set-model | `POST …/agents/<n>/model` | `agents.set_model(owner, n, m)` | refuses `n == raven_name` |
| delete | `DELETE …/agents/<n>` | `agents.delete(owner, n)` | refuses `n == raven_name` and refuses any `type: raven` target |
| relay | `…/agents/<n>/relay/…` | Contract G | target must be hermes/openclaw, owned by `owner` |

**Not in scope, and refused with 404/403 by construction:** connectors (they would put the tenant's
Slack/Discord/mail credentials in reach of an agent), key rotation or reprovision, BYO, any
`/admin/*` or `/portal/*` route, other users' agents, and any Kubernetes object that is not an
agent. The token record carries no verb list. Scope is **the router**: it mounts only the routes
above.

### Verification in the control plane

A new FastAPI dependency `require_agent_manager(creds) -> (owner, raven_name)`:

1. It takes the bearer, hashes it, and selects the row where `revoked_at IS NULL`. If there is no
   row, it returns **401** with a body that does not distinguish unknown from revoked.
2. It **re-checks that the Raven still exists and is owned by `owner`** through
   `_owned_deployment(owner, raven_name)` (`agents.py:457`) and that its type label is `raven`. A
   token whose Raven was deleted out from under it is dead even if revocation somehow failed.
3. It returns `owner`. The route then calls the existing `agents.*` function with `user=owner`.
   **The owner is never read from a path, body or header.** Owner-scoping is therefore
   `_owned_deployment`'s derive-then-recheck-label guard, the same one the portal relies on and
   `test_portal_agents.py` attacks. No second implementation exists to drift.
4. It updates `last_used_at` and writes the audit row (below).

It is **not cached**: every request hits the table, so revocation is immediate. The volume is one
Raven's tool calls, so this is cheap.

### Storage in the Raven pod

The token is a `secretKeyRef` env on the Raven container only, never in `envFrom` connector
Secrets, never in the seed ConfigMap, never on the PVC. Raven reads it through a small roster
tool/skill ("eaf-agents") that calls `http://control-plane:8000/agent-manager/v1/…`. The tool is
shipped in the seed as a skill or plugin; the exact Raven extension point is **UNVERIFIED**.

### Revocation

- **Raven deleted** → `agents.delete` sets `revoked_at` in the same call that revokes the virtual
  key (`agents.py:1479`, beside the existing `virtual_key` revoke) and deletes the Secret.
- **Owner turns manager power off** (portal toggle) → `revoked_at` is set and the env is replaced
  with an inert sentinel. Turning it back on mints a fresh token (a new row) and patches the
  Secret, which rolls the pod through a `checksum/agent-manager` annotation. Like `checksum/api-key`,
  it restarts only when the value actually changes.
- **Operator** → `POST /admin/agent-manager/revoke {owner, raven_name}` under the existing admin
  bearer, for incident response.
- **User disabled in the IdP** → verification also requires the owner principal to be enabled,
  with the same "principal exists and is enabled" check `issuance.issue` applies.

### Audit

Every agent-manager call writes `db.audit(owner, "<action>", f"{owner}/{target}",
via="agent-manager", raven=raven_name, …)`. The actions are the existing `agent.create`,
`agent.stop`, `agent.start`, `agent.model.set` and `agent.delete`, so one query shows every
lifecycle event regardless of who pressed the button, and `via` or `raven` says it was the agent.
Refusals (401/403/404) are audited too as `agent-manager.denied`, since a Raven probing names
it does not own is the signal worth seeing. Issue and revoke are audited as
`agent-manager.issue` and `agent-manager.revoke`.

### Billing attribution of Raven-created agents

A child created through the token goes through the unchanged `agents.create(owner, child)`. That
path mints `<owner>::agents/<child>` through `issuance.issue(owner, …, actor=owner)` before the pod
exists (Contract 1). So:

- **Inference:** the child's tokens land in the spend log under its own per-instance line,
  `agents/<child>`, for the owner. Raven's own host and EverOS tokens land under
  `agents/<raven>`. The owner sees one line per agent on `/portal/api/spend` and
  `/admin/spend`, with no query change.
- **Resident time and compute** (Contract 3b) come from the child pod's
  `agent.enterprise-ai/user` and `name` labels: the owner, per agent, usage not cost.
- **Provenance:** `agent.enterprise-ai/created-by: <raven-name>` is on the Deployment and pod
  template (not the selector). The Agents tab shows "created by <raven>", and a spend reader can
  group a Raven's children. Budget caps are whatever the owner's agent keys already carry. No new
  quota exists (decision 3).

---

## Contract G: Raven ↔ Hermes/OpenClaw attachment

### The constraint

Agents cannot reach each other, and that is deliberate. `63-agent-common.yaml` admits ingress to
agent pods **only** from `app: control-plane`. Its egress `0.0.0.0/0` rule excludes
`10.0.0.0/8` and so the pod CIDR. `66-agent-console-common.yaml` likewise admits only the
control plane. Under kube-router, the destination's ingress `from` is the whole control, so any
rule admitting Raven pods to agent pods would admit **every user's** Raven to **every user's**
agents. A NetworkPolicy cannot say "same `agent.enterprise-ai/user` as the source" without one
policy per user, and the control plane holds **no** `networkpolicies` authority
(`39-control-plane-rbac.yaml`). The `-f39` decision covers whether it ever should.

### Options weighed

| Option | Verdict |
|---|---|
| **(a) Direct pod-to-pod:** a policy admitting `type: raven` → agents on :8642/:18789 | **Rejected.** Cross-user reach at the network layer; isolation would rest on each agent's own API auth alone. |
| **(b) Per-user NetworkPolicy** created at Raven create | **Rejected.** It needs `networkpolicies` write for the control plane, which widens the one Role `-f39` is reviewing. |
| **(c) Local ACP subprocess in the Raven pod** (`hermes acp`, `openclaw acp`) | **Rejected as the attachment path.** It runs a *different*, in-pod agent on Raven's key, so the user's real instance is never touched and per-agent billing breaks. |
| **(d) Through the control plane (chosen)** | Raven → control-plane:8000 → owner-scoped relay → the agent's own API port, admitted from the control plane only. The same "the proxy is the only door" posture as Contract C. |

### The chosen path

**Network changes (exactly two, both additive):**

1. **New** `deploy/k8s/67-agent-raven-common.yaml`: an egress-only NetworkPolicy selecting
   `app.kubernetes.io/component: agent` **and** `agent.enterprise-ai/type: raven`, allowing TCP
   **8000** to `podSelector {app: control-plane}`. Hermes and openclaw pods are not selected and
   gain nothing. The control-plane pod is selected by no ingress policy, so under this CNI an
   egress ACCEPT naming it is sufficient (the rule measured for gateway:4000 in
   `60-workspace-common.yaml`). Whether agents can reach control-plane:8000 **without** this rule
   today is **UNVERIFIED**. The 63 comment asserts they cannot, and P2 measures it live before and
   after (see *Test plan*).
2. **Extend** `66-agent-console-common.yaml` ingress ports with **18793** (Raven console) and the
   agent **API** ports the relay targets: hermes **8642** (its OpenAI-compatible API server) and
   openclaw **18789** (already present). The source is still `app: control-plane` only.

The Raven egress also inherits all of `63-agent-common.yaml` (DNS, gateway:4000,
freerouter:8080, as landed in PR #50, and the internet minus private ranges). NetworkPolicies are
additive, so Raven's egress cannot be made narrower than a hermes agent's while it carries
`component: agent`. That is accepted; see *Security*, egress.

**Relay contract:** `POST /agent-manager/v1/agents/<n>/relay/v1/chat/completions`
(OpenAI-shaped, streaming allowed).

- Owner-scoped by Contract F (`_owned_deployment(owner, n)`). The target must be hermes or
  openclaw and must be running.
- The upstream is resolved from the object, as `console_target()` (`agents.py:584`) does today,
  and is never taken from the request.
- The control plane presents the **target agent's** API credential, which it reads from the
  target's `-key` Secret. Raven never holds another agent's credential.
- Inference performed by the target bills to the target's own `<owner>::agents/<n>` key, because
  the target runs its own model. The relay itself spends nothing.
- Raven sees each remote agent as a **`kind: openai` roster row**: `base_url =
  http://control-plane:8000/agent-manager/v1/agents/<n>/relay/v1`, `api_key =
  EAF_AGENT_MANAGER_TOKEN`, `model = <n>`. When Raven creates a child, the eaf-agents tool adds
  the row. Raven's `turn.send {target}` direct turns then reach the user's real instance.
- **Hermes:** the relay target is the hermes API server (`/v1/chat/completions`, :8642).
  **UNVERIFIED and not enabled in EAF today:** `65-agent-hermes.template.yaml` exposes only
  :9119, and no `8642`/`API_SERVER` setting exists in this tree. P2 must confirm the enable
  switch and auth against `nousresearch/hermes-agent:v2026.8.3` (a `-2ba`-style check), then add
  the env and a Service port to the hermes template. That is additive and rolls existing agents
  once. A stable session header (`X-Hermes-Session-Key`) keeps one conversation per Raven↔agent
  pair.
- **OpenClaw:** its gateway's OpenAI-compatible HTTP endpoint on :18789, if the pinned version
  has one. **UNVERIFIED.** The fallback is relaying openclaw's gateway WebSocket, which is
  heavier. The decision is made in P2 against the `-ff7` image.

**Reconfiguring a child** is only `set-model` through Contract F → `agents.set_model` → the
agent's own console API (Contract D). Raven never edits `/opt/data/config.yaml`, never patches
Secrets, and never re-runs a provisioner. `set_model` today returns 501 for openclaw until `-ff7`
lands its config path. Raven inherits that.

---

## Contract H: voice

### Shape

LiveKit is the voice front end. Raven's own voice is stubbed (`voice.toggle`/`voice.record`
return "not supported", `raven/rpc/methods/_stubs.py:54-55`), its STT is hard-wired to Groq
(`raven/providers/transcription.py:25`), and its TTS is one OpenRouter-shaped tool. **None of
Raven's voice code is used**, so no Raven patch is needed.

- **LiveKit server** (Apache-2.0): a Deployment `livekit` in `enterprise-ai`. Its keys
  (`LIVEKIT_API_KEY`/`SECRET`) are held by the control plane and the worker only. Media needs
  UDP (RTP) and TCP/TLS fallback reachable by browsers, plus LiveKit's embedded TURN.
  **Exposure (NodePort/hostPort range, TURN on the public edge) is externally visible and is
  RESERVED to Baron.** See *Open questions*, Q1.
- **Room and token:** the portal mints a LiveKit access token from the **portal session**
  (`require_user`): identity = user, room = `voice-<user>-<raven>`, TTL minutes. The owner is
  derived exactly as the console derives it. There is no LiveKit console; LiveKit is configured,
  not administered (one control plane).
- **Voice worker** (LiveKit Agents, Apache-2.0): one Deployment `voice-worker`, dispatched per
  room. The pipeline is VAD → STT → LLM node → TTS. The **LLM node** is Raven, reached through the
  control plane with a **per-session voice token** the control plane issues at room creation.
  The token is bound to `(owner, raven)`, is single-room and expires with the room. It is not the
  agent-manager token, and the worker holds no long-lived user credential. "Talk to Hermes X"
  becomes Raven's direct turn (`turn.send {target}`) through Contract G, with X's pinned voice.
  How the voice session is carried into Raven (Raven's WS RPC `turn.send`, or its A2A server on
  loopback reached through the control plane) is **UNVERIFIED**, and P3 decides it against the
  real runtime.
- **Egress:** a new `voice-worker` NetworkPolicy allowing only DNS, livekit and
  control-plane:8000. The worker does **not** talk to the gateway directly (next bullet).

### Audio through the one bill

- STT and TTS go through the **gateway** at `/v1/audio/transcriptions` and `/v1/audio/speech`,
  reached by the worker **via a control-plane audio relay** (`/voice/v1/audio/*`, session-token
  authenticated). The relay injects the **Raven's** integrated key, so voice spend lands on
  `<owner>::agents/<raven>` and keys never leave the control plane or Secrets.
- **Bundled LiteLLM gateway** (the OSS default profile): route the two audio endpoints to the
  configured speech models. LiteLLM exposes both routes; whether its spend log prices
  per-second STT and per-character TTS for the chosen models is **UNVERIFIED** and is a P3 test,
  not an assumption.
- **freerouter** (the operated profile): needs `/audio/speech` and `/audio/transcriptions`
  pass-through, a voice registry, and char/second billing. That is freerouter's own work item,
  tracked there. **WebSocket** streaming STT/TTS through the gateway comes later. Until it
  exists, STT is utterance-segmented (VAD cuts, then one `transcriptions` call per utterance),
  which costs latency but keeps every second on the bill.

### Per-agent voice registry

- **Pin:** an annotation `agent.enterprise-ai/voice: <voice-id>` on each agent Deployment,
  including Raven-created children and Raven itself. It lives on the object, like `model-source`,
  and is set through `POST /portal/api/agents/<n>/voice` (owner) or at child creation through
  Contract F.
- **Catalogue:** `voice-id` is validated against a deployment list (`AGENT_VOICES`, and the
  gateway's voice registry when freerouter provides one), the same way `allowed_models()` checks
  models. A body value never reaches a request verbatim.
- **Voices are designed once and registered** (Qwen3-TTS VoiceDesign), so an agent keeps its
  voice whether TTS is hosted or local.

### Model defaults, checked against the licensing rule

| Role | Default | Licence | Verdict |
|---|---|---|---|
| SFU / framework | LiveKit server + Agents | Apache-2.0 | passes |
| VAD / end-of-turn | **Silero VAD** | MIT | passes; the default |
| Semantic turn detector | LiveKit turn-detector model | **LiveKit Model License** (separate from the framework) | **not a default.** Non-OSI; a documented swap only, terms **UNVERIFIED** |
| STT, local | **Whisper (weights MIT) via an OpenAI-compatible server** | MIT | passes; the air-gap default |
| STT, local alt | Kyutai STT 1B | code permissive; **weights CC-BY-4.0** | **documented swap, not default.** CC-BY-4.0 has no user/seat/revenue trigger, but it is not an OSI-approved licence, so it fails the letter of the rule (see Q3) |
| STT, hosted | Deepgram Flux | proprietary **service** | allowed as a **provider** the tenant calls with the tenant's own credential, like any model API. Never bundled. Needs gateway WS or a direct worker call (Q4) |
| TTS | **Qwen3-TTS** (local, or DeepInfra hosted) | Apache-2.0 weights | passes; the default |
| TTS fallback | Kokoro-82M | Apache-2.0 | passes |

---

## Security analysis

The pod is the sandbox. Raven's own sandbox (boxlite) needs KVM and is off by default
(`raven/sandbox/config.py:34-38`, `backend: "none"`). DAG sub-agent steps run `exec` on the host
even when it is on (upstream #796). EAF does not rely on it. The boundary is the Kubernetes pod:

| Risk | Where | Control in this design |
|---|---|---|
| **Auto-approve.** Raven approves every ACP sub-agent permission request (`raven/acp_client/permissions.py:1-8`); presets pass `--accept-hooks`/`--yolo`/full access | Raven pod | Local third-party ACP presets are off (Contract E). Built-in sub-agents run in the Raven pod, so whatever they are approved to do, they do inside a pod with no SA token (`automountServiceAccountToken: false`), no pod-CIDR egress and no k8s API. Remote agents are reached only through the relay, which exposes chat only, and through Contract F's six verbs. |
| **Self-modifying harness.** The Curator writes and installs code into the agent's strategy seats; the Evolver rewrites the harness offline | Raven | **Disabled in hosted mode.** The image is built from the **released wheel**, which does not ship the Curator (repo-only, README:191). The Evolver is a separate tool and is not installed. A render/image test asserts both are absent. A future ask to enable them is a Baron decision, gated on a VM. |
| **Sandbox off** | Raven | The pod is the boundary. Recommended: `runAsNonRoot`, `capabilities.drop: [ALL]`, seccomp RuntimeDefault, resource limits as hermes has. Whether the Raven image runs non-root is **UNVERIFIED**; hermes needs root for s6, Raven may not. |
| **Prompt injection across agents.** A hermes agent reading Slack returns text that tells Raven "delete all agents" or "create 50 agents" | Raven ↔ children | The blast radius is **bounded to the owner's own agents** by Contract F. A Raven cannot delete itself or another Raven. Every action is audited with `via=agent-manager`. The owner can revoke in one click. Delete is the irreversible verb (it removes the PVC); see Q2 on narrowing it to `created-by` children. Relay replies are **data** to Raven, and the record cannot make Raven's model treat them so. That limit is stated, not solved. |
| **Runaway creation** (no quotas, by decision 3) | Raven | No quota is added. Visibility instead: `created-by` provenance, per-agent spend and resident lines, `agent-manager.*` audit, and revoke. If this proves insufficient, a quota is a new Baron decision, not something slipped in here. |
| **Token theft.** A process in the Raven pod reads `EAF_AGENT_MANAGER_TOKEN` | Raven | By design, anything in Raven can use it. It is worth exactly the owner's agent lifecycle, no more. It cannot mint keys, read Secrets or reach other users. Exfiltrated off-cluster it is useless, because control-plane:8000 has no external exposure (ClusterIP; the portal edge is 4180 through oauth2-proxy, and `/agent-manager` is not routed there). A test pins that the portal port does not serve `/agent-manager`. |
| **Egress.** Raven inherits `0.0.0.0/0` minus private ranges | Raven | Needed for its channels (Slack/Discord/…). Accepted, as for hermes. EverMind outbound (SkillHub, update notice) is configured off, and a render test asserts it. The one-bill posture relies on the seeded provider, the same as hermes: a user who pastes an OpenRouter key into Raven's UI is BYO off-ledger, and Contract 4's visibility applies only if EAF knows. See Q5. |
| **Control-plane reachability.** Raven can now reach control-plane:8000, which also serves `/admin/*` | control plane | `/admin/*` requires `CONTROL_PLANE_ADMIN_TOKEN`, which Raven does not hold. `/portal/*` honours identity only from loopback (`portal.py` `require_user`). A test asserts a Raven-sourced request to `/admin/*` and `/portal/api/*` is refused. |
| **Upstream maturity.** Pre-alpha, ~one release every 4–5 days, security advisory request #286 unanswered | Raven | Pin the image by digest. Upgrades are deliberate, and each re-runs the hosted-mode render tests and the live smoke. |

---

## Compliance with the standing constraints (CLAUDE.md)

| Constraint | How this design complies | Residual |
|---|---|---|
| **Apache 2.0, OSI defaults, no tiered capability** | Raven Apache-2.0; LiveKit server and Agents Apache-2.0; Silero MIT; Whisper MIT; Qwen3-TTS and Kokoro Apache-2.0. Non-OSI items are swaps, not defaults (LiveKit turn-detector: LiveKit Model License; Kyutai weights: CC-BY-4.0). | Raven's own dependency tree was not licence-audited, and EverMind SkillHub content is excluded (off). P1 runs a licence scan of the image. |
| **No telemetry to 3DL, no 3DL-operated service in any data path** | The OSS default is the bundled gateway and local or tenant-credentialed speech. freerouter is the *operated-profile* backend, not a dependency. No EverMind service is called (SkillHub/updates off). | none known |
| **Integrate, do not reimplement** | Raven, LiveKit, Whisper and Qwen3-TTS are integrated. Built here: a provisioner branch, a token verifier, a relay, a voice worker's glue. None of that is a chat UI, agent or inference engine. | The voice worker is new glue code; it stays configuration-shaped. |
| **One control plane** | Raven's console is proxied and auth-delegated (Contract C), and LiveKit has no console. Every lifecycle action by person or Raven goes through `control-plane/app/agents.py`, audited in one table. Voice pins and manager power are portal settings. | Raven's WebUI is also a settings surface (providers, channels), like hermes's dashboard today. |
| **Hoistable and operated** | `AGENT_RAVEN_IMAGE`, `AGENT_VOICES`, LiveKit URLs and keys and speech model ids are all config with agnostic defaults. No domain, IP or operator name enters a template. `tests/test_oss_clean.py` covers new templates. | LiveKit public exposure values live in the instance overlay. |
| **Verifiability under agent authorship** | The token is proven by an adversarial owner-scoping test (below), and the network change by fault-injected policy tests plus a live measurement. | none |
| **Code-untouched invariant** | Only new files plus additive edits to agents-pillar files. Nothing in the frozen set changes; `test_code_surface_frozen.py` stays green. | none |

---

## Dependencies

| Item | Why it blocks | Blocks |
|---|---|---|
| `enterpriseaiframework-ff7`: openclaw as the second agent type (**status `inbox`** at 988cf18; the brief called it in progress in another worktree) | Raven creating OpenClaw needs a real openclaw provisioner (today a non-hermes type falls to the opencode interim, `agents.py:1198-1205`) and openclaw `set_model` (today 501) | P2 for openclaw targets; hermes-only P2 can go first |
| `-e7f`: deploy watcher ships origin/main again; `-a24`: `make test` green on main (both `inbox`) | **The deploy pipeline is broken, so control-plane deploys are manual.** Every phase here ships a control-plane change, and P2 and P3 add manifests. Until `-e7f`/`-a24` close, each rollout step is a manual deploy with a named operator and a live verification. | the live-smoke step of every phase |
| `-f39`: control-plane namespace-WRITE authority posture (**decision, `waiting`**) | Contract F *uses* that authority on a user's behalf through an agent. This design adds **no** RBAC, but it widens who can drive the existing grant. `-f39` should be ruled with this in view. | P2 |
| freerouter audio pass-through, voice registry, char/sec billing (and WS later) | The operated profile's voice path | P3 on the operated instance (the OSS profile uses LiteLLM) |
| Hermes API server enable and verify (new, see Contract G) | The relay target for hermes | P2 |

---

## Test plan, every layer

**Hermetic (`make test`):**

1. **Owner-scoping proof for the token**
   (`control-plane/tests/test_agent_manager_token.py`, in the shape of `test_portal_agents.py`:
   the real `app.agents`, a real in-process fake apiserver, the ledger stubbed). Alice's Raven
   token against Bob's agent returns **404** for list, start, stop, set-model, delete and relay.
   The hyphen collision (`alice-x`/`x-bot`) is refused. The owner never comes from a body, path or
   header: a body `owner: bob` is ignored. A revoked token returns 401, as does a token whose
   Raven was deleted. Self-stop and self-delete are refused. Creating a `raven` type is refused.
   Connectors, keys, `/admin` and `/portal` are unreachable with the token. Every call writes one
   audit row with `via=agent-manager`. Each negative case is fault-injected (remove the check and
   the test must go red).
2. **Issuance/revocation:** exactly one live token per Raven. The plaintext appears only in the
   Secret render, never in a response body or log. Delete revokes. The toggle rotates and rolls
   the pod only on change.
3. **Provisioner render tests** (`tests/test_agent_raven_provisioning.py`): the template renders
   with every placeholder substituted, and the type label is present in both places. The seed has
   no credential and sets `providers.custom` → the gateway. SkillHub, updates, A2A inbound,
   Curator and Evolver are off. Local ACP presets are off. `RAVEN_AUTO_LOGIN=0`.
   `automountServiceAccountToken: false`. `test_agent_assets.py` covers the new template in
   `agent-assets`.
4. **NetworkPolicy tests** (`tests/test_agent_raven_netpol.py`, in the style of
   `test_workspace_egress_allowlist.py`): `67-` selects only `type: raven` and allows only
   control-plane:8000. `66-` ingress `from` is control-plane only. Fault injection: widening to a
   namespace selector or the pod CIDR, or dropping the `type` selector, must fail.
5. **Relay:** the upstream comes from the object and never the request. The target's credential
   is injected and never returned. A non-running or non-owned target returns 404.
6. **Voice (P3):** a LiveKit token minted only from the portal session. Voice session tokens are
   room-bound and expire. The audio relay injects the Raven key and attributes to
   `<owner>::agents/<raven>`. Voice-id is validated against the catalogue.
7. **oss-clean, Code-frozen, and the agents design guards** stay green.

**Live (`tests-live/`, second real identity, as `test_portal_agents.py` live does):**

8. Before P2, measure whether an agent pod reaches control-plane:8000. After P2, measure that a
   Raven pod does, a hermes pod still does not, and a Raven pod cannot reach any agent pod or the
   k8s API.
9. Smoke: create Raven → console loads through the proxy → Raven creates a hermes child through
   its tool → the child reaches Running with `created-by` → a direct turn through the relay gets a
   reply → spend rows appear under both `agents/<raven>` and `agents/<child>` for the owner →
   Raven stops and deletes the child → the audit shows `via=agent-manager` → deleting Raven
   revokes the token (a replay returns 401).
10. P3: a spoken round trip with a pinned voice, and audio spend on the Raven's line.

---

## Open questions

| # | Question | Recommendation | Absent an answer |
|---|---|---|---|
| Q1 | **LiveKit media exposure** (UDP/TURN on the edge). Externally visible, so **RESERVED to Baron.** | NodePort range on the LAN/tailnet only for dogfood, with TURN over TLS on the existing edge later | P3 stops at a LAN/tailnet-only deployment |
| Q2 | Should delete be limited to the Raven's **own `created-by` children**? Decision 2 says "the user's own agents". Narrowing it is a scope change, so it is Baron's call. | Yes. It bounds the one irreversible verb against prompt injection. | Implement decision 2 as written, all owned agents |
| Q3 | Is **CC-BY-4.0** (Kyutai weights) acceptable as a *default* under the OSI rule? | No. Keep it a swap. Whisper (MIT) is the default. | swap only |
| Q4 | Hosted streaming STT (Deepgram Flux) before the gateway has WebSocket: call direct from the worker (tenant credential, off-ledger, visible like BYO) or wait? | Wait. Utterance-segmented STT through the gateway keeps the bill whole. | wait |
| Q5 | Detecting a Raven user who adds an off-gateway provider in Raven's UI (BYO by the back door) | Same posture as hermes today: document it, and surface "model-source: integrated" as seeded-only | documented, not enforced |

---

## Phased rollout

Each phase ends with its live smoke. **While `-e7f`/`-a24` are open, every deploy is manual**, and
the phase is not done until the live check has been observed rather than inferred.

- **P1: Raven text-only type.** Contract E: template, provisioner branch, console proxy entry,
  hosted-mode seed, render tests, a `-2ba`-style verification of the Raven image (bind address,
  image, non-root, state path). Raven talks to the gateway and its own in-pod built-in agents
  only. No token, no relay. **Done when** a user creates, uses, stops and deletes a Raven from the
  portal, with spend on `agents/<raven>`.
- **P2: agent-manager token and attachment.** Contract F (table, verifier, router, audit, revoke,
  portal toggle), Contract G (the `67-` egress, the `66-` ports, the relay, the hermes API server
  enabled and verified), the eaf-agents Raven tool. Hermes targets first; openclaw targets when
  `-ff7` lands. Rule `-f39` first. **Done when** the owner-scoping test is green and fault-injected,
  and live smoke 8–9 passes with two identities.
- **P3: voice.** Contract H: LiveKit and the voice worker, the audio relay, gateway audio routes
  (LiteLLM for OSS; freerouter's items for the operated profile), the voice registry and pins.
  It needs Q1 ruled. **Done when** live smoke 10 passes.

---

## Could not verify (from this read)

- The Raven WebUI's bind address and whether it can be overridden. The image name/tag, whether it
  runs as non-root, its state directory, and the extension point for the eaf-agents tool. These
  were read from source only and have not been run.
- The hermes API server (:8642): its enable switch and auth. It is not configured anywhere in EAF.
- An OpenAI-compatible HTTP endpoint on openclaw's gateway at the `-ff7` pinned version.
- Whether agents reach control-plane:8000 today without an egress rule. The manifest comment
  says no; it has not been measured.
- The LiveKit turn-detector's Model License terms (known only to be separate from Apache-2.0),
  and whether LiteLLM's spend log prices audio routes for the chosen models.
- The carrier for voice turns into Raven (WS RPC `turn.send` vs A2A).

## Downstream consumers

| Item (to be filed) | Consumes |
|---|---|
| Raven type (P1) | Contract E; Contracts 1/3/4/6 and A–D inherited |
| Agent-manager token (P2) | Contract F; `-f39` ruling |
| Agent relay and hermes API server (P2) | Contract G |
| openclaw as a Raven target | Contract G openclaw row; `-ff7` |
| Voice (P3) | Contract H; freerouter audio items; Q1 |
| design.md §12 body (cascade: the type list and contract summary) | Contract E's type value, Contracts F–H |
