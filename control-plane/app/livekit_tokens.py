"""LiveKit room tokens, minted only for the signed-in user's own rooms.

WHY THIS IS A SECURITY BOUNDARY

The self-hosted LiveKit server (deploy/k8s/72-livekit.yaml) authorises nothing itself: any
holder of a token signed with the API secret may join the room the token names. So this
endpoint is the whole of room authorisation. The rule is one sentence: the room in a token
is derived from the identity `require_user()` established, never accepted from the caller.

ROOM NAMES ARE UNAMBIGUOUS ON PURPOSE

docs/design/records/agents-raven.md writes the room as `voice-<user>-<raven>`. Both halves
are slugs that may contain hyphens, so that form collides: user `a` with raven `b-x` and
user `a-b` with raven `x` would share `voice-a-b-x`, and a "does it start with my prefix"
check would hand one user the other's room. The separator here is `.`, which the slug
(gateway.AGENT_SLUG) cannot contain, so a room name parses back to exactly one
(user, raven). A caller may name the room it wants; it is honoured only if it is exactly
the room derived for the caller.

NO LIVEKIT CLOUD
The token is a plain HS256 JWT (LiveKit's documented access-token format) signed locally by
PyJWT (MIT, maintained). No livekit SDK is imported, so nothing here can reach LiveKit
Cloud/Inference. Signing is not hand-rolled: no digest primitives are imported here.
"""

import os
import time

import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import gateway
from .portal import require_user

router = APIRouter()

TOKEN_TTL_SECONDS = 600
ROOM_PREFIX = "voice-"


def room_name(user: str, raven: str) -> str:
    return f"{ROOM_PREFIX}{user}.{raven}"


def mint(api_key: str, api_secret: str, *, identity: str, room: str,
         ttl: int = TOKEN_TTL_SECONDS, now: float | None = None) -> str:
    """A LiveKit access token: HS256, iss=api key, sub=identity, `video` grant for one room."""
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": api_key,
        "sub": identity,
        "nbf": issued,
        "exp": issued + ttl,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    return jwt.encode(claims, api_secret, algorithm="HS256")


class TokenRequest(BaseModel):
    raven: str
    room: str | None = None


@router.post("/portal/api/voice/token")
async def voice_token(body: TokenRequest, user: str = Depends(require_user)):
    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    url = os.environ.get("LIVEKIT_URL", "")
    if not (api_key and api_secret and url):
        raise HTTPException(503, "voice is not configured on this deployment")
    if not gateway.AGENT_SLUG.match(body.raven or ""):
        raise HTTPException(400, f"raven must match {gateway.AGENT_SLUG.pattern}")
    if not gateway.AGENT_SLUG.match(user):
        raise HTTPException(400, f"the signed-in name {user!r} cannot form a room name")
    room = room_name(user, body.raven)
    if body.room is not None and body.room != room:
        # Somebody else's room, or a malformed one: the caller named a room it may not join.
        raise HTTPException(403, "you may only join your own rooms")
    return {
        "url": url,
        "room": room,
        "identity": user,
        "token": mint(api_key, api_secret, identity=user, room=room),
        "expires_in": TOKEN_TTL_SECONDS,
    }
