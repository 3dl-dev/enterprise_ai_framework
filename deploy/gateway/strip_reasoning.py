"""Gateway response hygiene: strip reasoning traces, normalise artifact fences.

Both problems are provider/model behaviour rather than client behaviour, so both are
fixed here once instead of in every surface that renders model output.

REASONING

GLM models served through DeepInfra return their chain of thought in a separate
`reasoning_content` field. Measured against this gateway, the provider ignores every
documented way to turn it off — `reasoning_effort: none`, `thinking: {type: disabled}`,
`chat_template_kwargs: {thinking: false}` all come back unchanged. Aider renders it
unconditionally (base_coder.py prepends it, with no flag), and so would any other client.

Stripping it here does NOT stop the model reasoning and does NOT stop you paying for
those tokens — they are generated and billed upstream either way. Token counts in the
metering ledger are deliberately untouched: a gateway that hid reasoning AND stopped
counting it would understate the bill, which is the failure this project cares most about.

ARTIFACT FENCES

The chat surface renders runnable programs using Anthropic's artifact format — a
`:::artifact{...}` opening fence. Claude models emit it natively; it is their own
convention. Models trained without it approximate it, and GLM emits `::artifact{...}`
with two colons, consistently, however firmly the system prompt states three. The parser
does not match, so the user is shown raw HTML instead of a running program.

Prompting was tried first, twice, and does not hold. This rewrite is deterministic and
model-agnostic, so the next model with its own near-miss is covered by the same rule.
"""

import re

from litellm.integrations.custom_logger import CustomLogger

# Providers are inconsistent about which of these they populate.
_REASONING_FIELDS = ("reasoning_content", "reasoning")

# One-to-four colons directly before `artifact{`, not preceded by another colon or a word
# character. Narrow on purpose: ordinary prose containing colons is not a match.
_OPEN_FENCE = re.compile(r"(?<![:\w]):{1,4}artifact\{")

# The longest tail that could still grow into an opening fence on the next chunk.
_FENCE_TARGET = ":::artifact{"


def _normalise_fences(text: str) -> str:
    if "artifact{" not in text:
        return text
    return _OPEN_FENCE.sub(":::artifact{", text)


def _split_pending_fence(text: str) -> tuple[str, str]:
    """Split off a trailing fragment that might be the start of a fence.

    Returns (carry, emit). A chunk ending in `::art` must not be emitted yet — the rest
    of the fence arrives next. Anything that cannot become a fence is emitted immediately
    so streaming stays responsive.
    """
    for n in range(min(len(text), len(_FENCE_TARGET) - 1), 0, -1):
        tail = text[-n:]
        # A tail is pending if it is a prefix of the fence, allowing for the two-colon
        # variant we are correcting (`::a`, `::art`, …) as well as the correct one.
        if _FENCE_TARGET.startswith(tail) or _FENCE_TARGET[1:].startswith(tail):
            return tail, text[:-n]
    return "", text


def _strip(obj) -> None:
    """Remove reasoning fields from a message or delta, in place."""
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
                # Frozen or computed attribute. Showing reasoning is cosmetic; a 500 is
                # not, so leave it rather than fail the request.
                pass


def _get_delta(choice):
    delta = getattr(choice, "delta", None)
    if delta is None and isinstance(choice, dict):
        delta = choice.get("delta")
    return delta


def _get_content(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get("content")
    return getattr(obj, "content", None)


def _set_content(obj, value) -> None:
    if isinstance(obj, dict):
        obj["content"] = value
        return
    try:
        obj.content = value
    except (AttributeError, ValueError):
        pass


def _strip_response(response):
    for choice in getattr(response, "choices", None) or []:
        _strip(getattr(choice, "message", None))
        _strip(_get_delta(choice))
        if isinstance(choice, dict):
            _strip(choice.get("message"))
    return response


def _rewrite_whole(response):
    """Non-streaming: the complete text is present, so rewrite it directly."""
    for choice in getattr(response, "choices", None) or []:
        for holder in (getattr(choice, "message", None),
                       choice.get("message") if isinstance(choice, dict) else None):
            text = _get_content(holder)
            if isinstance(text, str) and text:
                _set_content(holder, _normalise_fences(text))
    return response


class StripReasoning(CustomLogger):
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        return _rewrite_whole(_strip_response(response))

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data
    ):
        """Rewrite fences across chunk boundaries without dropping text.

        Two mechanisms working together:
          - a carry string holds a trailing fragment that could still become a fence,
            and prepends it to the next chunk;
          - one chunk of delay, so that when the stream ends there is always a chunk
            left to attach the final carry to. Without the delay the carry has nowhere
            to go and would be silently swallowed.
        """
        carry = ""
        pending = None

        async for chunk in response:
            chunk = _strip_response(chunk)

            for choice in getattr(chunk, "choices", None) or []:
                delta = _get_delta(choice)
                text = _get_content(delta)
                if not isinstance(text, str) or not text:
                    continue
                carry, emit = _split_pending_fence(_normalise_fences(carry + text))
                _set_content(delta, emit)

            if pending is not None:
                yield pending
            pending = chunk

        if pending is not None:
            if carry:
                # Flush the held fragment onto the last chunk. It was never a fence, so
                # it is ordinary text and must still reach the user.
                for choice in getattr(pending, "choices", None) or []:
                    delta = _get_delta(choice)
                    text = _get_content(delta)
                    if isinstance(text, str):
                        _set_content(delta, text + carry)
                        carry = ""
                        break
            yield pending


handler = StripReasoning()
