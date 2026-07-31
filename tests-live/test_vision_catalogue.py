"""enterpriseaiframework-282's live-tier proof: a real, priced, vision-capable model is
rendered into the gateway catalogue and is selectable.

SCOPE, STATED PRECISELY — this file was named test_vision.py until the wave-3 rework and
that name overstated what it proves. Read this before trusting the filename.

282's done-condition is "at least one vision-capable model priced and selectable" — a
catalogue and pricing claim, not an image-comprehension claim. This file proves exactly
that: a vision-family model name (matching Forge's `vl|vision|pixtral` naming) is present
in the LOCAL catalogue this bundle renders from Forge's live `/v1/models`
(bundle/bin/render-gateway-config.py), answers an ordinary text turn through this gateway,
and is priced (not metered at $0 — see /admin/unpriced, which exists because an unpriced
model silently under-reports the bill).

WHAT THIS FILE DOES NOT PROVE, AND WHY THAT ASSERTION LIVES ELSEWHERE

An earlier version of this file also asserted that the model correctly names the color of
a real image sent to it — the full round-trip claim. That assertion is TRUE IN SPEC but
FALSE IN THIS DEPLOYMENT today: measured directly against FORGE_BASE_URL, bypassing the
gateway, Forge's own request validator rejects `data:` image URLs for every vision model
in the catalogue ('invalid URL scheme "data": only http/https allowed'), which is the only
URL shape LibreChat's local file strategy emits (encode.js). The http(s) alternative fails
too — Bedrock demands inline base64, deepinfra cannot fetch a URL. So the real image path
400s regardless of which vision model is chosen.

That is a product defect, not a test defect. It is filed as enterpriseaiframework-e03
(gated for a founder decision between an S3/presigned file strategy and a Forge-side
fix), wired as a blocker of enterpriseaiframework-020 ("a user pastes a screenshot ...
and the model answers about its contents"), which is the item that actually owns the
round-trip claim. The full assertion — send a real image, assert the model names its
color — is recoverable from this file's git history at commit c8cb0a5 /
tests-live/test_vision.py (class TestTheVisionModelActuallySeesTheImage) and belongs back
in a live-tier test the moment e03 lands; 020 cannot close without re-adding it. It is not
re-added here, skipped or otherwise, because a test that can only fail until a founder
decision ships is not this item's proof and CLAUDE.md's "no skipped tests" rule means it
should not sit in this branch pretending to be one.

Requires the bundle's `make up` to have rendered its LOCAL gateway catalogue WITH a real
FORGE_API_KEY configured (`direnv exec . make up`, or `make forge-config` then `make up`)
— i.e. the same precondition every other test in this directory already has via
`tests-live/conftest.py`'s `env` fixture, which fails loudly rather than skipping when
Forge credentials are not configured.
"""

import re
import time
import uuid

import httpx
import pytest

TIMEOUT = 120.0

# Forge serves a whole vision-language family under different provider paths; matched
# by name here ONLY to pick a candidate to actually test against — never as the proof
# itself. The proof is the model being priced and answering a real turn below. Same
# family bundle/litellm/config.base.yaml's fake-vision-large entry documents.
_VISION_NAME_RE = re.compile(r"vl|vision|pixtral", re.IGNORECASE)


@pytest.fixture(scope="module")
def catalog_yaml() -> str:
    from conftest import BUNDLE

    path = BUNDLE / "litellm" / "config.generated.yaml"
    if not path.exists():
        pytest.fail(f"{path} missing — run `make up` first")
    return path.read_text()


@pytest.fixture(scope="module")
def vision_model_candidate(catalog_yaml: str) -> str:
    names = re.findall(r"^\s*-\s*model_name:\s*(\S+)\s*$", catalog_yaml, re.MULTILINE)
    candidates = [n for n in names if _VISION_NAME_RE.search(n) and "fake" not in n]
    if not candidates:
        pytest.fail(
            "no vision-family model name (matching vl|vision|pixtral) is in the "
            "rendered catalogue — either Forge's catalogue changed or FORGE_API_KEY "
            "was not configured when `make up` last rendered it"
        )
    return sorted(candidates)[0]


class TestARealVisionModelIsInTheCatalogueAndPriced:
    """Proves enterpriseaiframework-282's claim only: catalogue membership + pricing.

    Does NOT send an image and does NOT claim the model can see one — see the module
    docstring for why that assertion lives with enterpriseaiframework-020/e03 instead.
    """

    def test_it_is_offered_and_priced(
        self, gateway_url, virtual_key, control_plane_url, admin_headers,
        vision_model_candidate,
    ):
        since = time.strftime("%Y-%m-%dT%H:%M:%S+00", time.gmtime(time.time() - 5))
        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}"},
            json={
                "model": vision_model_candidate, "max_tokens": 10,
                "messages": [{"role": "user",
                              "content": f"reply with exactly: ok {uuid.uuid4().hex[:6]}"}],
            },
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, (
            f"{vision_model_candidate!r} is in the rendered catalogue but refused a "
            f"plain turn ({r.status_code}): {r.text[:300]}"
        )
        assert r.json()["usage"]["total_tokens"] > 0

        deadline = time.monotonic() + 60
        body = None
        while time.monotonic() < deadline:
            body = httpx.get(
                f"{control_plane_url}/admin/unpriced",
                headers=admin_headers, params={"since": since}, timeout=TIMEOUT,
            ).json()
            if body["ok"]:
                return
            time.sleep(3)
        pytest.fail(
            f"{vision_model_candidate!r} metered at $0 — an unpriced model meters at "
            f"$0, so budgets never trip and the bill under-reports: {body}"
        )
