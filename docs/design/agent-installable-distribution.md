# Design: the framework as an agent-installable distribution

Status: reviewed, not approved. Adversarial design 2026-07-31.
Record item: `enterpriseaiframework-98c`. Brief: `docs/design/distribution-brief.md`.

Four dispositions (adversary, creative, systems pragmatist, DAP purist) reviewed the brief's
eight positions across two rounds. This is the synthesis. It supersedes the brief where they
disagree.

---

## What survived, what changed

| Brief position | Outcome |
|---|---|
| 1. Install outside-in, operation inside | **Replaced.** Right answer, wrong argument, and it created the design's worst hole — see §1. |
| 2. The conformance suite is the moat | **Withdrawn.** Say *headstart under test*. `design.md` §9.2 already pre-registers C0 "the moat is closing" as a claim to be falsified. A competitor reproduces the suite-generating process in a weekend; Apache-2.0 with no CLA makes a code moat structurally impossible, which is the point. |
| 3. Class membership is testable | **Unproven.** No negative case was ever tested. `design.md` §11 gap 7 concedes a taste-adjacent criterion inside this very project, so the operational/taste line is asserted, not shown. |
| 4. Tenant extraction, not multi-tenancy | **Half true.** Regenerable artifacts extract cleanly. ~9 locality points are hardcoded in checked-in YAML with no templating at all. See §3. |
| 5. Transfer report, not pass/fail | **Kept, incomplete.** "7 of 9" has no denominator that varies — comparable to nothing until instrumented. See §2. |
| 6. The suite is a ratchet | **Kept, and now has a mechanism** it previously lacked. See §2. |
| 7. Adversarial verification is load-bearing | **Kept, strongest item in the brief.** It is DAP §3.1+§3.2 rediscovered independently; it cost three false greens to learn. |
| 8. Long grinds need durable progress | **Kept.** |

---

## 1. Authority: the front door is solved, the back door is not

**The mechanism (creative, from `design.md` §8.3e's eval-gate supremacy — the gate runs in a
separate process with separate credentials and the promoting actor has no write path to it):**

- The grinding agent **never holds an apply-credential to real infrastructure.** It gets
  read-target-state plus write-to-a-disposable-clone (ephemeral namespace or throwaway cluster
  the bootstrap provisions and destroys).
- The suite runs against the clone. Green produces a **diff**, not a live mutation.
- Promotion is a second, **non-LLM** gate: policy-as-code against a deny-list — no ingress
  widened to `0.0.0.0/0`, no NetworkPolicy loosened or deleted, no IAM or Keycloak admin-scope
  change, diff under a declared size threshold.
- Enforced by the **possessor's own cloud IAM and admission controller**, provisioned before the
  agent holds any key beyond read+plan.

Why this is the right shape: it moves the backstop out of the harness — which BYO cannot
guarantee — into infrastructure the possessor's own account enforces. It is DAP §3.3 **L4**
("the agent has no mechanism to bypass code it does not execute"), the top of the enforcement
hierarchy. And it caps a compromised skills package by the same mechanism rather than a new one:
*don't make the seed trustworthy, make the ceiling on a compromised seed small.*

**Two conceptual corrections it forced:**

- *Revertible ≠ safe.* Opening Keycloak to `0.0.0.0/0` for ten minutes while probing is
  perfectly revertible; the breach happened in the window. Revertibility bounds the end state,
  not the blast radius during the action. "Unattended iff reversible" fails on this alone.
- *Location was a proxy.* Position 1 gated by where the agent runs. What actually matters is what
  credential it holds and whether a non-LLM gate stands between diff and apply.

**THE HOLE — the single blocking finding of the review.** The mandate is *deploy, **operate and
extend***. This design protects **install only**. Position 1 handed operation to the product's own
Coder surface once the outcomes pass, with no gate — and that is exactly where the irreversible
action classes live. The adversary: *"a well-built gate on the front door of a house whose back
door — the part of the mandate actually named in the brief — is exactly as open as it was."*

**Required before build:** a non-LLM policy gate, or a non-optional human confirm, covering
**operate and extend** for the irreversible classes, not just bootstrap.

**Residual trust, named honestly.** The possessor still grants a read credential and runs a
bootstrap script that provisions the IAM policy, on first contact, from the same untrusted
package. Trusting-trust is **relocated, not dissolved** — and that relocation *is* the win,
because it is skillc's move: shrink what must be trusted to one readable file.

