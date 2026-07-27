# Deploying to k3s

Runs on the mainframe GPU rail's k3s cluster — CPU-only, no GPU requests, no broker lease.
See `mainframe/docs/ops/enterprise-ai-tenant.md` for the operator-facing note and the
one-command uninstall.

```bash
op signin                                   # credentials come from 1Password via direnv
direnv reload
PUBLIC_BASE_URL=https://ai.3dl.network deploy/bin/deploy.sh
```

Everything lives in the `enterprise-ai` namespace. `kubectl delete namespace enterprise-ai`
is a complete uninstall.

## Exposure

| What | How | Why |
|---|---|---|
| Chat | NodePort 30380 | Fronted by Caddy on the gateway VM, which terminates TLS |
| Gateway | NodePort 30400 | LAN/tailnet only — not published to the internet |
| Control plane | ClusterIP only | Its admin API is a shared static token (finding 11); reach it with `kubectl -n enterprise-ai port-forward svc/control-plane 8081:8000` |

## Known gaps

- **`PUBLIC_BASE_URL` must be https or nobody can log in.** The chat surface's OIDC client
  refuses plaintext discovery and validates the issuer against what it requested. The
  stack deploys and comes up healthy either way — the failure appears only at login.
  Blocked on DNS for the public name plus a Caddy site in front.
- **Storage is `local-path`**, which lives on `k3s-worker`. That node is cattle and is
  rebuilt wholesale, which destroys the ledger and audit chain with it. A tank-backed ZFS
  dataset is requested in the mainframe note; until it exists, treat cluster data as
  disposable.
- **The generated catalogue still carries the three fake-provider models**, which point at
  a service that is not deployed here. They 500 if selected. Harmless but untidy — the
  cluster should render a Forge-only catalogue.
