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
| Chat | NodePort 30380 | Gateway VM Caddy `:8081`, behind Tailscale Funnel on `:8443` |
| Identity | NodePort 30382 | Same origin as chat — Caddy routes `/realms/*` and `/resources/*` here. They **must** share an origin or the OIDC issuer check fails |
| Gateway | NodePort 30400 | LAN/tailnet only — not published to the internet |
| Control plane | ClusterIP only | Its admin API is a shared static token (finding 11); reach it with `kubectl -n enterprise-ai port-forward svc/control-plane 8081:8000` |

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
