# funnel-watchdog (gateway VM)

**Interim mitigation for `enterpriseaiframework-b66`. Not the durable fix.**

## What breaks

Off-tailnet users (the camp) reach the portal only through Tailscale Funnel on the
gateway VM. The gateway is behind CGNAT, and a middlebox on its uplink path flushes
long-lived TCP connection state on a **fixed ~34-minute timer, regardless of traffic**.

Evidence gathered on 2026-08-11 (`enterpriseaiframework-b66`):

- Gateway `tailscaled` control + DERP connections reset like clockwork every 33–36 min
  for 6+ hours straight. A metronome, not random NAT churn.
- The reset is a **silent blackhole**: a 40-min `tcpdump` on `:443` caught **zero** RST
  packets; the dying flow just had bytes stuck in `Send-Q`. Classic NAT-state teardown.
- The flows are **not idle** — packets flow every 2–13s — yet they still die on the
  tick. So keepalives cannot prevent it; the middlebox flushes on a schedule.
- **workshop** (same LAN, same CGNAT egress IP, same MiniUPnPd router, same UPnP/PMP/PCP
  portmapping, same virtio NIC offloads, same Tailscale version) had **0** such resets in
  the same window. The fault is specific to the gateway node's role/path, not the uplink
  as a whole and not any host config that can be copied over.
- The on-prem router at `192.168.2.1` reports `MiniUPnPd/2.1` on **Debian "wheezy"**
  (EOL 2016) — a prime suspect for an aggressive conntrack flush.

Each flush drops the gateway's Funnel path; `tailscaled` usually self-heals in 1–2 min
but occasionally wedges and needs a manual `tailscale down; up`. Any long HTTP stream
(e.g. a 3-min glm-5.2 turn) that is in flight when the tick lands is killed.

## What this watchdog does

Probes the **real public Funnel path** (`https://gateway.tailcb6ef9.ts.net/portal/` via
the Tailscale ingress IPs) every 30s. After ~90s of sustained public-path outage
(3 consecutive failures) it runs `tailscale down; up`, then holds a 120s cooldown so it
does not re-bounce into tailscaled's own recovery.

This replaces the previous watchdog, which checked `tailscale status … offline` — a
signal the fast self-heal almost never trips, so it fired **0 times in 12 h** while the
portal was actually down.

### What it does NOT do

- It does not eliminate the ~34-min blips (each flush still drops in-flight connections).
- It cannot save a long stream that is mid-flight when a flush lands.
- It is a safety net for genuine wedges, not a cure.

## The durable fix (operator decision)

The gateway's CGNAT uplink cannot hold a connection past ~34 min, so **no gateway-local
change makes the public path reliable.** Options, cheapest first:

1. **Fix/replace the on-prem router** (`192.168.2.1`, MiniUPnPd on Debian wheezy).
   Reboot it as a free first test; if the ~34-min flush stops, it was the router. Raising
   its conntrack timeouts or replacing the box may fully resolve it at zero recurring cost.
2. **Move public ingress off the CGNAT path** — a Cloudflare Tunnel (or a reverse proxy
   on a public-IP host). Most resilient, but changes the public hostname, which is the
   load-bearing OIDC issuer (`gateway.tailcb6ef9.ts.net` is baked into Keycloak,
   oauth2-proxy, and pod `hostAliases`), so it requires a coordinated auth reconfiguration,
   plus a Cloudflare account + domain (spend/accounts = operator scope).

## Install / refresh

```
ssh baron@gateway 'sudo bash -s' < install.sh
# or, on the gateway, from this directory:
sudo ./install.sh
```
