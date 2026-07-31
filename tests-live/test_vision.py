"""enterpriseaiframework-282, the live-tier half of the vision claim.

WHY THIS FILE EXISTS, SEPARATELY FROM tests/test_scope_items.py

That hermetic test proves "an image reaches a model" against fakeprovider, which
CANNOT see pixels — its whole job (scope item 8: no provider account, no spend) is to
stand in for a real upstream, and the strongest thing it can honestly prove is that an
`image_url` block was not silently dropped before it arrived. It cannot prove a model
actually SAW the image, because fakeprovider has no vision of its own to fail with.

This file proves the claim the hermetic tier structurally cannot: a real, priced,
vision-capable model in the catalogue this bundle renders from Forge's live
`/v1/models`, answering correctly about the actual content of an actual image sent to
it through this gateway. Costs a fraction of a cent per run — the same "spends real
money" tier as test_forge.py, and out of `make test` for the same reason (scope item 8).

Requires the bundle's `make up` to have rendered its LOCAL gateway catalogue WITH a real
FORGE_API_KEY configured (`direnv exec . make up`, or `make forge-config` then `make up`)
— i.e. the same precondition every other test in this directory already has via
`tests-live/conftest.py`'s `env` fixture, which fails loudly rather than skipping when
Forge credentials are not configured.
"""

import re
import struct
import uuid
import zlib

import httpx
import pytest

TIMEOUT = 120.0

# Forge serves a whole vision-language family under different provider paths; matched
# by name here ONLY to pick a candidate to actually test against — never as the proof
# itself. The proof is the model answering correctly about a real image below. Same
# family bundle/litellm/config.base.yaml's fake-vision-large entry documents.
_VISION_NAME_RE = re.compile(r"vl|vision|pixtral", re.IGNORECASE)


def _solid_color_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal, valid, single-color PNG — built with the standard library only
    (no Pillow in this venv), so a vision model has something unambiguous to name."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    row = bytes([0]) + bytes(rgb) * width  # filter byte 0 (None) + RGB pixels
    raw = row * height
    idat = chunk(b"IDAT", zlib.compress(raw, 9))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


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


class TestARealVisionModelIsInTheCatalogue:
    def test_it_is_offered_and_priced(
        self, gateway_url, virtual_key, control_plane_url, admin_headers,
        vision_model_candidate,
    ):
        import time

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
            import time as _t
            _t.sleep(3)
        pytest.fail(
            f"{vision_model_candidate!r} metered at $0 — an unpriced model meters at "
            f"$0, so budgets never trip and the bill under-reports: {body}"
        )


class TestTheVisionModelActuallySeesTheImage:
    """The claim tests/test_scope_items.py's fake stand-in structurally cannot make."""

    @pytest.mark.parametrize("color_name,rgb", [("red", (220, 20, 20)), ("blue", (20, 20, 220))])
    def test_the_model_names_the_color_of_a_real_image_sent_to_it(
        self, gateway_url, virtual_key, vision_model_candidate, color_name, rgb,
    ):
        png = _solid_color_png(64, 64, rgb)
        import base64
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()
        nonce = uuid.uuid4().hex[:8]

        r = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {virtual_key}"},
            json={
                "model": vision_model_candidate,
                "max_tokens": 20,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            f"[{nonce}] This image is a single solid color. Reply with "
                            "ONLY the color's common English name, one word, lowercase."
                        )},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            },
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, (
            f"{vision_model_candidate!r} refused an image turn ({r.status_code}): "
            f"{r.text[:500]}"
        )
        body = r.json()
        answer = body["choices"][0]["message"]["content"].strip().lower()
        assert color_name in answer, (
            f"{vision_model_candidate!r} was sent a solid {color_name} image and "
            f"answered {answer!r} — either it cannot see the image, or the image never "
            "reached it despite the 200"
        )
        usage = body["usage"]
        assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
