# Deploying to k3s

Runs on the mainframe GPU rail's k3s cluster — CPU-only, no GPU requests, no broker lease.
See `mainframe/docs/ops/enterprise-ai-tenant.md` for the operator-facing note and the
one-command uninstall.

```bash
op signin                                   # credentials come from 1Password via direnv
direnv reload
export PUBLIC_BASE_URL=https://gateway.tailcb6ef9.ts.net:8443
deploy/bin/deploy.sh
deploy/bin/post-deploy.sh                   # realm redirect URIs, bootstrap user, key sync
```

**Live at <https://gateway.tailcb6ef9.ts.net:8443>** — real Let's Encrypt cert, OIDC login
verified end to end.

Everything lives in the `enterprise-ai` namespace. `kubectl delete namespace enterprise-ai`
is a complete uninstall.

## Exposure

| What | How | Why |
|---|---|---|
| **Portal** | NodePort 30460 | `/portal/*` on the same origin. The signed-in front door: links to every surface, your spend, your keys, your published work, and the account console. oauth2-proxy sidecar on the control-plane pod authenticates first |
| Chat | NodePort 30380 | Gateway VM Caddy `:8081`, behind Tailscale Funnel on `:8443` |
| Identity | NodePort 30382 | Same origin as chat — Caddy routes `/realms/*` and `/resources/*` here. They **must** share an origin or the OIDC issuer check fails |
| Gateway | NodePort 30400 | LAN/tailnet only — not published to the internet |
| Control plane | ClusterIP only | Its **admin** API is a shared static token (finding 11) and stays unpublished; reach it with `kubectl -n enterprise-ai port-forward svc/control-plane 8081:8000`. Only `/portal/*` is routed publicly, and signing in there confers no operator capability |

## Known gaps

- **`ai.3dl.network` is not served.** Tailscale Funnel terminates TLS with the node's
  `*.ts.net` certificate and cannot present one for a custom domain, so a CNAME to the
  funnel host resolves and then fails every handshake. Using the custom name needs an
  inbound 443 port-forward to the gateway VM, or a cloud entry point proxying back over
  Tailscale. The Azure DNS zone exists and is writable when that decision is made.
- **`PUBLIC_BASE_URL` must be https or nobody can log in.** The OIDC client refuses
  plaintext discovery. The stack deploys healthy either way — the failure appears only at
  login.
- **Storage is `local-path`**, which lives on `k3s-worker`. That node is cattle and is
  rebuilt wholesale, which destroys the ledger and audit chain with it. A tank-backed ZFS
  dataset is requested in the mainframe note; until it exists, treat cluster data as
  disposable.
- **The generated catalogue still carries the three fake-provider models**, which point at
  a service that is not deployed here. They 500 if selected. Harmless but untidy — the
  cluster should render a Forge-only catalogue.

## The IDE surface — browser-terminal aider

A per-user pod running **real terminal aider** under `ttyd`, behind oauth2-proxy against
Keycloak. Not `aider --browser` (its Streamlit GUI drops most slash commands) and not an
MCP wrapper (one-shot `--message` throws away the context accumulation and the
apply/lint/test/repair loop, which is the product).

```bash
deploy/bin/kaniko-build.sh deploy/workspace 192.168.2.43:30500/enterprise-ai-workspace:$(git rev-parse --short HEAD)
deploy/bin/ensure-second-user.sh student          # a second realm user, for the isolation check
deploy/bin/provision-workspace.sh baron
deploy/bin/provision-workspace.sh student
make test-workspace                               # drives both, as a person would
```

`provision-workspace.sh` is idempotent and is the mechanism the on-click provisioning API
will call. Each run rotates that user's `<username>::ide` virtual key through the control
plane, so the pod never holds a shared key and the ledger's token hash stays correct.

| | |
|---|---|
| URL | `http://<k3s-worker>:<nodeport>` — allocated per user, stable across reprovisions |
| Auth | oauth2-proxy → Keycloak, then an allow-list containing exactly that user's email |
| Key | `<username>::ide`, minted by `POST /admin/keys/issue` at provision time |
| Model | `glm-5.2@deepinfra` by default; `--model` overrides from the gateway catalogue |
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
