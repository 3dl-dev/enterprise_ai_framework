"""Who a chat spend row belongs to.

THE PROBLEM THIS SOLVES

Every other surface holds a per-user virtual key, so the alias on the spend row already
names the person: `alice::ide`. The chat surface cannot work that way — one deployment
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


def resolve(value: str, *, refresh_on_miss: bool = True) -> str:
    """Translate one end_user value to a username, or hand it back untouched.

    `refresh_on_miss=False` is for callers translating many rows at once (the ledger
    export): re-reading Mongo per unknown id is one round trip per unresolvable row, and a
    departed tenant's export is nothing but unresolvable rows.
    """
    if not looks_like_chat_id(value):
        return value
    if not _loaded:
        refresh()
    if value not in _cache and refresh_on_miss:
        # A brand new chat account will miss the cache exactly once.
        refresh()
    return _cache.get(value, value)


def ids_for(username: str) -> list[str]:
    """Every chat id belonging to a username. Usually one; never assume exactly one."""
    if not _loaded:
        refresh()
    return [cid for cid, name in _cache.items() if name == username]


def all_users() -> list[tuple[str, str]]:
    """Every (mongo_id, username) chat account, freshly read.

    For the per-user-key reconcile (chat_keyseed), which must seed a key for anybody who has
    a chat account. Always refreshes rather than trusting the cache, because a user who signed
    in to chat seconds ago is exactly the one that still needs seeding — the cache existing for
    the bill's benefit would otherwise hide them for a whole render cycle.
    """
    refresh()
    return list(_cache.items())


# ---------------------------------------------------------------------------
# Naming a principal on the bill
#
# This lives here, and every rendering of the bill goes through it, because the defect it
# fixes was not a broken lookup — the lookup above was correct all along. It was that the
# translation had been applied at two call sites in the portal and nowhere else, so
# `/admin/spend` (which IS the query scope item 4 names) showed a column of hex while the
# web console showed names, over the same money (finding 34). Two renderings of one bill
# that disagree about the principal is the failure; a second copy of the lookup would
# have been a third thing to forget.
#
# So the rule is: nothing outside this module decides what a spend row's principal is
# called. `metering.spend_by_user_and_surface` applies `attribute()` to every row it
# returns, which means the CLI bill, the portal, the operator console and anything added
# later all inherit the same names by construction rather than by remembering.
# ---------------------------------------------------------------------------

# The SQL already emits this when a row has no alias and no trusted end user.
UNATTRIBUTED = "(unattributed)"

# The alias the shared chat key is minted under. It is a surface, not a person: rows land
# under it when the chat surface called the gateway without forwarding who it was for
# (titling a conversation, for instance). Listing it beside real names invites somebody to
# read it as one.
SHARED_SURFACE_PRINCIPAL = "chat-surface"
SHARED_SURFACE_LABEL = "(chat surface, no user)"

# A chat id we could not translate. It keeps the identifier — dropping the row would hide
# money that was definitely spent, and picking a name would be a guess — but it must not
# be mistaken for a person's name, which is exactly what a bare ObjectId in a username
# column looks like.
UNRESOLVED_PREFIX = "(unresolved chat user "


def is_unresolved(label: str) -> bool:
    """True for a principal this module could not translate. Drives the bill's warning."""
    return bool(label) and label.startswith(UNRESOLVED_PREFIX)


def principal_label(raw: str, *, refresh_on_miss: bool = True) -> str:
    """What one spend row's principal is called, everywhere.

    Idempotent: feeding a label back through returns it unchanged, so a caller that
    applies it twice cannot corrupt a name.
    """
    value = (raw or "").strip()
    if not value:
        return UNATTRIBUTED
    if value == SHARED_SURFACE_PRINCIPAL:
        return SHARED_SURFACE_LABEL
    if not looks_like_chat_id(value):
        # Already a username, or one of the labels above. Not ours to touch.
        return value
    name = resolve(value, refresh_on_miss=refresh_on_miss)
    if name != value:
        return name
    return f"{UNRESOLVED_PREFIX}{value})"


def _prime(values) -> None:
    """Load the map at most twice for a whole bill, rather than once per unknown row.

    `resolve` re-reads Mongo on a cache miss so that somebody who signed in to chat a
    second ago still gets a name. Left to itself that is one round trip per unresolvable
    id, and a bill with a column of them would wait on the chat database repeatedly.
    """
    ids = {v for v in values if looks_like_chat_id(v)}
    if not ids:
        return
    if not _loaded:
        refresh()
    if any(i not in _cache for i in ids):
        refresh()


_COUNTERS = ("requests", "spend", "prompt_tokens", "completion_tokens")


def attribute(rows: list[dict]) -> list[dict]:
    """Name the principal on every spend row, then re-merge rows that now agree.

    Translation can make two rows the same row: one person can hold more than one chat
    id, and a resolvable id and a plain username can both name `alice`. The query's
    contract is one row per (principal, surface), so merging here keeps that promise
    rather than emitting the same person twice and leaving each reader to add them up —
    which is how two renderings start disagreeing again.
    """
    _prime([(r.get("username") or "") for r in rows])
    merged: dict[tuple[str, str], dict] = {}
    for r in rows:
        row = dict(r)
        row["username"] = principal_label(row.get("username") or "", refresh_on_miss=False)
        row["surface"] = row.get("surface") or "(unknown)"
        key = (row["username"], row["surface"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            continue
        for col in _COUNTERS:
            if col in row or col in existing:
                existing[col] = (existing.get(col) or 0) + (row.get(col) or 0)
    return sorted(
        merged.values(),
        key=lambda r: (-(r.get("spend") or 0), r["username"], r["surface"]),
    )
