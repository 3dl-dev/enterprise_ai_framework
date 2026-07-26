# Switchboard

**You're spending six figures a year on AI and you can't answer basic questions about it.**

Who spent what. Which team. On which model. Whether it helped. Whether you could have paid less.
Your provider's console shows you *their* half of it, attributed to an API key rather than a
person. Nothing shows you all of it.

Switchboard is one layer that everything goes through. Your people keep the tools they already
use. You get one login, one bill, one audit trail across every provider — and then the bill starts
going down.

**Status: the design is finished. No code has been written. That is deliberate — see
[Does anyone care?](#does-anyone-care) below.**

---

## The problem, concretely

You are somewhere between $200k and several million a year across Anthropic and OpenAI. Some of
it is a chat app your staff use. Some is a coding assistant. Some is applications your teams
built. It arrives as two or three invoices and a console per vendor.

Ask any of these and see how far you get:

- Which department spent the most last month, and on what?
- Can I cap the platform team at $8k without cutting them off mid-sprint?
- How much of this spend produced work anyone kept?
- What would we save by running some of it ourselves — and *which* some?
- If I want to move providers, what breaks?

Not hard questions. There is just nothing that can answer them, because no single system sees all
of it.

## What Switchboard is

A layer between your people and the AI providers.

Everything routes through it — the chat app, the coding assistants, your internal applications.
Nothing about the user experience changes. They ask Claude a question, they get Claude's answer.
The only difference is that the request went through your infrastructure on the way, using your
existing provider contracts and your existing API keys.

What you get on the first day:

- **One login.** Wired to the identity provider you already run. Chat, coding assistant, and
  internal APIs all behind it.
- **One dashboard.** Spend and usage by person, team, model, and surface. Across every provider.
  Exportable, and it reconciles against the invoices you actually pay.
- **Budgets that stop.** Not warn. Per person, per team, per project.
- **An audit trail** your compliance people will accept.
- **A lower bill**, from caching repeated questions, batching what isn't urgent, right-sizing
  which model handles what, and catching the runaway loop nobody noticed.

What you get later, without changing anything:

- **A record of your own work.** Every question, every answer, and whether a human kept it. That
  is a corpus of how your company actually operates. Nobody else has it — not even the provider
  who generated the answers.
- **The option to stop renting.** Run open models on hardware you rent or own, for the work where
  they're good enough. Switchboard proves which work that is by testing candidates against your
  real traffic before anything moves. The bill drops again.

You are never required to take that last step. A company that stays entirely on frontier APIs
forever still gets one login, one bill, and one audit trail, which is more than they have today.

## Why this doesn't already exist

It nearly does, four times over. And every time, the same layer is behind a paywall.

| Project | Open | Paywalled |
|---|---|---|
| Open WebUI | The chat interface | Branding removal above 50 users — a licence change made in April 2025 |
| Onyx | Search and chat | SSO, RBAC, audit logs, admin APIs |
| LiteLLM | The proxy | SSO, RBAC, audit logs, secret management |
| Cline | The coding agent | Fleet auth, policy, telemetry — a separate commercial product |

Four unrelated teams. Four independent decisions. The same line drawn in the same place every
time: **the engine is free, and the thing that makes it deployable inside an organisation costs
money.**

That is not villainy. Enterprise governance is unglamorous, expensive, and never finished — nobody's
weekend project is SCIM provisioning — so somebody has to fund it, and paywalling it is the
mechanism the market found.

Switchboard's bet is that this stopped being true. That layer is now cheap enough to build that it
doesn't need a paywall to exist.

## What's different here

**Apache 2.0. No contributor licence agreement. No enterprise tier. No feature held back.** There
is no version of this with a sales call attached, and the licensing makes that irreversible rather
than a promise.

**Nothing phones home.** No telemetry to us, and no service of ours in any data path. It runs
air-gapped. Your usage data is yours, verifiably — there is an inspectable record of exactly what
was collected and what it was used for.

**You keep your own contracts and your own keys.** We are not a reseller and we don't sit between
you and your vendor commercially. Switchboard is software you run.

**Nothing is welded in.** The chat app, the coding assistant, the inference engine, the identity
provider, the dashboards — each is a component behind a defined interface with a conformance test.
Already standardised on something? Swap it. The defaults are opinions, not requirements.

## Does anyone care?

**This is the only question that matters right now, and it's why there's no code yet.**

We can build it. That isn't in doubt and it isn't interesting. The failure mode we're trying to
avoid is the one where a perfectly good tool ships to an empty room.

So the design is published first, in full, including the parts that are unresolved. Read it, tell
us it's wrong, tell us it's obvious, or tell us you'd run it.

- **[The design](docs/design/design.md)** — the complete architecture, every decision with the
  losing argument stated, the attack register, and fourteen known gaps we haven't closed.
- **[The brief](docs/design/brief.md)** — what we set out to build and why.

**If this would be useful to you**, open an issue. Tell us roughly what you spend, what you're
running today, and which of the five questions at the top of this page you most want answered. If
you'd rather not do that publicly, the same thing by email is fine.

**If you think this is wrong**, that's more useful. The design document lists what we think would
falsify each claim. Point at one.

We're not collecting emails, running a waitlist, or building a landing page funnel. Interest gets
measured by whether anyone shows up.

## What we won't do

Reimplement an inference engine, a chat interface, a coding agent, a GPU price catalogue, or a
model trainer. All of those exist and are good. The gap is the layer that makes them a system, and
that is the only thing being built here.

---

*Third Division Labs. The name is provisional — nothing depends on it yet.*