> **G1′** — the IAM bootstrap script is the trusted-by-reading kernel. Short, dependency-free,
> no network fetch, readable in one sitting, separately reviewable. Everything after runs under
> the ceiling it establishes. G1 was a chapter; G1′ is a paragraph.

---

## 2. The suite: a circuit breaker with a working slow loop and no fast loop

The purist's corrected headline. Not "a governance layer wearing a metabolism's name" — creative
built the slow half.

**Slow loop — CLOSED.** The ratchet's return channel is consent-based PRs: the transfer report
writes to a **local file only**, nothing leaves automatically; the agent then asks once whether to
publish the new behaviours as suite rows; the PR carries the executable check, not raw logs, and
goes through the existing CODEOWNERS gate. This is not a degraded loop — DAP §1.6 *requires* a
human promotion gate ("the PM decides what to promote… you are editing, not authoring"), and
§1.7's slow loop is explicitly calendar-cadence. Resolves the collision with the irreversible
no-telemetry constraint that the brief never noticed.

**Fast loop — OPEN. This is the highest-leverage unclosed item and the cheapest.**

> **G3 — instrumentation.** Three of DAP §1.3's signals, per outcome, plus the configuration
> surface §1.7 requires:
> - **S1 token count per outcome** — the denominator that makes "7 of 9" comparable to anything.
>   Answers "what does a first install cost" by construction, and yields §6.5 input-optimization
>   yield directly.
> - **S2 retry count per outcome** — attempts before green. The best environment-class detector in
>   the design: an outcome taking 1 attempt on compose and 6 on k3s *is* the class, quantified.
>   Also the missing §4.4 escalation trigger, and the recurrence counter.
> - **S3 error recovery cost** — tokens between first failure and green. High recovery flags the
>   muddled partial success §1.8.2 warns about. This is the instrument that would have caught
>   this project's three false greens automatically instead of by adversary dispatch.
> - **Configuration surface** — outcomes individually skippable and parameterizable *without
>   editing the suite*. If adapting requires editing, the possessor forks, **and the ratchet dies
>   at the fork** — every consented PR is then against a suite the filer has already diverged from.
>
> G3 is load-bearing for five other gaps (C1, C5, G5, G7, G8, plus the Dispatcher role). It is
> counters in the grind loop and a JSON row per outcome. Not a research program.

---

## 3. What the code actually says

- **Forge is in the data path today.** `dogfood-scope.md:120`, `deploy/k8s/30-gateway.yaml:59-60`,
  and 148 `api_base` refs to `forge.3dl.dev` in the live catalogue. `CLAUDE.md` line 60 forbids a
  3DL-operated service in any data path. Engineering cost to parameterize: **~150–200 LOC** —
  the control plane has **zero** Forge coupling, the portable suite is unaffected, and LiteLLM is
  natively provider-agnostic. **The judgment is reserved to the founder** (`enterpriseaiframework-129`).
  Separately: `config.base.yaml`'s header instructs uncommenting real-provider entries **that do
  not exist in the file** — a real doc bug independent of the ruling.
- **~9 locality points hardcoded** with no templating: `01-tank-pvs.yaml` pins nodeAffinity to
  hostname `k3s-worker` and a tank dataset; `hostAliases` hardcode an IP and tailnet name in three
  manifests; `60-workspace-common.yaml` bakes LAN CIDRs into NetworkPolicy.
- **Three hand-edit seams**, not one: skills need a volume/volumeMount pair in `50-chat.yaml` *and*
  an independent block in `61-workspace.template.yaml` kept in sync by a human; an MCP server needs
  `librechat.yaml` + `opencode.json` (**baked into the workspace image** — a rebuild, not a
  redeploy) + a NetworkPolicy egress rule. Three files, two action types, one conceptual addition.
- **Not gitops-shaped.** 11 `kubectl apply` sites, `kubectl patch` rewriting Secrets in place, no
  plan/apply gate. The only `--dry-run` is client-side YAML generation.
- **~1/3 of action classes are irreversible by nature, not by missing tooling.** Patched Secrets
  (old key overwritten, gateway record deleted *before* the new one is minted); Keycloak realm
  mutations (no export taken first); minted keys (one-way by design); Postgres (**no migration
  framework at all** — `CREATE TABLE IF NOT EXISTS` plus a raw `ALTER TABLE RENAME COLUMN`).
- Authority layer cost: **650–950 LOC**.

---

