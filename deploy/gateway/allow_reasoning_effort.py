"""Restore `reasoning_effort` past LiteLLM's own drop-params gate.

WHAT THIS FIXES, MEASURED RATHER THAN REASONED (enterpriseaiframework-282)

`litellm_settings.drop_params: true` (deploy/gateway/config.base.yaml) silently discards
any request field a model's provider config does not list as "supported", rather than
erroring. For every model in this gateway's catalogue — every fake, and every real Forge
model, `deepseek-r1` included — that provider config is `litellm.OpenAIGPTConfig`
(`litellm/llms/openai/chat/gpt_transformation.py`): `render-gateway-config.py` reaches
Forge as `model: openai/<id>` because Forge is itself OpenAI-wire-compatible, and LiteLLM
only swaps in a config that lists `reasoning_effort` as supported
(`OpenAIOSeriesConfig`/`OpenAIGPT5Config`) when the model STRING matches a literal OpenAI
o-series or gpt-5 name pattern (`get_provider_chat_config`,
`litellm/utils.py::ProviderConfigManager`) — a check with no path through `model_info`,
so nothing this bundle configures per-model can satisfy it.

Measured directly against this gateway, bypassing the chat surface entirely: a raw
`POST /v1/chat/completions` with `reasoning_effort: "low"` against `fake-large` reached
fakeprovider with `reasoning_effort: None` in the request body it actually received
(`GET /debug/prompts`). Before this hook existed, that was true of every model this
gateway serves — the Parameters panel's reasoning-effort control could never reach ANY
model, not merely the fakes.

WHY A CALLBACK RATHER THAN `drop_params: false`

`drop_params: true` is a blanket safety net protecting every OTHER param this project has
not audited against every provider in the catalogue — turning it off to fix one field
reopens the door to 400s from providers that reject a field they don't recognise, for
every request rather than the ones that need this exemption. LiteLLM has a narrower,
official escape hatch for exactly this: a request may name its own additional supported
params via `allowed_openai_params` (`litellm/utils.py`, `get_optional_params`'s
`allowed_openai_params` argument) and drop_params leaves those alone. Nothing upstream of
this gateway (LibreChat's custom-endpoint request builder) sends that field itself, so
this hook adds it — but only on a request that already carries `reasoning_effort`, so
every other request's drop_params protection is completely unchanged.

WHY THIS DOES NOT HAND-MAINTAIN A MODEL LIST

It runs on the SHAPE of the request (does it carry `reasoning_effort`), never on the
model name — the same reason `require_principal.py` and `strip_reasoning.py` need no
per-model list either. A model whose provider genuinely rejects the field with a 400 is
now free to say so, which is more honest than a request the caller believed included a
setting that was silently discarded before it ever left the gateway.
"""

from litellm.integrations.custom_logger import CustomLogger


class AllowReasoningEffort(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if data.get("reasoning_effort") is not None:
            allowed = set(data.get("allowed_openai_params") or ())
            allowed.add("reasoning_effort")
            data["allowed_openai_params"] = list(allowed)
        return data


handler = AllowReasoningEffort()
