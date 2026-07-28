"""Strip provider reasoning traces from responses at the gateway.

WHY THIS IS AT THE GATEWAY AND NOT IN A SURFACE

GLM models served through DeepInfra return their chain of thought in a separate
`reasoning_content` field alongside the answer. Three things make that a gateway problem
rather than a client one:

1. The provider will not turn it off. Measured against this gateway: `reasoning_effort:
   none`, `thinking: {type: disabled}` and `chat_template_kwargs: {thinking: false}` all
   come back with reasoning_content unchanged.
2. Aider renders it unconditionally — `base_coder.py` prepends the formatted reasoning to
   every displayed response, with no flag to suppress it.
3. It is not one client's problem. Any surface that shows model output shows this, so
   fixing it in a surface means fixing it again in the next surface.

Stripping here fixes the chat surface, the coding agent, and anything added later, once.

WHAT THIS DOES NOT DO

It does not stop the model reasoning, and it does not stop you paying for those tokens —
they are generated upstream and billed as output either way. It only stops them being
displayed. Reducing the spend needs a model that reasons less, or a provider that honours
a disable flag; neither is available in the priced catalogue today.

The token counts in the metering ledger are untouched, so the bill still reflects what was
actually generated. A gateway that hid reasoning AND stopped counting it would understate
spend, which is the failure mode this project cares most about.
"""

from litellm.integrations.custom_logger import CustomLogger

# Providers are inconsistent about which of these they populate.
_REASONING_FIELDS = ("reasoning_content", "reasoning")


def _strip(obj) -> None:
    """Remove reasoning fields from a message or delta, in place.

    Handles both pydantic-style objects and plain dicts because LiteLLM hands back
    different shapes on the streaming and non-streaming paths.
    """
    if obj is None:
        return
    for field in _REASONING_FIELDS:
        if isinstance(obj, dict):
            if obj.get(field) is not None:
                obj[field] = None
        elif getattr(obj, field, None) is not None:
            try:
                setattr(obj, field, None)
            except (AttributeError, ValueError):
                # A frozen or computed attribute: leave it rather than fail the request.
                # Showing reasoning is a cosmetic defect; a 500 is not.
                pass


def _strip_response(response):
    for choice in getattr(response, "choices", None) or []:
        _strip(getattr(choice, "message", None))
        _strip(getattr(choice, "delta", None))
        if isinstance(choice, dict):
            _strip(choice.get("message"))
            _strip(choice.get("delta"))
    return response


class StripReasoning(CustomLogger):
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        return _strip_response(response)

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data
    ):
        async for chunk in response:
            yield _strip_response(chunk)


handler = StripReasoning()
