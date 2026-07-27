"""Smoke tests for Forge as a real upstream.

The question these answer is not "does Forge work" — that is Forge's problem — but "does
our layer carry real traffic to it correctly, and does our bill agree with theirs."
"""

import time
import uuid

import httpx
import pytest

from conftest import forge_usage

TIMEOUT = 120.0

# The cheapest priced model in the catalog. Every test that spends money uses it, and
# keeps max_tokens tiny — a full run costs a fraction of a cent.
CHEAP_MODEL = "claude-haiku-4-5"


class TestForgeReachable:
    def test_key_is_an_agent_key_with_expected_scope(self, forge_url, forge_headers, env):
        r = httpx.get(f"{forge_url}/v1/whoami", headers=forge_headers, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        who = r.json()
        assert who["account_id"] == env["FORGE_ACCOUNT_ID"]
        assert who["role"] == "agent", (
            f"shipping a {who['role']} key at runtime — a leaked runtime credential "
            "should only be able to spend, not mint more keys"
        )

    def test_catalog_is_reachable(self, forge_url, forge_headers):
        r = httpx.get(f"{forge_url}/v1/models", headers=forge_headers, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) > 0


class TestGatewayCarriesForgeTraffic:
    """The full chain: virtual key -> our gateway -> Forge -> provider."""

    def test_completion_through_a_virtual_key(self, gateway_url, virtual_key):
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}"},
            json={
                "model": CHEAP_MODEL,
                "max_tokens": 20,
                "messages": [{"role": "user",
                              "content": f"Reply with exactly: ok {uuid.uuid4().hex[:6]}"}],
            },
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["choices"][0]["message"]["content"].strip()
        assert body["usage"]["total_tokens"] > 0

    def test_streaming_is_incremental(self, gateway_url, virtual_key):
        chunks = 0
        with httpx.stream(
            "POST", f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}"},
            json={
                "model": CHEAP_MODEL, "max_tokens": 30, "stream": True,
                "messages": [{"role": "user",
                              "content": f"Count to three. {uuid.uuid4().hex[:6]}"}],
            },
            timeout=TIMEOUT,
        ) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    chunks += 1
        assert chunks >= 3, f"expected incremental delivery, got {chunks} chunks"

    def test_anthropic_native_dialect(self, gateway_url, virtual_key):
        """The terminal coding agent speaks this, not the OpenAI dialect."""
        r = httpx.post(
            f"{gateway_url}/v1/messages",
            headers={"Authorization": f"Bearer {virtual_key}",
                     "anthropic-version": "2023-06-01"},
            json={
                "model": CHEAP_MODEL, "max_tokens": 20,
                "messages": [{"role": "user",
                              "content": f"Reply with exactly: ok {uuid.uuid4().hex[:6]}"}],
            },
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "message"
        assert body["usage"]["input_tokens"] > 0

    def test_unknown_model_is_refused(self, gateway_url, virtual_key):
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}"},
            json={"model": "no-such-model-anywhere",
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=TIMEOUT,
        )
        assert r.status_code >= 400


