# Design record — retarget the Agents surface from opencode to Hermes

**Status:** corrective record for epic `enterpriseaiframework-da7`. Supersedes the
**runtime and console** decisions in `agents-surface.md` (Contract 2, and the console half
of Contracts 1/4). Everything else in that record — identity/alias grammar (Contract 1),
the two metering dimensions (Contract 3), integrated-vs-BYO routing (Contract 4), the email
+ chat connectors (Contract 5), and the Code-untouched invariant (Contract 6) — **stands
unchanged**. This record changes *what the resident process is* and *what the console is*,
not the chassis around them.

**Why this exists.** The original record made a *hermes agent* literally a long-running
`opencode serve` process, and the console a proxy of opencode's web IDE. That conflated the
**Agents** surface with the **Code** surface. Baron's ruling (2026-08-10): "opencode is for
the coding app… don't conflate the coding UX with the agent UX. they're entirely separate
and different." An Agent is a **long-lived autonomous agent** (Hermes), and the console is a
**terminal into it** — you operate Hermes from a console, and "if chat goes south you have
to console in to fix it." opencode is not that, and never was the target.

**The runtime is Hermes Agent (NousResearch), deployed by reference to its Helm chart.**
Baron's ruling: default chart **`jyje/hermes-agent`** (Artifact Hub, versioned, `helm test`);
`ultraworkers/hermes-agent-helm-chart` is the documented swap. We do **not** run Helm in the
cluster — the control plane keeps its httpx server-side-apply path (`agents.py`), and the
chart is the *source of the manifest mechanics*, not an in-cluster dependency. The pinned
tag: image tags are date-based (`vYYYY.M.D`); `0.8.0` does not exist on Docker Hub.

---

## R1 — the resident process

| | Old (opencode) | **New (Hermes)** |
|---|---|---|
| Image | workspace image (reused) | **`nousresearch/hermes-agent:v2026.8.3`** (pin a real date tag) |
| PID model | `command:` overrides entrypoint → `opencode serve` | **keep the image entrypoint** (s6-overlay); `args: ["gateway","run"]` — the foreground, container-correct daemon (`gateway start` is the systemd variant; do not use it) |
| Data dir | `/workspace` + `XDG_DATA_HOME=/workspace/.agent-state` | **`/opt/data`**, exposed as **`HERMES_HOME=/opt/data`**; holds `config.yaml`, session `state.db`, `auth.json`, learned skills |
| securityContext | `runAsNonRoot`, `runAsUser/Group: 1000`, `fsGroup: 1000`, `drop: [ALL]` | **empty pod + container securityContext.** The s6 init MUST start as root to chown the volume, then drops to **uid/gid 10000** itself. Hardening here breaks boot. (This is the sharpest gotcha in the retarget.) |
| Inbound port | 4096 (opencode HTTP) | **none.** `gateway run` is outbound-only. The dashboard (9119) is opt-in and `--insecure` leaks keys — keep it off. |
| Probes | tcp/4096 exec | none by default (s6 supervises); optional exec `hermes gateway status` |
| Residency invariant | unchanged | **unchanged** — single writer, `replicas: 1`, `strategy: Recreate`, RWO PVC retained on `stopped`; the whole Contract 2 lifecycle (created→running→stopped→deleted via replicas 1/0 + PVC) applies verbatim |

The residency shape the original template already built (RWO PVC, replicas 1, Recreate,
scale-to-zero stop) is **exactly** what the Hermes chart uses. That part was right. Only the
container it wraps changes.

## R2 — how inference reaches our gateway (Contract 4, integrated default)

Hermes reads provider config from `$HERMES_HOME/config.yaml`. Because HERMES_HOME must be
writable (Hermes rewrites config/skills/auth at runtime), the file is **seeded by an init
container** from a per-agent ConfigMap, not mounted read-only over the volume. The
integrated block:

```yaml
# /opt/data/config.yaml  (seeded; provider id is arbitrary, "gateway" here)
providers:
  gateway:
    base_url: http://gateway:4000/v1     # our LiteLLM gateway, one route out of the building
    key_env: OPENAI_API_KEY              # Hermes sends Authorization: Bearer $OPENAI_API_KEY
    discover_models: true                # model picker populated from /v1/models
model:
  provider: gateway                      # NOT "openai" — that id aliases to openrouter upstream
  default: <a model the gateway serves>  # provision-time --model, as today
terminal:
  backend: local                         # in-cluster; the docker backend is unsupported here
```

