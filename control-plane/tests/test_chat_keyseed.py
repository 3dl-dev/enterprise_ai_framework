"""Pin LibreChat's credential encryption so the chat per-user-key seed cannot silently drift.

app/chat_keyseed.py reproduces LibreChat's v1 `encrypt` (AES-256-CBC, fixed IV, PKCS7, hex) so
the control plane can write a per-user key straight into LibreChat's `keys` collection. If a
LibreChat upgrade changes that scheme, or our reproduction drifts, seeded keys become
undecryptable and every chat user is silently dropped to the "paste a key" prompt — the exact
UX the seed exists to avoid.

THE GOLDEN VECTOR below was produced by LibreChat's OWN `encrypt` (from
@librechat/data-schemas, running image ghcr.io/danny-avila/librechat:v0.8.7), invoked with the
fixed test CREDS_KEY/CREDS_IV here over the exact plaintext a seed writes. Byte-equality proves
our Python reproduction is compatible with the real thing. It is a portable vector — the test
creds are dummy, not the deployment's — so this runs anywhere with no cluster.
"""

import json

from app import chat_keyseed

# Dummy 32-byte key (64 hex) and 16-byte IV (32 hex) — NOT the deployment's creds.
TEST_CREDS_KEY = "0123456789abcdef" * 4
TEST_CREDS_IV = "0123456789abcdef" * 2

# LibreChat's own encrypt(JSON.stringify({apiKey:"fr-sk-GOLDEN-test-0001"})) under those creds.
GOLDEN_PLAINTEXT_KEY = "fr-sk-GOLDEN-test-0001"
GOLDEN_CIPHERTEXT = (
    "07bc77ca35ba9eb88a8fa2d59fbd93e28af0e5bc325cb9aa22a30a7737e18bd5"
    "378cf7ef6a76711cab77d332de33177b"
)


def test_encrypt_matches_librechat_golden_vector():
    """Our AES-256-CBC reproduction is byte-identical to LibreChat's own encrypt."""
    plaintext = json.dumps({"apiKey": GOLDEN_PLAINTEXT_KEY}, separators=(",", ":"))
    got = chat_keyseed._encrypt(plaintext, creds_key=TEST_CREDS_KEY, creds_iv=TEST_CREDS_IV)
    assert got == GOLDEN_CIPHERTEXT, (
        "chat_keyseed._encrypt no longer matches LibreChat's v1 credential encryption. "
        "If LibreChat's crypto changed on an upgrade, seeded keys are undecryptable and "
        "every chat user falls back to the paste-a-key prompt. Re-capture the vector from "
        "the running image's @librechat/data-schemas `encrypt` and reconcile _encrypt."
    )


def test_encrypt_is_deterministic():
    """v1 uses a fixed IV, so the same plaintext always yields the same ciphertext — which is
    what makes the golden vector a stable pin rather than a flake."""
    a = chat_keyseed._encrypt("hello", creds_key=TEST_CREDS_KEY, creds_iv=TEST_CREDS_IV)
    b = chat_keyseed._encrypt("hello", creds_key=TEST_CREDS_KEY, creds_iv=TEST_CREDS_IV)
    assert a == b


def test_encrypt_api_key_wraps_in_apikey_json():
    """The stored plaintext is exactly {"apiKey": "<key>"} with JS-compatible compact
    separators, because LibreChat's custom-endpoint resolver reads `userValues.apiKey`."""
    # Point the module's env-derived creds at the test creds for the wrapper path.
    import app.chat_keyseed as m

    orig_k, orig_v = m._CREDS_KEY, m._CREDS_IV
    m._CREDS_KEY, m._CREDS_IV = TEST_CREDS_KEY, TEST_CREDS_IV
    try:
        assert m.encrypt_api_key(GOLDEN_PLAINTEXT_KEY) == GOLDEN_CIPHERTEXT
    finally:
        m._CREDS_KEY, m._CREDS_IV = orig_k, orig_v


def test_configured_requires_all_three():
    """configured() gates seeding: no Mongo URL or no creds means we do not touch chat."""
    import app.chat_keyseed as m

    saved = (m._MONGO_URL, m._CREDS_KEY, m._CREDS_IV)
    try:
        m._MONGO_URL, m._CREDS_KEY, m._CREDS_IV = "mongodb://x", "aa", "bb"
        assert m.configured() is True
        m._MONGO_URL = ""
        assert m.configured() is False
    finally:
        m._MONGO_URL, m._CREDS_KEY, m._CREDS_IV = saved
