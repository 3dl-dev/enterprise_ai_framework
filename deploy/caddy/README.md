# Gateway-VM Caddy config

`Caddyfile` here is the authoritative copy of `/etc/caddy/Caddyfile` on the **gateway VM**
(the Tailscale node `gateway`, `192.168.2.42`). Caddy on that VM terminates TLS for the
public name and fronts the k3s NodePorts — the k8s manifests in `../k8s/` describe what
runs *inside* the cluster; this file describes how the outside reaches it.

It lives in the repo because it was not, and that cost a login outage: the config was
hand-edited on the VM only, so nothing version-controlled it and nothing tested it. A VM
rebuild would have silently dropped it. Treat the VM copy as a deployment target of this
file, not as the source of truth.

## Listeners

| Block | Purpose |
|-------|---------|
| `:8080` | Local-inference edge — proxies OpenAI-shaped `/v1/*` to the `inference-*` hosts on the LAN. Co-tenant on this VM; not part of the enterprise-ai public origin. Now reached over Funnel **`:8443`, tailnet-only** (it swapped ports with the public origin and its funnel is off — nothing external depends on it). |
| `:8081` | The enterprise-ai public origin, **plain HTTP behind Tailscale Funnel on `:443`** (Funnel terminates TLS with the node cert). Moved from `:8443` to the standard `:443` because venue/school wifi routinely blocks the nonstandard port and a device off the tailnet then gets a connection *timeout* (`enterpriseaiframework-e32`). Routes `/realms/*` + `/resources/*` → identity, `/live/*` → published, `/portal/*` + `/workshop/*` → the portal, the bare landing page → the portal, and everything else → chat. |
| `https://gateway.tailcb6ef9.ts.net:443, :8443` (bound to `192.168.2.42`) | The **same routes as `:8081`, over LAN TLS**, for the in-cluster OIDC backchannel. Pods reach the issuer here via `hostAliases` because the cluster is not on the tailnet; the issuer string must be byte-identical to what the browser uses or the OIDC token is rejected. The issuer is now port-less (`:443`), so this listener serves `:443`; `:8443` is kept as a harmless second address. Browsers never hit this block — Funnel forwards them to `:8081`. |
| `:8083` | The Coder workspace, Funnel-exposed for an operator on another subnet. |

## The landing page is the portal, not raw LibreChat

Both origin blocks redirect the **bare root** `/` to `/portal/` — the signed-in wrapper
with the Chat/Code tabs, spend, keys and published work — so nobody lands on raw LibreChat
(`enterpriseaiframework-6c9` follow-up).

The redirect is guarded by `not header Sec-Fetch-Dest iframe`, and that guard is
**load-bearing, not defensive polish**. The portal embeds chat in an `<iframe>` whose `src`
is this same origin root (`control-plane/app/portal_static/app.js`). A request loading a
document *into a frame* carries `Sec-Fetch-Dest: iframe`; a top-level browser navigation
carries `document`. Without the guard, the Chat iframe — and LibreChat's own post-login
redirect back to `/` inside that frame — would load `/portal/` inside itself and the
wrapper would nest recursively. Only exact `/` is matched, so chat's `/api`, `/assets`,
`/oauth`, `/c/...` are untouched.

`tests/test_landing_page_is_portal.py` pins both properties.

## Applying a change

```sh
scp deploy/caddy/Caddyfile baron@gateway:/tmp/Caddyfile.new
ssh baron@gateway '
  sudo caddy validate --adapter caddyfile --config /tmp/Caddyfile.new &&
  sudo cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S) &&
  sudo cp /tmp/Caddyfile.new /etc/caddy/Caddyfile &&
  sudo systemctl reload caddy'
```

Then confirm the repo copy still matches the VM:

```sh
ssh baron@gateway 'cat /etc/caddy/Caddyfile' | diff - deploy/caddy/Caddyfile && echo IN SYNC
```

Caddy warns that the file "is not formatted" — that is intentional. The file is kept in the
hand-written style it was deployed in so a `diff` against the VM is meaningful; do not run
`caddy fmt` on it, which would reformat every pre-existing block and make the copies drift.
