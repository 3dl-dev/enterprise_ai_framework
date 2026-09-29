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

import logging
import os
import time

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agents, gateway, voice
from .portal import require_user

router = APIRouter()

TOKEN_TTL_SECONDS = 600
ROOM_PREFIX = "voice-"
# A voice room closes this many seconds after its last participant leaves.
EMPTY_TIMEOUT_SECONDS = 10
DEPARTURE_TIMEOUT_SECONDS = 10
AGENT_IDENTITY_PREFIX = "agent-"  # LiveKit's own naming for a dispatched agent's participant
_log = logging.getLogger("livekit")


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


def api_url() -> str:
    """Where the CONTROL PLANE reaches the LiveKit server (Twirp API): the in-cluster Service,
    not the browser-facing LIVEKIT_URL (which is the LAN/VPN signalling address)."""
    return (os.environ.get("LIVEKIT_API_URL") or "http://livekit:7880").rstrip("/")


def _admin_token(api_key: str, api_secret: str, room: str) -> str:
    now = int(time.time())
    return jwt.encode({
        "iss": api_key, "sub": "eaf-control-plane", "nbf": now, "exp": now + 60,
        "video": {"roomCreate": True, "roomAdmin": True, "room": room},
    }, api_secret, algorithm="HS256")


async def ensure_room_and_agent(api_key: str, api_secret: str, room: str, agent_name: str,
                                metadata: str) -> bool:
    """Make sure `room` exists and has the voice worker dispatched to it. Returns whether a new
    dispatch was made.

    WHY A SERVER-SIDE DISPATCH AND NOT THE TOKEN'S `roomConfig`. The token's dispatch is applied
    only when the join CREATES the room. A user who hangs up and presses Talk again inside the
    room's empty timeout rejoins the room that still exists, and no worker is dispatched:
    silence (measured against the real server, see tests-live/test_voice_live.py). Creating the
    dispatch here works whether or not the room is there. CreateRoom is idempotent, and it is
    what fixes the short empty/departure timeouts, so the room still closes seconds after the
    user leaves. If an agent is already in the room (a second tab) no second one is sent.

    The control plane holds the API secret already (it signs the join token); this is the same
    authority used for two more calls. No LiveKit SDK: the API is Twirp over JSON, and nothing
    here can reach LiveKit Cloud.
    """
    headers = {"Authorization": f"Bearer {_admin_token(api_key, api_secret, room)}"}

    async def call(client: httpx.AsyncClient, service: str, method: str, body: dict) -> dict:
        r = await client.post(f"{api_url()}/twirp/livekit.{service}/{method}", json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"{service}/{method} -> {r.status_code} {r.text[:200]}")
        return r.json()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await call(client, "RoomService", "CreateRoom", {
                "name": room, "empty_timeout": EMPTY_TIMEOUT_SECONDS,
                "departure_timeout": DEPARTURE_TIMEOUT_SECONDS})
            present = await call(client, "RoomService", "ListParticipants", {"room": room})
            if any(str(p.get("identity", "")).startswith(AGENT_IDENTITY_PREFIX)
                   for p in present.get("participants", [])):
                return False
            await call(client, "AgentDispatchService", "CreateDispatch", {
                "agent_name": agent_name, "room": room, "metadata": metadata})
            return True
    except (httpx.HTTPError, RuntimeError) as exc:
        _log.warning("could not prepare voice room %s: %s: %s", room, type(exc).__name__, exc)
        raise HTTPException(502, "the voice server is unreachable, so no agent could be sent "
                                 "to your room. Try again shortly.") from exc


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
    # The raven must exist and be the caller's (404 for both, as the console does) and must be a
    # raven: 7f6 minted a token for any slug, which would have opened a room nobody serves.
    async with agents._client() as client:
        dep = await agents._owned_deployment(client, user, body.raven)
    if ((dep.get("metadata") or {}).get("labels") or {}).get(agents.TYPE_LABEL) != "raven":
        raise HTTPException(400, f"agent {body.raven!r} is not a raven")
    voice_id, entry = voice.resolve(voice._pinned(dep))
    session = voice.mint_session(user, body.raven, room)
    await ensure_room_and_agent(api_key, api_secret, room, voice.WORKER_AGENT_NAME,
                                voice.dispatch_metadata(session, entry))
    return {
        "url": url,
        "room": room,
        "identity": user,
        "voice": voice_id,
        "token": mint(api_key, api_secret, identity=user, room=room),
        "expires_in": TOKEN_TTL_SECONDS,
    }
