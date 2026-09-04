# Deploying to k3s

Runs on the mainframe GPU rail's k3s cluster — CPU-only, no GPU requests, no broker lease.
See `mainframe/docs/ops/enterprise-ai-tenant.md` for the operator-facing note and the
one-command uninstall.

```bash
op signin                                   # credentials come from 1Password via direnv
direnv reload
export PUBLIC_BASE_URL=https://ai.example.org
deploy/bin/deploy.sh
deploy/bin/post-deploy.sh                   # realm redirect URIs, bootstrap user, key sync
```

**Live at <https://ai.example.org>** — real Let's Encrypt cert, OIDC login
verified end to end.

Everything lives in the `enterprise-ai` namespace. `kubectl delete namespace enterprise-ai`
is a complete uninstall.

## Exposure

| What | How | Why |
|---|---|---|
| **Portal** | NodePort 30460 | `/portal/*` on the same origin. The signed-in front door: links to every surface, your spend, your keys, your published work, and the account console. oauth2-proxy sidecar on the control-plane pod authenticates first |
| Workshop | *(no port)* | `/workshop/*` on the same origin, proxied by the portal to the signed-in user's own pod. It has no URL of its own — see below |
| Chat | NodePort 30380 | Gateway VM Caddy `:8081`, behind Tailscale Funnel on `:443` |
| Identity | NodePort 30382 | Same origin as chat — Caddy routes `/realms/*` and `/resources/*` here. They **must** share an origin or the OIDC issuer check fails |
| Gateway | NodePort 30400 | LAN/tailnet only — not published to the internet |
| Control plane | ClusterIP only | Its **admin** API is a shared static token (finding 11) and stays unpublished; reach it with `kubectl -n enterprise-ai port-forward svc/control-plane 8081:8000`. Only `/portal/*` is routed publicly, and signing in there confers no operator capability |

## Known gaps

- **`ai.example.org` is not served.** Tailscale Funnel terminates TLS with the node's
  `*.ts.net` certificate and cannot present one for a custom domain, so a CNAME to the
  funnel host resolves and then fails every handshake. Using the custom name needs an
  inbound 443 port-forward to the gateway VM, or a cloud entry point proxying back over
  Tailscale. The Azure DNS zone exists and is writable when that decision is made.
- **`PUBLIC_BASE_URL` must be https or nobody can log in.** The OIDC client refuses
  plaintext discovery. The stack deploys healthy either way — the failure appears only at
  login.
- **Chat's own cookies are not Secure (enterpriseaiframework-40f), because the gateway VM's
  Caddy `:8081` block — the one Tailscale Funnel forwards to — is itself plain HTTP**, so
  whatever it forwards downstream reports `http`, never `https`, no matter how the browser
  reached Funnel. LibreChat's `shouldUseSecureCookie()` marks the OIDC session cookie Secure
  whenever `DOMAIN_SERVER` is a non-localhost https origin, and express-session's own
  cookie-setting gate then silently drops that Set-Cookie header on a request it sees as
  plain HTTP — the callback then has no state to check against and login fails with
  "Unable to verify authorization request state". `SESSION_COOKIE_SECURE=false` on the
  `chat` Deployment works around it (same trade-off already accepted for the portal's
  oauth2-proxy cookie, `--cookie-secure=false` in `40-control-plane.yaml`): the browser's
  connection to the public origin really is TLS, so a non-Secure cookie set over it is
  still stored, and only defense-in-depth against a *plain-http* path to the same NodePort
  is given up. The fix that keeps that defense-in-depth is a gateway-VM change outside this
  repo and outside the cluster: Caddy's `:8081` block asserting
  `header_up X-Forwarded-Proto https` so the pod sees the true scheme.
- **Storage is `local-path`**, which lives on `k3s-worker`. That node is cattle and is
  rebuilt wholesale, which destroys the ledger and audit chain with it. A tank-backed ZFS
  dataset is requested in the mainframe note; until it exists, treat cluster data as
  disposable.
- **The bill does not charge for a cache hit** (rd `d58`). A response served from the
  gateway's own Valkey cache writes a spend row of `$0` and does not consult the budget:
  85 requests and 150,574 tokens are recorded at zero. Both headline claims are affected —
  the bill under-reports, and a user past their cap keeps being served.
- **The workshop's pods share one internal token** (rd `1b9`). Reaching another workspace
  additionally requires defeating the NetworkPolicy, which is tested, but per-pod
  credentials would be stronger.
- **The generated catalogue still carries the three fake-provider models**, which point at
  a service that is not deployed here. They 500 if selected. Harmless but untidy — the
  cluster should render a Forge-only catalogue.

