# Platform facts

Baked into this image (see Dockerfile) and always loaded before the tenant's own
instructions (see opencode.json's `instructions` key). Unlike the tenant file, this one is
never a ConfigMap and never changes without a rebuild — everything in it must be something
we can actually verify about the pod, not a preference, because we are asserting it as
true for every deployment, present and future, that anyone ever mounts a different tenant
file on top of.

1. **`localStorage` and `sessionStorage` throw in the preview.** The preview iframe's
   sandbox (`deploy/workspace/shell/index.html`) does not carry `allow-same-origin`, so the
   browser treats it as a unique opaque origin and both storage APIs throw a
   `SecurityError` there. Keep state in a variable, not storage.

## What used to be here, and is not anymore (enterpriseaiframework-cbf / -644)

The camp's original house rules also claimed "there is no internet here" and "never run
npm install or pip install — there is no egress." Those read as platform facts — they
describe the pod's NetworkPolicy, not a preference — so the first draft of this file
carried them forward unedited. They are false, checked twice:

- `deploy/k8s/60-workspace-common.yaml`'s NetworkPolicy has always had an egress rule to
  `0.0.0.0/0` (minus the private ranges) with no port restriction, captioned "The internet,
  for pip / npm / git clone" in its own top comment, present since the very first commit
  that created this NetworkPolicy (883e8a0).
- Confirmed live against the running cluster, not just the checked-in YAML: `kubectl exec`
  into `ws-alice`'s `ttyd` container and `curl -sS -o /dev/null -w '%{http_code}'
  https://registry.npmjs.org/` returned `200`. The pod reaches the internet today.

A platform-facts file that asserts something checkably false is the same defect this file
exists to fix, just moved one layer down — so the two rules are dropped here rather than
carried forward. They still exist, unedited, in the tenant's own instructions (the seed is
`deploy/workspace/AGENTS.md`, byte-for-byte) — a tenant's own text is free to be wrong
about its own deployment, the same way any of an operator's other preferences might be;
what must not happen is the platform image asserting it as a verified fact.

**enterpriseaiframework-644 has since resolved the tenant-content half.** The camp's seed
(`deploy/workspace/AGENTS.md`) keeps both rules — inline everything, do not run installers
— but no longer justifies them with a network claim, because the network claim is false.
The same item corrected the four other places in `deploy/workspace/` that repeated it, and
`tests/test_workspace_network_claims.py` now derives the expectation from the NetworkPolicy
rather than hard-coding it, so the claim cannot be reintroduced on one side without the
other side moving too. This file's decision to drop the two rules is unchanged.

Note for anyone updating a RUNNING deployment: `ensure_tenant_instructions` only seeds
`workspace-tenant-instructions` when it is absent, so a cluster provisioned before this
change still serves the old text until an operator runs
`deploy/bin/provision-workspace.sh <user> --instructions deploy/workspace/AGENTS.md`.
