"""Who a chat spend row belongs to.

THE PROBLEM THIS SOLVES

Every other surface holds a per-user virtual key, so the alias on the spend row already
names the person: `baron::ide`. The chat surface cannot work that way — one deployment
serves everybody through one key — so it identifies the person by forwarding them in the
request's `user` field, which the gateway records as `end_user`.

What LibreChat forwards there is its OWN primary key: `6a67b18069dba4d1126fef44`. That is
a correct, stable identifier and a completely useless one to show a human. Without a
translation, the one bill reads as a column of hex for the surface most people use, and
"your spend" in the portal silently omits chat entirely — the worst kind of wrong, because
a missing number looks like a zero.

WHY IT READS ANOTHER COMPONENT'S DATABASE, WHICH IS NOT SOMETHING WE DO LIGHTLY

The mapping exists in exactly one place: LibreChat's Mongo. There is no API that exposes
it, and inventing a second source of truth for identity would be worse than reading the
first one. So this is a deliberate, narrow, READ-ONLY bridge over one collection and two
fields, isolated in this module so the coupling is visible rather than smeared through
the metering code.

It is also entirely optional. If Mongo is unreachable, unconfigured, or shaped
differently than expected, every function here returns empty and callers fall back to
showing the raw identifier. Losing a friendly name costs a label. Failing the bill or the
portal because a chat database hiccuped would cost the product.
"""

import os
import re

# A LibreChat user id as it appears in end_user: a 24-character hex ObjectId. Anything
# else in that column did not come from the chat surface and is not ours to translate.
_OBJECT_ID = re.compile(r"^[0-9a-f]{24}$")

_MONGO_URL = os.environ.get("CHAT_MONGO_URL", "")
_MONGO_DB = os.environ.get("CHAT_MONGO_DB", "librechat")

# Cached because this is called once per bill render and the mapping changes only when
# somebody signs in to chat for the first time. Cleared by refresh().
_cache: dict[str, str] = {}
_loaded = False


def looks_like_chat_id(value: str) -> bool:
    return bool(value) and bool(_OBJECT_ID.match(value))


def _connect():
    """Return a Mongo client, or None if we cannot or should not have one."""
    if not _MONGO_URL:
        return None
    try:
        from pymongo import MongoClient
    except ImportError:
        return None
    try:
        # Short timeouts on purpose: this is a nicety on a page that must render. It is
        # never worth making a user wait on it, let alone time out a request.
        return MongoClient(_MONGO_URL, serverSelectionTimeoutMS=1500,
                           connectTimeoutMS=1500, socketTimeoutMS=2000)
    except Exception:
        return None


def refresh() -> int:
    """Reload the id -> username map. Returns how many entries were loaded."""
    global _cache, _loaded
    client = _connect()
    if client is None:
        _loaded = True
        return 0
    try:
        users = client[_MONGO_DB]["users"].find({}, {"_id": 1, "username": 1, "email": 1})
        fresh: dict[str, str] = {}
        for u in users:
            name = (u.get("username") or "").strip()
            if not name:
                # Fall back to the local part of the email rather than showing hex. A
                # chat account created by OIDC always has one of the two.
                name = (u.get("email") or "").split("@")[0].strip()
            if name:
                fresh[str(u["_id"])] = name
        _cache = fresh
    except Exception:
        # Deliberately swallowed — see the module docstring. An unreachable chat database
        # must degrade to unfriendly labels, never to a failed bill.
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass
    _loaded = True
    return len(_cache)


def resolve(value: str) -> str:
    """Translate one end_user value to a username, or hand it back untouched."""
    if not looks_like_chat_id(value):
        return value
    if not _loaded:
        refresh()
    if value not in _cache:
        # A brand new chat account will miss the cache exactly once.
        refresh()
    return _cache.get(value, value)


def ids_for(username: str) -> list[str]:
    """Every chat id belonging to a username. Usually one; never assume exactly one."""
    if not _loaded:
        refresh()
    return [cid for cid, name in _cache.items() if name == username]