## One surface, two tabs

There is one address: **`$PUBLIC_BASE_URL/portal/`**. Chat and Code are tabs on it,
remembering whichever was used last, with spend, keys, published work and the account
console behind the avatar. An operator named in `PORTAL_ADMINS` also sees everyone's usage
there — read-only, because seeing the bill should not require the admin token that can
revoke every key.

The workshop used to be a per-user NodePort on the house LAN. That made it a separate
website, unreachable from any other network, where it did not fail cleanly but hung until
the browser gave up. It is now proxied by the control plane, which already knows who you
are and routes you to your own pod — so adding a user needs no routing configuration
anywhere. The per-pod oauth2-proxy is gone; what replaced it is a NetworkPolicy admitting
7681/7682 only from the control-plane pod, plus a token the pod checks on every request.
Both are tested in `tests-live/test_workspace_isolation.py` rather than asserted, because
this CNI resolves a packet on the destination's ingress rules without consulting the
source's egress.

```bash
deploy/bin/setup-portal.sh        # registers the portal's Keycloak client, idempotent
make test-browser                 # drives both UIs in a real Chromium
make test-e2e                     # the whole journey: login, agent, run, publish, bill
```

## The IDE surface — a browser terminal running opencode

A per-user pod running a **real terminal agent** under `ttyd`. Not `aider --browser` (its
Streamlit GUI drops most slash commands) and not an MCP wrapper (one-shot `--message`
throws away the context accumulation and the apply/lint/test/repair loop, which is the
product). opencode is the default because it explores the repo itself rather than asking
which files to add; aider stays installed and is one word away.

The terminal **resumes its last session**, so a reload, a tab switch or a project switch
no longer drops the conversation. Sessions live on the PVC — they were on an emptyDir and
erased by every restart until that was found.

```bash
deploy/bin/kaniko-build.sh deploy/workspace <registry>/enterprise-ai-workspace:$(git rev-parse --short HEAD)
deploy/bin/ensure-second-user.sh student          # a realm user; each gets its own secret
deploy/bin/provision-workspace.sh alice
deploy/bin/provision-workspace.sh student
make test-workspace                               # drives both, as a person would
```

`provision-workspace.sh` is idempotent and is the mechanism the on-click provisioning API
will call. Each run rotates that user's `<username>::ide` virtual key through the control
plane, so the pod never holds a shared key and the ledger's token hash stays correct.

**The agent's house rules are configuration, not image content** (enterpriseaiframework-cbf).
`/etc/opencode/PLATFORM.md` is baked into the image and cannot be changed without a rebuild —
facts about the pod, verified against the NetworkPolicy and the live cluster, not preference.
`/etc/opencode/tenant/TENANT.md` is an operator's own standing instructions for the terminal
agent, mounted from a ConfigMap and never baked in. It is **per-deployment, not per-user**: one
`workspace-tenant-instructions` ConfigMap, shared by every workspace this namespace provisions.

```bash
deploy/bin/provision-workspace.sh alice                                   # seeds TENANT.md from deploy/workspace/AGENTS.md the first time
deploy/bin/provision-workspace.sh student --instructions ./our-house-rules.md   # replaces it, for every workspace, no image rebuild
```

A change takes effect on the next pod, and on an already-running pod without a restart. The
mount is a directory (`/etc/opencode/tenant/`), not a `subPath` mount of a single file —
`subPath` mounts do NOT receive ConfigMap updates from the kubelet's sync, only a whole-volume
mount does, so the mount shape is load-bearing for that claim (see
`deploy/bin/lib/tenant-instructions.sh` and `deploy/workspace/Dockerfile`). First run with
nothing passed seeds the ConfigMap from `deploy/workspace/AGENTS.md` unedited, so the coding
camp's current rules are what a fresh deployment gets by default.

| | |
|---|---|
| URL | none of its own — reached at `$PUBLIC_BASE_URL/portal/`, Code tab |
| Auth | the portal authenticates, then proxies you to your own pod; the pod is ClusterIP, admitted only from the control plane, and checks a token on every request |
| Key | `<username>::ide`, minted by `POST /admin/keys/issue` at provision time |
| Model | `<model>` by default; `--model` overrides from the gateway catalogue |
| Budget | 0.5 CPU / 1Gi requested, 1 CPU / 2Gi limit, 4Gi ephemeral |

### What is deliberately closed

- **ttyd binds loopback.** No Service anywhere exposes 7681. The only published port is
  oauth2-proxy's. Changing `--interface lo` removes the entire access control.
