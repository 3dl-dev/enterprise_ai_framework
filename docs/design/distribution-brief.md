# Brief: the framework as an agent-installable distribution

Status: brief, for adversarial review. Not a decision. 2026-07-31.

## The proposal

Ship this repo, minus one deployment's specifics, as a distribution whose **install method is
an agentic coding agent**. It carries:

- the platform: bundle, manifests, deploy scripts, control plane
- `CLAUDE.md` / `AGENTS.md` — the operating instructions an agent reads on arrival
- a package of skills that let an agent **deploy, operate and extend** the framework
- the conformance suite — the nine dogfood outcomes — as the definition of "working"

Bring your own harness and model. The possessor says *"deploy this on DigitalOcean"* and the
agent grinds until the outcomes pass, then reports what transferred.

## Why an agent, rather than a README

On 2026-07-30/31 a single day of deploying this framework to one cluster surfaced six defects.
**Every one was invisible to static validation** and discoverable only by deploying and then
probing a running system:

| Defect | Why static validation could not see it |
|---|---|
| Skills crash-loop chat (`256`) | A k8s ConfigMap volume is a symlink farm; LibreChat's loader rejects symlinks. Works in compose, fatal on k8s. |
| webfetch/rerank crash-loop (`954`) | k8s injects `<SERVICE>_PORT=tcp://…`, colliding with the var each app reads for its listen port. No such injection in compose. |
| Deploy leaves chat unusable (`0e97`) | `deploy.sh` never calls `post-deploy.sh`; the surface comes up holding a virtual key the gateway rejects. Every pod green, first prompt 401. |
| `post-deploy.sh` aborts (`18d`) | Fails on a cluster where an optional service is not deployed, silently skipping later steps. |
| Code execution advertised, absent (`c8b`) | chat carries the codeapi env but codeapi is not deployed. Invisible in a clean browser — the toggle defaults off. |
| Portal 404 (`ce2`) | Every user is linked to their published work; a user who has published nothing gets a raw nginx 404. |

A human following a README hits all six and cannot tell why. An agent holding a definition of
done — *a real user signs in and gets a completion* — catches all six.

This is also what the project already says it optimises for: *"executable conformance suites"*
is named in `CLAUDE.md` as a mechanism that converts attention-gated work into throughput-gated
work.

## The structural precedent

`~/projects/skillc` is the same artifact one scale down. Its distinguishing move is not that it
packages a skill — it is that **the recipient's agent installs it and reports how well it
transferred**. Not "install succeeded" but "here is how much of this works in your environment."

The nine outcomes are that transfer report, for a platform instead of a skill.

## Positions this brief takes

These are argued, not assumed. The review should attack them.

1. **Install is outside-in; operation is inside.** A product that validates its own installation
   cannot report that it is broken — Thompson's trusting-trust in miniature. Demonstrated: when
   chat crash-looped, the instrument that revealed it was `kubectl`, not the product. So the
   first install runs from a harness the customer already has. Once the outcomes pass, the
   product's own Coder surface may take over operating and extending it.

2. **The conformance suite is the moat, not the agent.** Anyone can point a coding agent at a
   repo. What cannot be casually reproduced is an executable definition of working, hardened
   against a real environment.

3. **Class membership is testable.** This pattern fits software whose pain is *operational
   rather than featural*, and whose outcomes are *mechanically checkable*. Zimbra, Nextcloud,
   Mattermost, Synapse, Discourse qualify. Software whose success criterion is taste does not,
   and no amount of inference changes that.

4. **Tenant extraction, not multi-tenancy.** One deployment, one tenant, tenant specifics
   parameterised into config that is gitignored or lives in a separate repo. No tenant scoping
   in the data model. Completion test: *the distribution boots from an empty example tenant
   config and passes its own suite.*

5. **The output is a transfer report, not pass/fail.** "7 of 9 pass; codeapi needs 15–20GB to
   build from source, which this droplet cannot provide" is more useful than a red X. The
   proven-inability-with-named-prerequisite verdict already exists in this repo's dispatch
   protocol.

6. **The suite is a ratchet, not a constant.** Two of today's defects exist on Kubernetes and
   not in compose. Each new target discovers its own class and grows the suite; every
   subsequent target is cheaper. That compounding is the asset.

7. **Adversarial verification is load-bearing.** "Grind until tests pass" optimises for tests
   passing. Today three items reported green and were not: one asserted on a fetch log rather
   than model-facing content, one compared config files instead of invoking the tool, one passed
   only on a freshly provisioned bundle. All three were caught by something that mutated the
   system and checked the assertion actually bit.

8. **Long grinds need durable progress.** `e6f` took three dispatches and lost all work twice
   before its sweep was made append-per-call and resumable. Any agent loop against a real
   environment needs that checkpoint shape.

## Economics

The claim under test: *flexibility becomes a function of API fees* rather than of vendor roadmap,
professional services, or in-house headcount.

Correction today's measurements suggest: most of the fee is **verification, not authoring**. The
waves that landed spent the bulk of their tokens on adversaries re-running work and mutating
configs. So the honest form is: flexibility is a function of API fees *where a cheap oracle
exists*. Without the suite, the fee buys drift.

## Open questions for the review

1. **The authority model.** A devops agent with cluster admin is exactly what a safety
   classifier blocked today until the operator confirmed personally. Ship that to strangers and
   the confirmation problem has nobody to confirm. What may it do unattended; what must it
   surface? This is the hardest question here and the least designed.
2. **How opinionated?** The defaults are rulings with losing arguments recorded — Valkey not
   Redis, Perses not Grafana, LiteLLM MIT core only. Is the distribution's value the opinions,
   the swap ports, or both? "Unopinionated with sane defaults" may undersell it.
3. **Where does tenant config live** — gitignored directory, separate repo, or a generated
   scaffold? What is the migration story when the platform's expectations change?
4. **Which targets, in what order?** Bundle, k3s, DigitalOcean, bare VM? What is the minimum set
   that proves portability rather than asserting it?
5. **Does the devops agent ship as skills, an agent spec, or both** — and how does that survive
   BYO harness, given LibreChat validates `SKILL.md` frontmatter strictly while opencode strips
   unknown keys?
6. **What does a first install actually cost**, in tokens and wall clock? The claim is priced in
   API fees; nobody has measured one.
7. **Does a weak model install it?** That is the honest test of "sane defaults" — and the one
   most likely to fail.
8. **What is out of scope**, so this does not become a second product competing with v0.1-dogfood
   for the same attention.