## 4. C6 — the hardest remaining contradiction

DAP §4.8 sets P0 evidence as *"root cause documented, reproducing test, full suite green,
**rollback verified**, human approval before deploy."*

Minted keys and realm mutations are security-boundary; Postgres is data-loss. These are P0 by any
honest read. **The distribution therefore ships P0 action classes that structurally cannot produce
the required evidence.** The clone design does not fix it — a policy gate permits or denies, but
permitting an irreversible action still leaves no rollback.

**Remedy is not rollback.** Irreversible classes require human approval **at the action, not at
promotion**. Batch-approving a promotion containing one irreversible step gives the possessor no
way to undo the step that went wrong. Split the gate: reversible actions promote as a batch;
irreversible classes are individually gated and individually logged.

---

## 5. Roles (DAP §4.1)

- **Ship authority — assigned**, and mechanically rather than in prose, which is better than the
  reference project manages: the promotion decision *is* the release decision.
- **Architect — already assigned, and the brief did not realise it.** The *distributor* is
  Architect, exercising "sets constraints" through the suite plus the recorded rulings (Valkey not
  Redis, Perses not Grafana, LiteLLM MIT core). Open question 2 ("how opinionated?") is really
  asking *how much Architect authority ships in the box.* State it in AGENTS.md and the role is
  assigned. The possessor takes the role only by forking.
- **Quality gate — partial.** Policy-as-code decides what is *permitted*, not what is *acceptable*.
  A conformance outcome that goes red for a novel reason has no assigned reviewer.
- **Dispatcher — unassigned, sharpest residue.** BYO-model picks a tier once with no escalation
  path when the cheap tier stalls. Open question 7 ("does a weak model install it?") is
  unknowingly a question about this role. **S2 is the escalation trigger.**

---

## 6. Still open, unengaged by any rebuttal

Class membership has no tested negative case (D1–D2). Tenant extraction has been *declared, not
performed* — camp-specific fixtures are live in the repo today (E1), and the upgrade story for a
pinned tenant config against a changing platform contract is an open question stated as a settled
position (E2). The ratchet at N=2 cannot distinguish convergence from a growing pile of special
cases, and nothing budgets for suite *retirement* (C1–C2). Nobody measured the adversarial layer's
own false-negative rate (F1–F2). Liability for autonomous actuation under someone else's
credentials is entirely absent (G2). No harness sweep on a shipped governance package whose
defects have portfolio-wide blast radius (C4, DAP §6.8) — a release blocker on its own.

**N=1 survives completely.** Nothing added a second target. The redesign changed what a wrong
answer *costs* — bounded to a failed diff — but that is orthogonal to whether the grind *converges
to correct* unattended. Cost-of-failure and rate-of-failure are different claims; only the first
moved.

---

## 7. Sign-off condition

DAP compliance re-scored **45% → 58% genuine**. The purist signs at ~75%, reached by closing
exactly two things:

1. **G3** as specified — S1/S2/S3 per outcome plus the configuration surface.
2. **C6** — per-action gating for irreversible classes.

G1′, G4 and G6 are paragraph-sized and ride along. **C4 and C6 are each release blockers on their
own**; 58% is an honest pass on architecture with an incomplete instrument deck.

---

## 8. What flows back to the framework

**N1 — the environment is a fourth node, and its edge does not merely hide information: it answers
differently.** DAP §1.3's third edge is structurally unreliable — you cannot trust you are seeing
all of it. The environment edge is worse in kind. `<SERVICE>_PORT=tcp://…` injection and the k8s
ConfigMap symlink farm are not missing information; the environment answered, and answered
differently than the reference deployment. §2's "design for the union of all possible visibility
states" does not cover this: the union framing assumes states differ in what you can *see*, never
in what is *true*.

And the irreversibility finding exposes a deeper hole: every DAP loop assumes cheap iteration.
Against a substrate where a third of action classes are one-way, **you get one attempt at the
Keycloak realm mutation.** DAP has no theory of a loop that cannot be re-run.

Proposed upstream edits to `dap.md`: a fourth edge in §1.3 characterised by *divergent semantics*
rather than incomplete visibility (the human-AI edge is dark; the environment edge lies); a
boundary condition in §1.7 that the loops assume re-runnable iteration; §7 novelty list; a fifth
entry in §5.4's boundary conditions. **File against the dap repo regardless of what happens to
this proposal.**

<!-- rd-item: enterpriseaiframework-98c -->
