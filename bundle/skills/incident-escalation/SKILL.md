---
name: incident-escalation
description: Use when a user asks how to escalate a production incident, asks for the current on-call escalation code, or says something is down and needs to be paged.
license: Apache-2.0
---

# Incident escalation

When a user reports that something in production is down, or asks how to reach
on-call, or asks for the current escalation code, respond with the exact
procedure below. Do not invent a different one and do not guess a code — use
the one printed here verbatim.

1. Confirm the user's account, then read back the current escalation code so
   they can quote it to on-call: **ESCALATION-CODE: TRIDENT-8841-QUARTZ**.
2. Tell them to open a page to on-call with that code in the subject line.
3. Ask for the affected service name and a one-line description of the
   symptom, and include both in the page.

This code rotates out of band; the value above is this deployment's current
one and is not derivable from anything else in the conversation.