- **No service-account token, no RBAC.** `automountServiceAccountToken: false` on both
  the ServiceAccount and the pod.
- **NetworkPolicy** allows DNS, the gateway on 4000, the public TLS edge for the OIDC
  backchannel, and the public internet. Everything private is excluded — the API server,
  Postgres, the control plane, and every other workspace. Verified by trying it from
  inside the shell, not by reading this table.
- **Non-root, no privilege escalation, all capabilities dropped, RuntimeDefault seccomp.**

### Known gaps

- **Workspaces are not durable.** `local-path` lives on `k3s-worker`, which is cattle and
  is rebuilt wholesale. A workspace does not survive that. The tank-backed dataset that
  fixes it is staged behind a reboot gate.
- **The NodePort is plain HTTP**, so the session cookie is set with `--cookie-secure=false`
  and travels in clear on the LAN. Put TLS in front before this is reachable from anywhere
  untrusted, and flip the flag back.
- **`externalTrafficPolicy: Local`** means each workspace answers only on the node its pod
  runs on. That is what keeps the NetworkPolicy honest; it also means the URL changes node
  if the pod is rescheduled.
- **No idle reclaim.** Pods run until deleted:
  `kubectl -n enterprise-ai delete deploy,svc,pvc,secret -l workspace.enterprise-ai/user=<name>`.
- **No entry point from the chat surface.** Reaching a workspace means knowing its URL.

## The agent surface — one command

A **resident** agent is not a workspace with a different tab: its own process is
`opencode serve`, a headless daemon that holds a session with no console attached and keeps
holding it across every connect and disconnect. The whole value is being away from it.

Standing one up used to be three commands and a checklist — provision the agent, wire the
chat connector, then go and prove by hand that the pod is up, that the tool inside it can
see its tokens, and that inference actually reaches the gateway. `hermes-up.sh` is the one
command, and it is the *validation* that makes it worth having:

```bash
# Slack (the default) — resident agent, metered on the one bill, in your workspace
deploy/bin/hermes-up.sh alice hermes --slack-config-file ~/.secrets/hermes-slack.env

# Discord instead
deploy/bin/hermes-up.sh alice hermes --chat discord \
    --discord-config-file ~/.secrets/hermes-discord.env

# Re-run it any time. Nothing restarts, nothing rotates, the credential file is not
# needed again — re-supplying it is the only way to rotate.
deploy/bin/hermes-up.sh alice hermes
```

It **composes** `provision-agent.sh` and the chat tools the pod already carries; it
reimplements none of them. What is new is a default — integrated inference (gateway →
Forge, metered, budgeted and audited as `<user>::agents/<name>`) plus Slack — and a refusal
to print `READY` over anything it has not observed:

| Checked | Why not just trust the previous step |
|---|---|
| Deployment `Available=True`, pod `Running` | `kubectl rollout status` is satisfied by a ReplicaSet reaching its target; the pod behind it can be CrashLoopBackOff by the time anyone looks |
| `agent-<chat> config` **inside the pod** | The tool's own report. A Secret existing is not the same as the process that will post to Slack being able to see it — `envFrom` is injected at pod start and never updated |
| The connector's `.md` composed into opencode's instructions | `entrypoint.sh` deliberately falls back to the image config rather than CrashLooping every agent over a doc file, so this failure is silent: the model simply never reaches for chat |
| A **real 200** from `POST /chat/completions` in-pod | The only step that proves the agent can do its job. One call covers the egress allowlist, the minted key, the gateway, the upstream and the model name — and asserts the base is *our* gateway, because a BYO agent answers 200 too and produces no ledger row |

Anything short of all four exits non-zero with the diagnosis and the `kubectl` line to run
next. Proven in `tests/test_hermes_up.py`, which drives the real script through recording
kubectl/curl and injects each of those failures in turn.

For BYO (the user's own provider credential, no gateway ledger row, declared with
`model-source: byo`), call `deploy/bin/provision-agent.sh --byo-key-file` directly —
`hermes-up.sh` refuses a BYO environment rather than reporting an unmetered agent as
metered.

### Known gap

- **Live turnkey needs the Agents-surface deploy.** The control plane currently running on
  the cluster predates this surface, so `POST /admin/keys/issue` will not mint
  `<user>::agents/<name>` and the inference check cannot pass against it. Tracked on the
  ship checklist, `enterpriseaiframework-a39`. Until it lands, mint with a locally-run
  control-plane app (`enterpriseaiframework-ede`) or supply `AGENT_OPENAI_API_KEY`.
