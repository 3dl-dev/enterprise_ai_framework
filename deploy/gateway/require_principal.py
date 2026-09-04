"""No principal, no service.

WHAT THIS FIXES, MEASURED RATHER THAN REASONED

On the live cluster on 2026-07-29, `GET /admin/spend` reported 42 requests and $0.1133
under `(unattributed)`, 25 of them — 98% of the unattributed money — spent in a two-hour
window on 2026-07-28 between 21:31Z and 23:31Z, some 26 hours after the deployment. Real
agent traffic: <model> at 16k-55k tokens a request, plus a burst of one-shot
model probes. Finding 25 ("rows written before the alias metadata existed") did not cover
it, and neither did finding 34 (a principal that exists, named inconsistently). There was
no principal at all.

Reading the raw ledger settled it. Every one of those rows carries
`api_key = c8c29e12…4a560`, which is the SHA-256 of `GATEWAY_MASTER_KEY`, with
`metadata.user_api_key_alias = null` and `metadata.user_api_key_user_id = 'default_user_id'`.
That is LiteLLM's signature for a request authenticated with the master key itself.

So the gateway's *administrative root credential* was also a valid inference credential.
Anything holding it could buy tokens, and the resulting spend row had nothing to join to:
no key alias on the row, and no `LiteLLM_VerificationToken` to fall back to, because the
master key has no such row. The bill was honest — it said `(unattributed)` — but it could
not name the money, and no rendering of it ever could.

Two consequences, and the second is worse than the first:

  - **Attribution.** Money lands on the one bill belonging to nobody. That is the exact
    failure this project exists to prevent.
  - **Budgets.** Budgets bind to virtual keys. A credential with no key row has no budget
    and can never exhaust one, so the single admission point admits it without limit.

WHY REFUSAL RATHER THAN A BETTER LABEL

Relabelling the bucket — attributing master-key spend to a synthetic `(root)` principal —
would make the number attributable-looking without making it attributable. The holder of
the master key is whichever process was handed it; a label invented at query time names
none of them, and it would restore neither budget enforcement nor revocation.

The mint path is unaffected: this hook runs only on the inference call types, so the
master key keeps doing the one job it should have — administering keys. It just stops
being able to spend. Fail closed: a credential the ledger cannot name is refused before
it reaches an upstream, so the refusal costs nothing and produces no billable row.

Any key whose alias is missing or blank is refused by the same rule, not just the master
key. The property being enforced is "the ledger can name who spent this", and a virtual
key minted without an alias breaks it identically. The control plane always mints with a
`username::surface` alias, so this is a floor, not a constraint on normal issuance.
"""

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

# The message an operator sees. It has to say what to do, because the most likely reader
# is someone who wired a script to the master key because it was the credential to hand.
_DETAIL = {
    "error": {
        "type": "no_attributable_principal",
        "message": (
            "This credential carries no key alias, so any spend it incurred could not be "
            "attributed to anyone on the bill and no budget would apply to it. Inference "
            "requires a virtual key minted with a `username::surface` alias. The gateway "
            "master key administers keys; it does not buy tokens."
        ),
    }
}


def principal_of(user_api_key_dict) -> str:
    """The alias the ledger will attribute this request to, or "" if there is none.

    Kept separate from the hook so the rule can be exercised directly by a unit test
    without standing a proxy up, and so there is exactly one definition of "named".
    """
    alias = getattr(user_api_key_dict, "key_alias", None)
    if not isinstance(alias, str):
        return ""
    return alias.strip()


class RequirePrincipal(CustomLogger):
    """Refuse any inference request the spend ledger could not attribute."""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not principal_of(user_api_key_dict):
            raise HTTPException(status_code=403, detail=_DETAIL)
        return data


handler = RequirePrincipal()
