"""Give the chat surface a per-user key, so it conforms to freerouter's identity model.

THE PROBLEM THIS SOLVES

freerouter's identity is the API key: the key *is* the principal, and possession is
authenticated. Every other surface already works that way — a workspace or an agent holds
its own `<user>::<surface>` key, so freerouter attributes its spend unspoofably.

Chat was the exception. LibreChat served everybody through ONE shared key and identified the
person by a `user` field it puts in the request body — LibreChat's own Mongo `_id`. That is a
*self-asserted payload*: anyone holding the shared key could set it to anyone. LiteLLM honored
it (that is how per-user chat billing worked, via `end_user`), but freerouter does not, and
teaching it to would import a spoofable signal into a model whose whole strength is that
identity cannot be forged. The capture/audit ledger is only trustworthy if attribution cannot
be forged — a payload field can be, a key cannot.

So chat conforms to freerouter, not the reverse: each chat user gets their OWN `<user>::chat`
freerouter key, and LibreChat sends it. LibreChat's mechanism for a per-user key is
`apiKey: "user_provided"` — normally the USER pastes their key. We keep the zero-config UX by
having the operator SEED each user's key into LibreChat's own credential store, so nobody
pastes anything. LibreChat then picks the key from ITS authenticated session, never from a
client field — unspoofable by construction.

WHY IT WRITES ANOTHER COMPONENT'S DATABASE, WHICH IS NOT SOMETHING WE DO LIGHTLY

There is no LibreChat API to set a user's key as an operator; the store is its Mongo. This is
the WRITE counterpart to chat_identity.py's read-only bridge — deliberately narrow (one
collection, the shape LibreChat itself reads), isolated in this module so the coupling is
visible, and guarded: the encryption is byte-for-byte LibreChat's own v1 credential scheme,
pinned by a golden vector so a LibreChat upgrade that changes it fails a test loudly rather
than silently seeding keys nobody can decrypt.

VERIFIED AGAINST THE RUNNING v0.8.7 (image ghcr.io/danny-avila/librechat:v0.8.7):
  * the custom-endpoint resolver reads `getUserKeyValues({ userId, name: endpoint }).apiKey`,
    so the doc is `{ userId, name: <endpoint name>, value, expiresAt }` in the `keys`
    collection, and `value` is `encrypt(JSON.stringify({ apiKey }))`;
  * `encrypt` is AES-256-CBC with `key = fromhex(CREDS_KEY)`, `iv = fromhex(CREDS_IV)`,
    PKCS7 padding, hex output — deterministic (fixed IV), reproduced in `_encrypt` below;
  * `userId` is stored as a BSON ObjectId (the users collection `_id`), so we write one.
"""

import json
import os

# The custom endpoint's `name` in bundle/librechat/librechat.yaml. LibreChat looks the
# per-user key up under exactly this string. If the endpoint is renamed there, rename it here
# (or set CHAT_ENDPOINT_NAME), or every seeded key becomes unfindable and chat falls back to
# prompting the user for one — the UX this module exists to avoid.
_ENDPOINT_NAME = os.environ.get("CHAT_ENDPOINT_NAME", "Enterprise AI")

_MONGO_URL = os.environ.get("CHAT_MONGO_URL", "")
_MONGO_DB = os.environ.get("CHAT_MONGO_DB", "librechat")
_CREDS_KEY = os.environ.get("CHAT_CREDS_KEY", "")
_CREDS_IV = os.environ.get("CHAT_CREDS_IV", "")


def configured() -> bool:
    """True only when we can actually seed: Mongo reachable-in-principle and creds present.

    Callers treat False as "chat is not on the per-user-key path here" and skip seeding
    rather than failing provisioning — the same degrade-don't-break posture as chat_identity.
    """
    return bool(_MONGO_URL and _CREDS_KEY and _CREDS_IV)


def _encrypt(plaintext: str, *, creds_key: str | None = None, creds_iv: str | None = None) -> str:
    """LibreChat v1 credential encryption, reproduced byte-for-byte.

    AES-256-CBC with the fixed CREDS_IV and PKCS7 padding, hex output. `creds_key`/`creds_iv`
    override the env for the golden-vector test; production reads the env. Matches
    `@librechat/data-schemas` `encrypt` — proven by `test_chat_keyseed`'s golden vector, which
    was produced by that function itself.
    """
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = bytes.fromhex(creds_key if creds_key is not None else _CREDS_KEY)
    iv = bytes.fromhex(creds_iv if creds_iv is not None else _CREDS_IV)
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex()


def encrypt_api_key(api_key: str) -> str:
    """The exact `value` LibreChat stores for a user_provided custom-endpoint key.

    Compact separators match JS `JSON.stringify` byte-for-byte, so the stored plaintext is
    identical to what LibreChat produces itself (LibreChat only `JSON.parse`s it, so spacing
    would not break reading — but matching exactly is what lets the golden vector pin it)."""
    return _encrypt(json.dumps({"apiKey": api_key}, separators=(",", ":")))


def _client():
    """A Mongo client, or None if unconfigured/unavailable. Short timeouts: a provisioning
    path must not hang on the chat database."""
    if not _MONGO_URL:
        return None
    try:
        from pymongo import MongoClient
    except ImportError:
        return None
    try:
        return MongoClient(
            _MONGO_URL,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000,
            socketTimeoutMS=3000,
        )
    except Exception:
        return None


def seed_chat_key(mongo_id: str, api_key: str) -> bool:
    """Upsert the user_provided credential LibreChat reads for this user's chat requests.

    `mongo_id` is the LibreChat user's ObjectId (from chat_identity.ids_for). Idempotent per
    (user, endpoint): re-seeding replaces the value. Returns True on a confirmed write, False
    if unconfigured/unreachable — the caller decides whether that is fatal (a backfill) or a
    best-effort nicety (an incremental reconcile).
    """
    from bson import ObjectId

    client = _client()
    if client is None:
        return False
    try:
        oid = ObjectId(mongo_id)
        client[_MONGO_DB]["keys"].update_one(
            {"userId": oid, "name": _ENDPOINT_NAME},
            {
                "$set": {"userId": oid, "name": _ENDPOINT_NAME, "value": encrypt_api_key(api_key)},
                # No expiry — an operator-seeded key must not silently lapse and drop the user
                # back to the paste-a-key prompt.
                "$unset": {"expiresAt": ""},
            },
            upsert=True,
        )
        return True
    except Exception:
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def has_key(mongo_id: str) -> bool:
    """True if this user already has a seeded chat credential. Makes the reconcile idempotent:
    a user with a key is skipped, so a reconcile loop does not rotate live keys every pass."""
    from bson import ObjectId

    client = _client()
    if client is None:
        return False
    try:
        return client[_MONGO_DB]["keys"].find_one(
            {"userId": ObjectId(mongo_id), "name": _ENDPOINT_NAME}, {"_id": 1}
        ) is not None
    except Exception:
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def delete_chat_key(mongo_id: str) -> None:
    """Remove a user's seeded chat credential. Called on deprovision so a departed user's key
    does not outlive them in the chat store (mirrors the key revocation every other surface
    gets)."""
    from bson import ObjectId

    client = _client()
    if client is None:
        return
    try:
        client[_MONGO_DB]["keys"].delete_many({"userId": ObjectId(mongo_id), "name": _ENDPOINT_NAME})
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass
