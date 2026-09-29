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
control-plane write-authority posture decision), `-147` (the hermes API server, which is the
relay's **target**), and, for the operated instance only, freerouter's audio routes. See
*Dependencies*.

**Revision 2 (2026-09-29), after the EAF owner's review of PR #55.** Changed: the agent-manager
API refuses loopback-origin requests (the old text wrongly said `/agent-manager` was not routed
through oauth2-proxy); relay timeouts, disconnect cancel, a concurrency cap, header stripping,
a server-derived session key and a per-turn audit; `-147` stated as the relay's target; the
delete-scope endorsement recorded in Q2; LiveKit licence and Cloud-only traps checked against
upstream source; an OSS-profile audio route that never touches freerouter; IdP-disable sync
revokes agent-manager tokens; Raven's provider and channel settings locked in hosted mode;
EverOS embeddings through the gateway; the audit actor `agent-manager:<owner>/<raven>`; and
attack-register rows `R1`–`R6` in `design.md` §10.

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
| **Gateway** (bundled LiteLLM in the hoistable profiles; freerouter on the operated instance only) | The single inference route. It gains `/v1/audio/transcriptions` and `/v1/audio/speech` for P3. | existing, extended in P3 |
| **LiveKit server** | Apache-2.0 WebRTC SFU on k3s, the voice front end. Self-hosted; **no LiveKit Cloud feature is used** (Contract H, *LiveKit traps*). | new (P3) |
| **Voice worker** | A LiveKit Agents (Apache-2.0) worker: Silero VAD (baked into the image) → STT → LLM node (Raven, through the relay) → TTS, with STT/TTS pinned to the control plane's `/voice/v1` base. It holds no user credential. | new (P3) |
| **Local speech servers** (the OSS default for voice) | An OSI-licensed STT (Whisper via an OpenAI-compatible server) and TTS (Qwen3-TTS, Apache-2.0) serving `/v1/audio/*`, routed by the bundled gateway. | new (P3) |

### Data paths

```
                    browser (portal session via oauth2-proxy)
                      │  /agents/<raven>/  (native console, owner-scoped)      WebRTC (P3)
                      ▼                                                          │
 ┌──────────────── control-plane pod (app: control-plane) ────────────────┐      ▼
 │ portal (loopback identity)  agent console proxy (C)                    │  ┌──────────┐
 │ /agent-manager/v1/*  ◄── Bearer (F), pod IP only, loopback → 403       │  │ LiveKit  │
 │ /agent-manager/v1/agents/<n>/relay/*  ── agent relay (G)               │  │ server   │
 │ /voice/v1/*  (session token, pod IP only, P3) (H)                      │  └────┬─────┘
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

The shape rests on five rules. They are restated as contracts below.

- **Two doors into the control plane, never crossed.** A person arrives through oauth2-proxy
  (:4180, which forwards **every** path to `127.0.0.1:8000`, `40-control-plane.yaml:214`), so
  their requests are loopback-origin. `/portal/*` accepts only loopback (`require_user`,
  `portal.py:54-55`). `/agent-manager/*` and `/voice/v1/*` accept only a **non-loopback** peer
  (a pod IP on the Service port) and refuse loopback (Contract F, *Origin*). A signed-in person
  holding a stolen agent-manager token therefore gets 403 at the front door.
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
- EverOS `[llm]` **and its embedding model** are pointed at the gateway (`/v1/chat/completions`
  and `/v1/embeddings`) with the **same** Raven key, so all memory inference, including every
  embedding call, is on the Raven's line and there is no second key to issue. EverOS must not
  fall back to a bundled or hosted embedder. The embedding section's name in EverOS's config is
  **UNVERIFIED** (only `[llm]` was read); P1 finds it in the real image, and the render test
  asserts that every EverOS model endpoint in the seed is the gateway.
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
- **Providers and channels are owned by the control plane, not by Raven's WebUI.** Raven's WebUI
  can edit providers and channels, which would make it a second console for the same settings.
  In hosted mode they are **read-only in Raven**: the provider is the seeded `providers.custom`
  (changed only by `set-model`/Contract D), and channels come from the control plane's existing
  connectors (`agents.configure_connector`, `agents.py:979`, the per-agent connector Secrets).
  Mechanism, in order of preference, decided in P1 against the real image: (1) an upstream
  lock or read-only setting, if Raven has one; otherwise (2) the console proxy refuses the
  WebUI's provider- and channel-**write** RPC methods (a deny list over those namespaces,
  fail-closed on any unknown method in them) and returns a message pointing at the portal. The
  settings stay visible, so the user can see which provider is in force. The RPC method names are
  **UNVERIFIED**. A console test proves a provider write and a channel write through the proxy
  are refused and the seeded config is unchanged afterwards.

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

**Argument checks stay in `agents.py`, with no parallel path.** The router validates nothing
that `agents.*` already validates. The model on create is checked against `allowed_models()`
inside `agents.create` (`agents.py:1158`) and on set-model inside `agents.set_model`
(`agents.py:1271`); the type against `AGENT_TYPES` inside `agents.create`; names by
`object_name`. The router adds only the Raven-specific refusals in the *Extra rule* column
(no `raven` type, no self-target). A router-side copy of the model list would be a second
list to drift, and the test that removes the `agents.py` check must go red through the
agent-manager route as well as the portal route.

### Verification in the control plane

A new FastAPI dependency `require_agent_manager(request, creds) -> (owner, raven_name)`:

0. **Origin: pod IP only, loopback refused.** If `request.client.host` is `127.0.0.1` or `::1`,
   it returns **403** before looking at the bearer. This is the exact inverse of `require_user`
   (`portal.py:54-55`). The reason: oauth2-proxy runs `--upstream=http://127.0.0.1:8000`
   (`40-control-plane.yaml:214`) and forwards **every** path, not only `/portal/*`. So
   `/agent-manager/*` **is** reachable from outside through the portal NodePort (:4180/30460)
   by any signed-in user, and every such request arrives from loopback. A person who has
   exfiltrated a Raven's token and signs in as themselves would otherwise drive that Raven's
   owner's agents from a browser. With this check the portal is loopback-only and the agent
   manager is pod-IP-only, and no request satisfies both. The check is sound only because
   uvicorn runs `--no-proxy-headers` (`control-plane/Dockerfile:20`), so `request.client.host`
   is the TCP peer and cannot be set by an `X-Forwarded-For` that oauth2-proxy passes through. A
   test pins that flag (below). The same origin check (a shared helper, not a copy) guards the P3
   voice routes `/voice/v1/*`, whose bearer is the voice session token.

   *Alternative weighed: a second uvicorn listener on its own port (say :8001) for
   `/agent-manager` and `/voice`, with the Service and the `67-` egress naming only that port.*
   It separates the doors by port instead of by peer address, but it adds a listener, a Service
   port and a NetworkPolicy port, and it still needs the same "which app is mounted where"
   test. The loopback refusal is one check in the one place that already reasons about origin,
   and mirrors a rule already proven there. **Chosen: the loopback refusal.** If the control
   plane ever gains a second in-pod caller that must use the agent manager, revisit with the
   port split.
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

- **Raven deleted** → `agents.delete` (`agents.py:1479`) sets `revoked_at` in the same call that
  revokes the virtual key (beside the existing `virtual_key` revoke at `:1529`) and deletes the
  Secret.
- **Owner turns manager power off** (portal toggle) → `revoked_at` is set and the env is replaced
  with an inert sentinel. Turning it back on mints a fresh token (a new row) and patches the
  Secret, which rolls the pod through a `checksum/agent-manager` annotation. Like `checksum/api-key`,
  it restarts only when the value actually changes.
- **Operator** → `POST /admin/agent-manager/revoke {owner, raven_name}` under the existing admin
  bearer, for incident response.
- **User disabled in the IdP** → two controls, both required. (1) **The sync revokes.** The
  identity sync's disabled branch (`main.py:209-228`) today revokes only `virtual_key` rows (and
  the chat key). It gains, in that branch, **unconditionally** (not inside the `if stale:`
  block, since a user may have no active gateway key and still have a live token),
  `UPDATE agent_manager_token SET revoked_at = now() WHERE owner = $username AND revoked_at IS
  NULL`, audited when n > 0 as `db.audit("system", "agent-manager.revoke", <username>,
  reason="disabled_in_idp", count=n)`, beside the existing `key.revoke_all` row. What happens
  to a disabled user's Raven pods is outside this contract; it guarantees only that their
  manager power is dead. (2) **The verifier re-checks.** Verification also
  requires the owner principal to be enabled, with the same "principal exists and is enabled"
  check `issuance.issue` applies, so a disable that lands between syncs is still refused.
  Revocation is stored state, not only a per-request check, so re-enabling the user does **not**
  resurrect the token; the owner re-arms manager power from the portal toggle, which mints a new
  one.

### Audit

Every agent-manager call writes `db.audit(f"agent-manager:{owner}/{raven_name}", "<action>",
f"{owner}/{target}", via="agent-manager", owner=owner, raven=raven_name, …)`. The **actor** is
`agent-manager:<owner>/<raven>`, never the bare owner: the owner did not press the button, and
an audit that records them as the actor misstates who acted. The owner stays queryable through
the target prefix and the `owner` detail. The actions are the existing `agent.create`,
`agent.stop`, `agent.start`, `agent.model.set` and `agent.delete`, so one query by action shows
every lifecycle event regardless of who acted, and the actor prefix says it was an agent.
Refusals (401/403/404, including the loopback 403) are audited too as `agent-manager.denied`,
since a Raven probing names it does not own, or a person replaying a token through the portal,
is the signal worth seeing. A 401 has no resolved owner, so its actor is
`agent-manager:unknown` with the token hash prefix and peer address as detail. Issue and revoke
are audited as `agent-manager.issue` and `agent-manager.revoke`. Every relay turn is audited as
`agent.relay.turn` (Contract G).

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

**`-147` (the hermes API server on :8642) is the relay's target, not an alternative to it.**
Enabling :8642 gives hermes an OpenAI-shaped API; it does not give Raven a way to reach it.
Reaching it directly would be option (a), which fails isolation for the reason above:
kube-router NetworkPolicy cannot express "same owner", so any rule that lets a Raven pod reach
:8642 lets every Raven reach every user's hermes. The relay wins on all three counts that
matter here:

- **Isolation:** owner scoping is `_owned_deployment` on every turn, in code that
  `test_portal_agents.py`-style tests attack, rather than a network rule that cannot say it.
- **Billing:** the target answers with its own model on its own `<owner>::agents/<n>` key, so
  each agent's thinking lands on its own line. The relay spends nothing.
- **One control plane:** every turn passes the one place that authenticates, scopes, limits
  and audits it (below). There is no second path to keep in step.

`-147`'s done-condition therefore includes the reviewer's addition: a connection to :8642
**from a Raven pod is refused (measured)**, as well as from another agent pod.

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
- **Hermes:** the relay target is the hermes API server (`/v1/chat/completions`, :8642), enabled
  by `-147`. **UNVERIFIED and not enabled in EAF today:** `65-agent-hermes.template.yaml` exposes
  only :9119, and no `8642`/`API_SERVER` setting exists in this tree. `-147` confirms the enable
  switch and auth against `nousresearch/hermes-agent:v2026.8.3` (a `-2ba`-style check), then
  adds the env and a Service port to the hermes template. That is additive and rolls existing
  agents once.

**Relay stream rules.** The console proxies set no read timeout on purpose
(`agent_gateway_console.py:39`, `read=None`, because a dashboard holds event streams open for a
tab's life). The relay is a different thing, an agent driving another agent unattended, and it
must not inherit that:

- **Timeouts:** an **idle** timeout (no upstream byte for `AGENT_RELAY_IDLE_TIMEOUT`, default
  120 s) and a **total** timeout per turn (`AGENT_RELAY_TOTAL_TIMEOUT`, default 900 s), plus the
  console's connect and write timeouts. On expiry the relay closes the upstream request and ends
  the client stream with an OpenAI-shaped error event, never a silent truncation.
- **Cancel on client disconnect:** when Raven's connection drops, the relay cancels the upstream
  request at once (the streaming task watches `request.is_disconnected()`), so an abandoned turn
  does not keep the target thinking and spending.
- **Concurrency cap per token:** at most `AGENT_RELAY_MAX_STREAMS` (default 4) open relay
  streams per agent-manager token; the next one gets **429** with `Retry-After`. **This is a
  denial-of-service guard on the control plane's connections and worker slots, not a quota.** It
  limits nothing over time: no count per day, no spend cap, no agent count (decision 3 stands).
  The counter is in-process, which is correct for today's `replicas: 1`
  (`40-control-plane.yaml:51`); if the control plane is scaled out, it moves to a Postgres
  advisory-lock count.
- **Session key derived server-side:** the relay sets `X-Hermes-Session-Key` itself, as a
  stable derivation of `(owner, raven_name, target)` (for example `raven:<raven>→<target>`,
  hashed), so there is one conversation per Raven↔agent pair and Raven cannot pick or collide
  with another session.
- **Inbound headers stripped:** the relay forwards only an allow list (content type, accept,
  and the body). It drops the inbound `Authorization` (Raven's token must never reach an agent)
  and every inbound `X-Hermes-*` header, then sets the target's credential and the derived
  session key. Hop-by-hop headers go as in the console proxy's `_HOP` set.
- **Audit every turn:** one `agent.relay.turn` row per turn, actor
  `agent-manager:<owner>/<raven>`, target `<owner>/<n>`, with outcome (`ok`, `timeout_idle`,
  `timeout_total`, `client_cancel`, `upstream_error`, `capped`), duration and byte counts.
  **Never the prompt or the reply**: the audit records that a turn happened, not what it said.
  The lifecycle audit above covers the verbs; this covers the traffic.
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

Two hops, two paths, one of each per hop:

| Hop | Path | Who serves it |
|---|---|---|
| worker → control plane | **`/voice/v1/audio/transcriptions`**, **`/voice/v1/audio/speech`** | the control-plane audio relay, session-token authenticated, pod-IP only (Contract F *Origin*) |
| control plane → gateway | **`/v1/audio/transcriptions`**, **`/v1/audio/speech`** | the deployment's gateway (profile table below) |

The reviewer's `/voice/v1/audio/*` fits EAF's layout for the first hop: the control plane's own
routes are prefixed by surface (`/portal/*`, `/admin/*`, `/agents/*`, `/agent-manager/*`), and
`/v1/*` is the gateway's OpenAI-shaped namespace, which the example edge already routes by
exact path (`deploy/caddy/Caddyfile:7-12` sends `/v1/audio/speech` and
`/v1/audio/transcriptions` to speech servers on the inference edge). `/v1/audio/*` is right for
the second hop. The relay injects the **Raven's** integrated key, so voice spend lands on
`<owner>::agents/<raven>` and keys never leave the control plane or Secrets. It applies the
Contract G stream rules (timeouts, disconnect cancel, stripped `Authorization`) and audits each
call as `voice.audio` with seconds or characters, not audio.

**Plugin pinning.** The worker uses the Apache-2.0 `livekit-plugins-openai` STT and TTS, which
take a `base_url` (checked in upstream source, `livekit-plugins-openai/.../stt.py:199`,
`tts.py:91`). Both are constructed with **`base_url = http://control-plane:8000/voice/v1`**
(so the SDK appends `/audio/transcriptions` and `/audio/speech`), `api_key` = the voice session
token, and `use_realtime=False` for STT, so STT is a plain utterance POST and never the
realtime WebSocket. The base URL is config (`VOICE_AUDIO_BASE`) with that in-cluster default. A
render test asserts no other STT/TTS/LLM plugin and no other base URL is configured, and the
worker's egress policy (DNS, livekit, control-plane:8000 only) makes any other endpoint
unreachable.

**The gateway behind `/v1/audio/*`, by profile.** Voice must not depend on the operated
instance's freerouter: that instance runs on a 3DL hostname, and "no 3DL-operated service in
any data path" binds every hoistable profile.

| Profile (`hoistable-and-operated.md`) | `/v1/audio/*` served by | Notes |
|---|---|---|
| `personal`, `gateway` (the hoistable, OSS profiles; `GATEWAY_PROVIDER=litellm`, the default) | **the bundled LiteLLM**, routing the two audio model names to the **local speech servers** (Whisper-compatible STT, Qwen3-TTS/Kokoro TTS) in the tenant's cluster, or to a hosted speech provider on the **tenant's own** credential | No freerouter, no 3DL host. Air-gapped installs use the local servers only. Whether LiteLLM's spend log prices per-second STT and per-character TTS for these models is **UNVERIFIED** and is a P3 test, not an assumption. If it does not, the relay's `voice.audio` seconds/characters are the usage record and the gap is filed, never a silent $0. |
| `operated` (our instance; `GATEWAY_PROVIDER=freerouter`) | **freerouter**, which needs `/audio/speech` and `/audio/transcriptions` pass-through, a voice registry and char/second billing | freerouter's own work items. This is the only profile in which freerouter is in the voice path. |

**WebSocket** streaming STT/TTS through the gateway comes later. Until it exists, STT is
utterance-segmented (Silero VAD cuts, then one `transcriptions` call per utterance), which costs
latency but keeps every second on the bill.

### LiveKit traps (checked against upstream, 2026-09-29)

Three LiveKit features look like part of the Apache-2.0 framework and are not usable here:

| Feature | What it actually is (evidence) | Rule |
|---|---|---|
| **LiveKit Inference** (hosted STT/LLM/TTS via `livekit.agents.inference.STT/LLM/TTS` or model strings like `"deepgram/nova-3"`) | A **LiveKit Cloud** service: "LiveKit Inference is included in LiveKit Cloud" (docs.livekit.io/agents/models/). The client's default endpoint is `https://agent-gateway.livekit.cloud/v1` (`livekit-agents/livekit/agents/inference/_utils.py:18`). | **Forbidden.** A third-party hosted data path and a second bill. Never construct `inference.STT/LLM/TTS` or pass a model string to `AgentSession`. |
| **Enhanced noise cancellation** (Krisp, ai-coustics, `livekit-plugins-noise-cancellation`) | "LiveKit Cloud includes access to advanced noise cancellation models" (docs.livekit.io/home/cloud/noise-cancellation/). `livekit-plugins-krisp` is Apache-2.0 code but by default "authenticates through LiveKit Cloud"; the self-hosted path needs the proprietary `krisp-audio` SDK and a Krisp licence (plugin README). `livekit-plugins-noise-cancellation` on PyPI is licensed "SEE LICENSE IN livekit.io/legal/terms-of-service". | **Forbidden.** Cloud-only or proprietary. No noise-cancellation filter in the default pipeline. |
| **Turn-detector model** (`livekit-plugins-turn-detector`, now `livekit.agents.inference.TurnDetector`) | Weights under the **LiveKit Model License** (huggingface.co/livekit/turn-detector, LICENSE): usable "only together with the LiveKit Agents framework", no standalone use, no use of outputs to improve other models. Non-OSI. The new `TurnDetector` `v1` model runs on LiveKit Cloud (`inference/eot/transports.py`, `_CloudTransport`); only `v1-mini` runs locally. | **Forbidden as a default**, and the cloud variant is forbidden outright. End of turn is Silero VAD. |

**A fourth trap, found in checking:** from `livekit-agents` **1.6.1** onward (latest 1.8.3), the
framework has a **hard dependency** on `livekit-local-inference` (PyPI metadata), which PyPI
lists as `Apache-2.0 AND LicenseRef-LiveKit-Model` and whose wheel is a 35 MB native module
shipping a `MODEL_LICENSE` (the end-of-turn model and a VAD). `import livekit.agents` imports it
eagerly (`agents/__init__.py` → `inference/__init__.py` → `inference/vad.py`). So a current
LiveKit Agents install carries LiveKit-Model-licensed material in the image even if it is never
called. `livekit-agents` 1.5.x and 1.6.0 do not depend on it. That is a licensing call, so it is
**Q6**, not decided here.

**Silero VAD is baked into the image.** `livekit-plugins-silero` (Apache-2.0) ships
`silero_vad.onnx` as package data (its `pyproject.toml`), and Silero VAD itself is MIT
(snakers4/silero-vad). The worker image build runs `python -m livekit.agents download-files`
and a build-time check loads the VAD with networking disabled, so an air-gapped install never
fetches a model at runtime. The worker sets `HF_HUB_OFFLINE=1`. Use the plugin's
`silero.VAD.load()`, **not** `livekit.agents.inference.VAD`, which is backed by
`livekit-local-inference` (`inference/vad.py:27`).

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
| SFU / framework | LiveKit server + Agents | Apache-2.0 (checked: `livekit/livekit`, `livekit/agents` LICENSE) | passes, **but** Agents ≥1.6.1 pulls in `livekit-local-inference` (Apache-2.0 AND LiveKit Model License); see Q6 |
| VAD / end-of-turn | **Silero VAD** via `livekit-plugins-silero`, baked into the image | MIT weights, Apache-2.0 plugin (checked) | passes; the default |
| Semantic turn detector | LiveKit turn-detector model | **LiveKit Model License** (checked: framework-bound use only, no training on outputs; `v1` is Cloud-hosted) | **forbidden as a default**; the Cloud variant is forbidden outright |
| Noise cancellation | Krisp / ai-coustics / `livekit-plugins-noise-cancellation` | LiveKit Cloud or proprietary SDK (checked) | **forbidden** |
| Hosted STT/LLM/TTS | LiveKit Inference | LiveKit Cloud service (checked) | **forbidden** |
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
| **Prompt injection across agents.** A hermes agent reading Slack returns text that tells Raven "delete all agents" or "create 50 agents" | Raven ↔ children | The blast radius is **bounded to the owner's own agents** by Contract F. A Raven cannot delete itself or another Raven. Every action is audited with `via=agent-manager`. The owner can revoke in one click. **The sharpest path is injection → delete:** delete removes the PVC and is irreversible, Raven auto-approves its own tool calls, and the token sits in Raven's env, so one injected instruction can erase an agent the user built by hand. The EAF owner's review **endorses limiting delete to the Raven's own `created-by` children**; that is Q2's recommendation, and it remains Baron's call. Relay replies are **data** to Raven, and the record cannot make Raven's model treat them so. That limit is stated, not solved. |
| **Runaway creation** (no quotas, by decision 3) | Raven | No quota is added. Visibility instead: `created-by` provenance, per-agent spend and resident lines, `agent-manager.*` audit, and revoke. If this proves insufficient, a quota is a new Baron decision, not something slipped in here. |
| **Token theft.** A process in the Raven pod reads `EAF_AGENT_MANAGER_TOKEN` | Raven | By design, anything in Raven can use it. It is worth exactly the owner's agent lifecycle, no more. It cannot mint keys, read Secrets or reach other users. **Exfiltrated, it is refused at the only external door.** control-plane:8000 is ClusterIP, but the portal NodePort (:4180 → oauth2-proxy → `127.0.0.1:8000`, `40-control-plane.yaml:214`) forwards every path, `/agent-manager` included, for any signed-in user. (The previous revision said it was not routed there; that was wrong.) Contract F step 0 refuses every loopback-origin request, so a replay through the portal gets 403 and an `agent-manager.denied` audit row. Tests: the hermetic loopback-refusal test, the `--no-proxy-headers` pin, and a live replay through :4180 (see *Test plan*). |
| **Egress.** Raven inherits `0.0.0.0/0` minus private ranges | Raven | Needed for its channels (Slack/Discord/…). Accepted, as for hermes. EverMind outbound (SkillHub, update notice) is configured off. **A render test is not proof of that**, so it is also measured live, two ways: (1) in the default profile, the Raven pod boots, serves a turn and runs a memory write while CoreDNS query logging (or a capture on the node) is filtered to its pod IP, and the test asserts zero lookups of and connections to `evermind.ai` hosts; (2) in an egress-denied (air-gap) overlay, a NetworkPolicy denies the pod's internet egress, a connection attempt from the pod to `skillhub.evermind.ai:443` is shown to fail, and Raven still boots and answers, proving there is no hard dependency. Because Raven must keep general internet egress for channels, an IP-based NetworkPolicy cannot deny one public hostname in the default profile; there the proof is observational. The one-bill posture relies on the seeded provider, the same as hermes: a user who pastes an OpenRouter key into Raven's UI is BYO off-ledger, and Contract 4's visibility applies only if EAF knows. See Q5. |
| **Control-plane reachability.** Raven can now reach control-plane:8000, which also serves `/admin/*` | control plane | `/admin/*` requires `CONTROL_PLANE_ADMIN_TOKEN`, which Raven does not hold. `/portal/*` honours identity only from loopback (`portal.py` `require_user`). A test asserts a Raven-sourced request to `/admin/*` and `/portal/api/*` is refused. The inverse also holds: `/agent-manager/*` refuses loopback (Contract F step 0), so neither door opens from the other side. |
| **Relay abuse.** A looping or injected Raven opens many long relay streams, or smuggles headers to pick another session | control plane | Contract G stream rules: idle and total timeouts, cancel on disconnect, a per-token concurrent-stream cap (a DoS guard, not a quota), a server-derived `X-Hermes-Session-Key`, inbound `Authorization`/`X-Hermes-*` stripped, and one `agent.relay.turn` audit row per turn. |
| **Second console.** Raven's WebUI edits providers and channels, a settings surface beside the portal | Raven | Locked in hosted mode (Contract E): provider from the seed and `set-model`; channels from control-plane connectors; provider/channel writes refused. |
| **Upstream maturity.** Pre-alpha, ~one release every 4–5 days, security advisory request #286 unanswered | Raven | Pin the image by digest. Upgrades are deliberate, and each re-runs the hosted-mode render tests and the live smoke. |

---

## Compliance with the standing constraints (CLAUDE.md)

| Constraint | How this design complies | Residual |
|---|---|---|
| **Apache 2.0, OSI defaults, no tiered capability** | Raven Apache-2.0; LiveKit server and Agents Apache-2.0; Silero MIT; Whisper MIT; Qwen3-TTS and Kokoro Apache-2.0. LiveKit Cloud features (Inference, enhanced noise cancellation, the Cloud turn detector) are forbidden. Non-OSI items are swaps, not defaults (LiveKit turn-detector: LiveKit Model License; Kyutai weights: CC-BY-4.0). | `livekit-agents` ≥1.6.1 transitively installs LiveKit-Model-licensed `livekit-local-inference` (Q6). Raven's own dependency tree was not licence-audited, and EverMind SkillHub content is excluded (off). P1 and P3 run a licence scan of each image. |
| **No telemetry to 3DL, no 3DL-operated service in any data path** | The hoistable profiles serve `/v1/audio/*` from the bundled LiteLLM and local or tenant-credentialed speech; **freerouter is in the voice path only on the operated instance**. No EverMind service is called (SkillHub/updates off, measured live), and no LiveKit Cloud endpoint is called (the worker's egress admits only DNS, livekit and the control plane). | none known |
| **Integrate, do not reimplement** | Raven, LiveKit, Whisper and Qwen3-TTS are integrated. Built here: a provisioner branch, a token verifier, a relay, a voice worker's glue. None of that is a chat UI, agent or inference engine. | The voice worker is new glue code; it stays configuration-shaped. |
| **One control plane** | Raven's console is proxied and auth-delegated (Contract C), and LiveKit has no console. Every lifecycle action by person or Raven goes through `control-plane/app/agents.py`, audited in one table, and every Raven→agent turn goes through the relay. Voice pins and manager power are portal settings. Raven's provider and channel settings are read-only in hosted mode (Contract E). | Raven's WebUI keeps its other settings (persona, skills, cron), as hermes's dashboard does. The lock mechanism depends on what P1 finds in the image. |
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
| freerouter audio pass-through, voice registry, char/sec billing (and WS later) | The **operated instance's** voice path only | P3 on the operated instance. The hoistable profiles use the bundled LiteLLM and local speech servers and never wait on this. |
| `-147`: hermes API server (:8642), reachable only from the control plane | The relay's **target** for hermes, not an alternative path; its done-condition includes ":8642 refused from a Raven pod (measured)" | P2 |
| Q6: the `livekit-agents` version and licence ruling | Which framework release the voice worker may ship | P3 |

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
   audit row whose actor is exactly `agent-manager:<owner>/<raven>`. An unknown model on create
   and on set-model is refused **by `agents.py`** (removing the `allowed_models()` check in
   `agents.create`/`set_model` turns the agent-manager case red too, proving there is no router
   copy). Each negative case is fault-injected (remove the check and the test must go red).
1a. **Origin (the review's blocker):** a request with a **valid, live** token whose peer is
   `127.0.0.1` or `::1` (the oauth2-proxy path, simulated with the ASGI client's peer address) is
   refused **403** on every `/agent-manager/*` route and on `/voice/v1/*`, before any agent is
   touched, and writes `agent-manager.denied`. The same token from a pod-IP peer succeeds. An
   `X-Forwarded-For: 10.42.0.9` header on a loopback request is still refused. Fault injection:
   delete the loopback check and the test goes red. A companion guard pins
   `--no-proxy-headers` in `control-plane/Dockerfile`'s `CMD` (removing it must fail the test),
   because the origin check is meaningless if uvicorn rewrites the peer from forwarded headers.
2. **Issuance/revocation:** exactly one live token per Raven. The plaintext appears only in the
   Secret render, never in a response body or log. Delete revokes. The toggle rotates and rolls
   the pod only on change. **IdP disable:** running the sync with a user flipped to disabled sets
   `revoked_at` on all their live agent-manager tokens, even when they hold no active gateway key,
   writes one `agent-manager.revoke` row with `reason=disabled_in_idp`, and a replay then returns
   401. Re-enabling the user does not revive the token.
3. **Provisioner render tests** (`tests/test_agent_raven_provisioning.py`): the template renders
   with every placeholder substituted, and the type label is present in both places. The seed has
   no credential and sets `providers.custom` → the gateway. **Every EverOS model endpoint (LLM
   and embeddings) is the gateway.** SkillHub, updates, A2A inbound, Curator and Evolver are
   off. Local ACP presets are off. `RAVEN_AUTO_LOGIN=0`.
   `automountServiceAccountToken: false`. `test_agent_assets.py` covers the new template in
   `agent-assets`. **Console lock:** a provider write and a channel write sent through the
   console proxy are refused, and the seeded config is byte-unchanged afterwards.
4. **NetworkPolicy tests** (`tests/test_agent_raven_netpol.py`, in the style of
   `test_workspace_egress_allowlist.py`): `67-` selects only `type: raven` and allows only
   control-plane:8000. `66-` ingress `from` is control-plane only. Fault injection: widening to a
   namespace selector or the pod CIDR, or dropping the `type` selector, must fail.
5. **Relay:** the upstream comes from the object and never the request. The target's credential
   is injected and never returned. A non-running or non-owned target returns 404. Against a fake
   upstream: a stalled stream ends at the idle timeout and a slow-dribbling one at the total
   timeout, each with an error event; a client disconnect cancels the upstream request (the fake
   sees the cancel); the (cap+1)th concurrent stream on one token gets 429 while a second token
   is unaffected; the upstream receives **no** inbound `Authorization` or `X-Hermes-*` header and
   receives an `X-Hermes-Session-Key` equal to the server derivation for (raven, target), whatever
   Raven sent; every turn writes one `agent.relay.turn` row with the outcome and no body text.
6. **Voice (P3):** a LiveKit token minted only from the portal session. Voice session tokens are
   room-bound and expire. The audio relay injects the Raven key and attributes to
   `<owner>::agents/<raven>`. Voice-id is validated against the catalogue. **Worker config:** the
   STT and TTS plugins are the OpenAI plugins with `base_url` = the `/voice/v1` base and
   `use_realtime=False`; no `livekit.agents.inference` STT/LLM/TTS/VAD/TurnDetector, no model
   string, no Krisp/ai-coustics/noise-cancellation plugin, no turn-detector plugin appear in the
   worker (a source and dependency scan, fault-injected by adding one). **Image:** Silero VAD
   loads with networking disabled; the image's licence scan lists no LiveKit Model License
   component unless Q6 has ruled to allow it. **Profile:** the hoistable render of the audio
   route names the bundled gateway and contains no freerouter host.
7. **oss-clean, Code-frozen, and the agents design guards** stay green.

**Live (`tests-live/`, second real identity, as `test_portal_agents.py` live does):**

8. Before P2, measure whether an agent pod reaches control-plane:8000. After P2, measure that a
   Raven pod does, a hermes pod still does not, and a Raven pod cannot reach any agent pod or the
   k8s API. **A connection from a Raven pod to a hermes pod's :8642 is refused** (the `-147`
   addition), as is one from another hermes pod.
8a. **Front-door replay:** signed in as a real user through the portal NodePort (:4180), send a
   request to `/agent-manager/v1/agents` carrying a valid live token (the second identity's
   Raven's). It returns 403 and the audit shows `agent-manager.denied` with a loopback peer.
8b. **No EverMind outbound, measured:** the two-part live egress test in *Security*, *Egress*
   (DNS/connection observation in the default profile; a denied connection attempt plus a
   working Raven in the egress-denied overlay).
9. Smoke: create Raven → console loads through the proxy → Raven creates a hermes child through
   its tool → the child reaches Running with `created-by` → a direct turn through the relay gets a
   reply → spend rows appear under both `agents/<raven>` and `agents/<child>` for the owner →
   Raven stops and deletes the child → the audit shows `via=agent-manager` → deleting Raven
   revokes the token (a replay returns 401).
10. P3: a spoken round trip with a pinned voice, and audio spend (or, if LiteLLM does not
    price audio, the relay's seconds/characters) on the Raven's line, on the **hoistable
    profile** with local speech servers and no route to freerouter. The voice worker's egress
    to any host but DNS, livekit and the control plane (for example
    `agent-gateway.livekit.cloud:443`) is refused.

---

## Open questions

| # | Question | Recommendation | Absent an answer |
|---|---|---|---|
| Q1 | **LiveKit media exposure** (UDP/TURN on the edge). Externally visible, so **RESERVED to Baron.** | NodePort range on the LAN/tailnet only for dogfood, with TURN over TLS on the existing edge later | P3 stops at a LAN/tailnet-only deployment |
| Q2 | Should delete be limited to the Raven's **own `created-by` children**? Decision 2 says "the user's own agents". Narrowing it is a scope change, so it is Baron's call. **Not decided.** | Yes. It bounds the one irreversible verb against prompt injection: delete removes the PVC, Raven auto-approves, and the token is in Raven's env. **The EAF owner's review (PR #55) endorses this limit.** Stop/start/set-model stay on all owned agents (all reversible). | Implement decision 2 as written, all owned agents |
| Q3 | Is **CC-BY-4.0** (Kyutai weights) acceptable as a *default* under the OSI rule? | No. Keep it a swap. Whisper (MIT) is the default. | swap only |
| Q4 | Hosted streaming STT (Deepgram Flux) before the gateway has WebSocket: call direct from the worker (tenant credential, off-ledger, visible like BYO) or wait? | Wait. Utterance-segmented STT through the gateway keeps the bill whole. | wait |
| Q5 | Detecting a Raven user who adds an off-gateway provider in Raven's UI (BYO by the back door) | Largely closed by the hosted-mode provider lock (Contract E). What remains is a lock that P1 cannot implement against the real image; then, the hermes posture: document it, and surface "model-source: integrated" as seeded-only | documented, not enforced |
| Q6 | **`livekit-agents` ≥1.6.1 hard-depends on `livekit-local-inference`** (Apache-2.0 AND LiveKit Model License; a native wheel carrying the end-of-turn and VAD models), and `import livekit.agents` loads it. Shipping it puts non-OSI material in the default voice image even though EAF never calls it. A licensing call, so **Baron's**. | Pin the worker to the newest `livekit-agents` release without the dependency (1.6.0 at time of writing) for P3, file the dependency upstream as a request to make it optional, and re-rule when upstream moves. The alternative, accepting an uncalled non-OSI transitive dependency, is weaker against the "OSI defaults" rule. | pin to a release without it; P3 does not ship on ≥1.6.1 |

---

## Phased rollout

Each phase ends with its live smoke. **While `-e7f`/`-a24` are open, every deploy is manual**, and
the phase is not done until the live check has been observed rather than inferred.

- **P1: Raven text-only type.** Contract E: template, provisioner branch, console proxy entry,
  hosted-mode seed (EverOS LLM and embeddings on the gateway), the provider/channel lock, render
  and console-lock tests, a `-2ba`-style verification of the Raven image (bind address, image,
  non-root, state path, EverOS embedding config, the WebUI's provider/channel RPC methods).
  Raven talks to the gateway and its own in-pod built-in agents only. No token, no relay.
  **Done when** a user creates, uses, stops and deletes a Raven from the portal, with spend on
  `agents/<raven>`, and live test 8b shows no EverMind outbound.
- **P2: agent-manager token and attachment.** Contract F (table, verifier with the loopback
  refusal, router, audit with the `agent-manager:` actor, revoke including IdP-disable sync,
  portal toggle), Contract G (the `67-` egress, the `66-` ports, the relay with its stream
  rules, `-147` as the hermes target), the eaf-agents Raven tool. Hermes targets first; openclaw
  targets when `-ff7` lands. Rule `-f39` first; Q2 should be ruled before the delete verb ships.
  **Done when** the owner-scoping and origin tests (1, 1a) are green and fault-injected, the relay
  tests (5) pass, and live checks 8, 8a and 9 pass with two identities.
- **P3: voice.** Contract H: LiveKit and the voice worker (Silero baked in, OpenAI plugins
  pinned to `/voice/v1`, no LiveKit Cloud feature), the audio relay, gateway audio routes (the
  bundled LiteLLM and local speech servers for the hoistable profiles; freerouter's items for the
  operated instance only), the voice registry and pins. It needs Q1 and Q6 ruled. **Done when**
  live smoke 10 passes on the hoistable profile.

---

## Could not verify (from this read)

- The Raven WebUI's bind address and whether it can be overridden. The image name/tag, whether it
  runs as non-root, its state directory, and the extension point for the eaf-agents tool. These
  were read from source only and have not been run.
- The hermes API server (:8642): its enable switch and auth. It is not configured anywhere in EAF.
- An OpenAI-compatible HTTP endpoint on openclaw's gateway at the `-ff7` pinned version.
- Whether agents reach control-plane:8000 today without an egress rule. The manifest comment
  says no; it has not been measured.
- Whether LiteLLM's spend log prices audio routes for the chosen models.
- The carrier for voice turns into Raven (WS RPC `turn.send` vs A2A).
- EverOS's embedding configuration section, and the names of Raven's provider/channel write
  RPC methods (needed for the console lock), and whether Raven has an upstream lock setting.
- LiveKit (revision 2): checked from upstream source, PyPI metadata and docs, not by running a
  worker: that `livekit-agents` ≥1.6.1 fails to import without `livekit-local-inference` was
  inferred from its eager imports, not observed; the full LiveKit Inference docs page
  (`docs.livekit.io/agents/models/inference`) returned 404, so the Cloud-only status rests on the
  models overview and the client's default `livekit.cloud` endpoint; the ai-coustics plugin's
  own licence was not read.

## Downstream consumers

| Item (to be filed) | Consumes |
|---|---|
| Raven type (P1) | Contract E; Contracts 1/3/4/6 and A–D inherited |
| Agent-manager token (P2) | Contract F; `-f39` ruling |
| Agent relay (P2) | Contract G, including the stream rules |
| `-147` hermes API server (P2) | Contract G: the relay's target; ":8642 refused from a Raven pod" |
| openclaw as a Raven target | Contract G openclaw row; `-ff7` |
| Voice (P3) | Contract H; freerouter audio items; Q1 |
| design.md §12 body (cascade: the type list and contract summary) | Contract E's type value, Contracts F–H |