class TestMoneyIsCorrect:
    """The claim that matters: our bill agrees with theirs."""

    def test_every_forge_model_in_our_catalog_is_priced(self, control_plane_url, admin_headers):
        """An unpriced model meters at $0, so budgets never trip and the bill
        under-reports — silently. Config generation excludes unpriced models for exactly
        this reason; this asserts none slipped through."""
        r = httpx.get(f"{control_plane_url}/admin/unpriced", headers=admin_headers,
                      timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        assert r.json()["ok"], f"models metering at $0: {r.json()['models']}"

    def test_our_computed_cost_matches_what_forge_billed(
        self, gateway_url, virtual_key, forge_url, forge_admin_key, env
    ):
        """Reconciliation to the cent, independently computed on both sides.

        Our gateway prices from Forge's published rate card; Forge prices from its own
        meter. If those ever diverge, the savings number this whole layer exists to
        produce is fiction — so the agreement is asserted rather than assumed.
        """
        before = len(forge_usage(forge_url, forge_admin_key, env["FORGE_ACCOUNT_ID"]))

        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}"},
            json={
                "model": CHEAP_MODEL, "max_tokens": 25,
                "messages": [{"role": "user",
                              "content": f"Reconcile check {uuid.uuid4().hex[:8]}"}],
            },
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        usage = r.json()["usage"]
        prompt_tokens = usage["prompt_tokens"]
        completion_tokens = usage["completion_tokens"]

        # Forge records usage asynchronously; poll rather than sleep-and-hope.
        deadline = time.monotonic() + 90
        event = None
        while time.monotonic() < deadline:
            events = forge_usage(forge_url, forge_admin_key, env["FORGE_ACCOUNT_ID"])
            if len(events) > before:
                match = [
                    e for e in events[before:]
                    if e.get("input_tokens") == prompt_tokens
                    and e.get("output_tokens") == completion_tokens
                ]
                if match:
                    event = match[-1]
                    break
            time.sleep(5)

        assert event is not None, (
            "our request never appeared in Forge's usage — the two ledgers cannot be "
            "reconciled at all"
        )

        forge_cost = float(event["cost_usd"])
        # Recompute from the published rate card the generated config was built from.
        ours = (prompt_tokens * 1.0625 + completion_tokens * 5.3125) / 1_000_000

        assert abs(ours - forge_cost) < 1e-9, (
            f"our price {ours} disagrees with Forge's {forge_cost} for "
            f"{prompt_tokens} in / {completion_tokens} out"
        )

    def test_attribution_reaches_forge(
        self, gateway_url, virtual_key, forge_url, forge_admin_key, env
    ):
        """Without attribution you get a token bill and no idea which system spent it."""
        httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}"},
            json={"model": CHEAP_MODEL, "max_tokens": 15,
                  "messages": [{"role": "user", "content": f"attr {uuid.uuid4().hex[:6]}"}]},
            timeout=TIMEOUT,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            events = forge_usage(forge_url, forge_admin_key, env["FORGE_ACCOUNT_ID"])
            tagged = [e for e in events if e.get("project") == "enterprise-ai-framework"]
            if tagged:
                return
            time.sleep(5)
        pytest.fail(
            "no usage event carried X-Forge-Project — our traffic is unattributable "
            "inside Forge"
        )


class TestSovereignty:
    def test_floor_violation_is_refused_with_alternatives(self, forge_url, forge_headers):
        """Forge rejects a request that violates the pinned floor and names compliant
        models, so a caller can retry rather than guess.

        Asserted against Forge directly. Whether our gateway can *forward* a per-request
        sovereignty pin is a separate question, tested below.
        """
        r = httpx.post(
            f"{forge_url}/v1/chat/completions",
            headers={**forge_headers, "X-Forge-Sovereignty": "us-only"},
            json={"model": "glm-4.7@deepinfra", "max_tokens": 10,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=TIMEOUT,
        )
        # Forge answers 451 Unavailable For Legal Reasons. Its consumer quickstart
        # documents 403 for a sovereignty violation, so anything written against the doc
        # would miss this. 451 is arguably the better code; the doc is what is wrong.
        # Both are accepted here so this does not break when the docs are reconciled.
        assert r.status_code in (403, 451), (
            f"expected a sovereignty refusal, got {r.status_code}: {r.text[:300]}"
        )
        body = r.json()
        assert body["error"]["type"] == "sovereignty_violation", body
        alternatives = body["error"].get("alternatives")
        assert alternatives, f"refusal named no alternatives, so a caller must guess: {body}"
        assert all(a["sovereignty"] == "us-only" for a in alternatives), (
            "alternatives offered do not all satisfy the requested floor"
        )

    def test_whether_a_per_request_sovereignty_pin_survives_our_gateway(
        self, gateway_url, virtual_key
    ):
        """Documents a real limitation rather than asserting a wish.

        Sovereignty is enforced per request via a header. If our gateway drops caller
        headers, then a caller behind our layer cannot pin a stricter floor than the
        key's default — which is a capability regression the design should know about.
        """
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}",
                     "X-Forge-Sovereignty": "us-only"},
            json={"model": "glm-4.7@deepinfra", "max_tokens": 10,
                  "messages": [{"role": "user", "content": f"sov {uuid.uuid4().hex[:6]}"}]},
            timeout=TIMEOUT,
        )
        if r.status_code == 403:
            return  # forwarded and enforced

        assert r.status_code == 200, r.text
        pytest.xfail(
            "our gateway does not forward X-Forge-Sovereignty, so a caller behind the "
            "layer cannot pin a stricter sovereignty floor per request; the key's floor "
            "still applies. Recorded in docs/design/dogfood-findings.md."
        )