`OPENAI_API_KEY` is the **integrated virtual key** `<user>::agents/<name>` minted through
`/admin/keys/issue` and injected via `envFrom` a Secret — the existing issuance path
(Contract 1/4), unchanged. Metering is therefore also unchanged: inference lands on the one
bill under surface `agents/<name>` (Contract 3a); resident-time + compute keeps reading the
pod labels (Contract 3b). **BYO** swaps the provider block's `base_url` to the user's own
provider and the key Secret to `agent-<user>-<name>-byo`, and drops the `model-source: byo`
label — verbatim Contract 4.

## R3 — the console is an exec-attach to `hermes --tui`, not an HTTP proxy

The operator console is **`hermes --tui`** run *inside the running pod*:

```
kubectl exec -it agent-<user>-<name>-<hash> -- hermes --tui
```

This is not a network client of the gateway process — `hermes --tui` is self-contained and
**coordinates with the resident `gateway run` only through the shared session DB on disk**
(`/opt/data/state.db`), which is why it must run in the **same container / same HERMES_HOME**.
That makes it a true *attach*: it starts a client that shares the daemon's state and leaves
the daemon untouched on disconnect — the exact "attach, start nothing" property Contract 2
demanded, now honoured by the runtime instead of faked over opencode's SPA.

**Mechanism (RULED here, mechanical detail to `-0e7`'s successor):** the control plane
drives the **Kubernetes `pods/exec` subresource** over its existing API client — it already
talks to the apiserver via httpx SA-auth; exec is a websocket upgrade
(`.../pods/<pod>/exec?command=hermes&command=--tui&stdin=true&stdout=true&tty=true`,
subprotocol `v4.channel.k8s.io`). The portal serves an **xterm terminal** for the Agents
view (the workspace-shell terminal pattern, reused) and bridges its browser websocket to
that exec stream. Owner-scoping is unchanged: the pod is resolved as `agent-<user>-<name>`
from the authenticated identity and re-checked against its owner label (Contract 1), never
from the path.

- **`agent_console.py` is rewritten**: delete the opencode-SPA HTML rewrite + `/event`/`/api`
  proxy; replace with the exec-websocket bridge. RBAC gains **`pods/exec` create** on the
  agent pods (`39-control-plane-rbac.yaml`).
- **The portal front-end** renders a terminal in the Agents view instead of embedding the
  opencode iframe.
- **NetworkPolicy** `63-agent-common.yaml`: the port-4096 ingress rule is removed (exec
  streams via the kubelet, not through the pod's Service; there is no inbound agent port).
  Egress to `gateway:4000` and DNS stays. The `OPENCODE_SERVER_PASSWORD` gate in
  `entrypoint.sh` is removed (no server port to guard; the console is authenticated by k8s
  RBAC + owner-label re-check).

## R4 — what changes, file by file (the build)

Frozen set (Contract 6) is **still frozen** — none of this touches `deploy/workspace/*` etc.

| File | Change |
|---|---|
| `deploy/k8s/64-agent.template.yaml` | image→hermes, `args:[gateway,run]`, drop `command`, HERMES_HOME=/opt/data, PVC mount /opt/data, **empty securityContext**, init-container config seed, drop port 4096 + tcp probes, keep envFrom connector secrets + key secret |
| new `deploy/agent/hermes-config.yaml.tmpl` | the seeded `config.yaml` provider/model/terminal block (integrated + BYO variants) |
| `deploy/agent/entrypoint.sh` | repurposed to the **init container** that seeds `/opt/data/config.yaml` (or retired if the init is inline); the opencode/`OPENCODE_SERVER_PASSWORD` daemon logic is removed |
| `deploy/bin/provision-agent.sh` | render the new template + config seed; `--model`, integrated/BYO, connector checksums unchanged |
| `control-plane/app/agents.py` | renderer parity with the new template (it re-renders the same bytes); `console_target()` returns the pod for exec instead of a proxy upstream |
| `control-plane/app/agent_console.py` | **rewrite** to the `pods/exec` websocket bridge (R3) |
| `control-plane/app/portal_static/*` | Agents view renders a terminal, not the opencode iframe |
| `deploy/k8s/39-control-plane-rbac.yaml` | add `pods/exec` create |
| `deploy/k8s/63-agent-common.yaml` | drop the 4096 ingress rule |
| `deploy/bin/hermes-up.sh` + `deploy/README.md` | turnkey path provisions Hermes, not opencode |
| tests (`tests/`, `tests-live/`, `control-plane/tests/`) | every opencode assertion (`opencode serve`, port 4096, SPA proxy, `OPENCODE_SERVER_PASSWORD`) retargets to Hermes (`gateway run`, exec `--tui`, config seed). This is the bulk of the diff. |

## R5 — proof (the E2E, `-ede`'s successor)

A live k3s run: provision an integrated Hermes agent; the pod reaches `Running` on
`gateway run`; `config.yaml` carries the gateway provider; an inference through the agent's
key lands on `/admin/spend` under `agents/<name>`; the console exec-attaches `hermes --tui`
and it shares the daemon's session; `stopped`→`started` resumes the same `state.db`; and the
Code/workspace surface is still byte-identical and green (Contract 6). Retarget the existing
`agent-baron-rudi` (currently on opencode) as the first live subject.

---

## Validated live on k3s — 2026-08-10 (agent-baron-rudi)

The recipe below was proven end to end against the real cluster before any template edit
(the throwaway probe pod + the retargeted `agent-baron-rudi` Deployment). Everything here is
observed, not inferred.

- **Image `nousresearch/hermes-agent:v2026.8.3`** pulls from Docker Hub on the cluster;
  Hermes v0.20.0, Python 3.13, `HERMES_HOME=/opt/data`, agent user **uid/gid 10000
  (`hermes`)**.
- **Daemon `hermes gateway run`** boots under the image's s6-overlay and stays foreground;
  with no messaging platform wired it logs "No messaging platforms enabled" and keeps
  running (valid resident PID). s6 supervises + auto-restarts it in-container.
- **securityContext MUST allow root at boot.** The s6-overlay preinit chowns `/run` and the
  PVC and *then drops to uid 10000*; under `runAsNonRoot: true` / `runAsUser: 1000` /
  `drop: [ALL]` it dies with `/run belongs to uid 0 … lacking the privileges to fix it`.
  So the pod runs with an **empty securityContext** (image default root → drops itself to
  10000). **This is a real change from the opencode template**, which hardened to non-root
  1000 + drop-ALL. Net posture: the *agent process* is still non-root (10000) and the
  NetworkPolicy still locks egress; only the brief s6 init is root. **Flagged for Baron —
  it is forced by the image, not chosen; if unacceptable the alternative is bypassing s6
  (`command: [hermes, gateway, run]`, run as 1000 + `fsGroup`, set `HOME`), not tested.**
- **Config seed via init container.** A per-agent ConfigMap (`agent-<user>-<name>-config`,
  from `deploy/agent/hermes-config.yaml.tmpl`) is copied to `/opt/data/config.yaml` by an
  init container that runs **as root** (`chown 10000:10000` needs CAP_CHOWN); the daemon
  then reads/rewrites it as 10000. (First attempt failed two ways worth recording: a
  non-root init can't chown, and `kubectl apply` silently *retained* the old opencode
  Deployment's hardened securityContext — use `kubectl replace` / a clean object.)
- **Inference through our gateway works.** Provider block above; model
  **`deepseek-v4-flash@deepinfra`** (Baron's pick), `context_length: 128000`,
  `max_tokens: 8000`. Live round-trip in the pod: `hermes chat -q` → correct answer,
  metered on the `<baron::agents/rudi>` virtual key. Two integration traps, both recorded
  in `hermes-config.yaml.tmpl`: the model id is **bare** (no `enterprise-ai/` prefix), and
  a missing `context_length`/an over-cap `max_tokens` both surface as a misleading
  "context length exceeded".
- **Console** = `kubectl exec -it deploy/agent-<user>-<name> -c agent -- hermes --tui`,
  sharing the daemon's `/opt/data/state.db`. The in-portal terminal (rewriting
  `agent_console.py` to a `pods/exec` bridge, R3) is the productization and needs a
  control-plane redeploy — deferred while the camp is live.

Reference manifests as deployed: `deploy/agent/hermes-config.yaml.tmpl` (the seed) and the
per-agent ConfigMap + Deployment shape in R4. The repo template build (`-8a9`) renders
these; the live rudi objects are hand-applied equivalents pending that build.

### Sources
NousResearch/hermes-agent; jyje/hermes-agent Helm chart (`charts/hermes-agent`, chart 1.4.0,
appVersion `v2026.8.3`); upstream CLI reference (`gateway run`, `hermes --tui`, `hermes
dashboard` :9119); Docker Hub `nousresearch/hermes-agent` date tags. Full extraction in the
retarget rd item's trail.
