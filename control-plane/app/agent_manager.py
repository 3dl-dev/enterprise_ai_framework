"""The agent-manager API: `/agent-manager/v1/*` (agents-raven.md, Contract F).

A Raven drives its OWNER's agents through here, holding a token bound to one owner and one
Raven (`agent_manager_token`). This file is only the door:

  * `require_agent_manager` resolves (owner, raven) from the token record — never from a
    path, body or header — and refuses loopback-origin requests outright;
  * every route calls the SAME `agents.*` function the portal calls, with `user=owner` and
    `via_raven=<its raven>`. Owner scoping is `agents._owned_deployment`, and the
    Raven-specific refusals (no raven creation, no self-target, delete only its own
    created-by children) are in `agents.py` too. Nothing here decides who may do what;
  * every call, allowed or refused, is audited with actor `agent-manager:<owner>/<raven>`.

Scope is the router: it mounts list/create/start/stop/model/delete and the agent relay
(Contract G, `agent_relay`) and nothing else. No
connector, key, BYO, admin or portal route exists here, so none can be reached.

WHY LOOPBACK IS REFUSED

oauth2-proxy upstreams EVERY path to 127.0.0.1:8000 (deploy/k8s/40-control-plane.yaml), so
`/agent-manager/*` is reachable through the portal's public entry by any signed-in person,
and all of those requests arrive from loopback. A person holding an exfiltrated Raven token
would otherwise drive its owner's agents from a browser. The portal accepts only loopback;
this refuses it; no request satisfies both (`portal.is_loopback` is the one predicate).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import agent_manager_token, agent_relay, agents, db, portal

router = APIRouter(prefix="/agent-manager/v1")

_bearer = HTTPBearer(auto_error=False)

UNKNOWN_ACTOR = "agent-manager:unknown"


@dataclass(frozen=True)
class Manager:
    owner: str
    raven: str
    # SHA-256 of the presented bearer: the key of the relay's per-token stream cap. The
    # plaintext never leaves require_agent_manager.
    token_hash: str = ""

    @property
    def actor(self) -> str:
        return agents.manager_actor(self.owner, self.raven)


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


async def _deny_unresolved(request: Request, status: int, reason: str, token: str,
                           detail: str) -> HTTPException:
    await db.audit(
        UNKNOWN_ACTOR, "agent-manager.denied", None,
        status=status, reason=reason, peer=_peer(request),
        method=request.method, path=request.url.path,
        hash_prefix=agent_manager_token.token_hash(token)[:12] if token else "",
    )
    return HTTPException(status, detail)


async def require_agent_manager(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Manager:
    """(owner, raven) for a live agent-manager token presented from a pod IP, or 403/401.

    Not cached: every request reads the table, so a revocation is immediate.
    """
    token = creds.credentials if creds is not None else ""
    # 0. Origin, BEFORE the bearer: the inverse of portal.require_user.
    if portal.is_loopback(request):
        raise await _deny_unresolved(
            request, 403, "loopback_origin", token,
            "the agent-manager API is not reachable through the portal's proxy",
        )
    # 1. The bearer's hash, live rows only. Unknown, revoked and disabled-owner are one 401.
    row = await agent_manager_token.lookup_live(token) if token else None
    if row is None or not row.get("owner_enabled"):
        raise await _deny_unresolved(
            request, 401,
            "no_token" if not token else ("unknown_or_revoked" if row is None
                                          else "owner_disabled"),
            token, "invalid or revoked agent-manager token",
        )
    mgr = Manager(row["owner"], row["raven_name"], agent_manager_token.token_hash(token))
    # 2. The Raven must still exist, be the owner's, and be a raven. A token whose Raven
    #    was deleted underneath it is dead even if revocation somehow failed.
    try:
        await agents.owned_raven(mgr.owner, mgr.raven)
    except HTTPException:
        await db.audit(mgr.actor, "agent-manager.denied", f"{mgr.owner}/{mgr.raven}",
                       status=401, reason="raven_gone", peer=_peer(request),
                       method=request.method, path=request.url.path,
                       owner=mgr.owner, raven=mgr.raven)
        raise HTTPException(401, "invalid or revoked agent-manager token")
    await agent_manager_token.touch(token)
    return mgr


async def _guarded(mgr: Manager, request: Request, action: str, target: str | None, call):
    """Run one agents.* call; audit a refusal as `agent-manager.denied` and re-raise it.

    Successful calls are audited by `agents.*` itself (actor = mgr.actor), so a lifecycle
    event has exactly one row whoever acted.
    """
    try:
        return await call()
    except HTTPException as exc:
        await db.audit(
            mgr.actor, "agent-manager.denied",
            f"{mgr.owner}/{target}" if target else mgr.owner,
            status=exc.status_code, attempted=action, peer=_peer(request),
            owner=mgr.owner, raven=mgr.raven, reason=str(exc.detail)[:300],
        )
        raise


@router.get("/agents")
async def list_owner_agents(request: Request,
                            mgr: Manager = Depends(require_agent_manager)):
    rows = await agents.list_agents(mgr.owner)
    await db.audit(mgr.actor, "agent-manager.list", mgr.owner,
                   via="agent-manager", owner=mgr.owner, raven=mgr.raven, count=len(rows))
    return {
        "agents": rows,
        "models": list(agents.allowed_models()),
        "types": list(agents.MANAGER_CREATABLE_TYPES),
    }


@router.post("/agents", status_code=201)
async def create_agent(body: dict, request: Request,
                       mgr: Manager = Depends(require_agent_manager)):
    body = body or {}
    name = (body.get("name") or "").strip()
    model = (body.get("model") or "").strip() or None
    agent_type = (body.get("type") or "").strip() or None
    return await _guarded(
        mgr, request, "agent.create", name or None,
        lambda: agents.create(mgr.owner, name, model=model, agent_type=agent_type,
                              via_raven=mgr.raven),
    )


@router.post("/agents/{name}/start")
async def start_agent(name: str, request: Request,
                      mgr: Manager = Depends(require_agent_manager)):
    return await _guarded(mgr, request, "agent.start", name,
                          lambda: agents.scale(mgr.owner, name, 1, via_raven=mgr.raven))


@router.post("/agents/{name}/stop")
async def stop_agent(name: str, request: Request,
                     mgr: Manager = Depends(require_agent_manager)):
    return await _guarded(mgr, request, "agent.stop", name,
                          lambda: agents.scale(mgr.owner, name, 0, via_raven=mgr.raven))


@router.post("/agents/{name}/model")
async def set_agent_model(name: str, body: dict, request: Request,
                          mgr: Manager = Depends(require_agent_manager)):
    model = ((body or {}).get("model") or "").strip()
    return await _guarded(mgr, request, "agent.model.set", name,
                          lambda: agents.set_model(mgr.owner, name, model,
                                                   via_raven=mgr.raven))


@router.delete("/agents/{name}")
async def delete_agent(name: str, request: Request,
                       mgr: Manager = Depends(require_agent_manager)):
    return await _guarded(mgr, request, "agent.delete", name,
                          lambda: agents.delete(mgr.owner, name, via_raven=mgr.raven))


@router.post("/agents/{name}/relay/v1/chat/completions")
async def relay_chat_completions(name: str, request: Request,
                                 mgr: Manager = Depends(require_agent_manager)):
    """Contract G: one turn from this Raven to its owner's named hermes agent.

    The per-token stream cap is checked first (it guards connections, so it must not wait on
    anything); then the target is resolved by `agents.relay_target` behind the same owner
    guard as every other verb, and a refusal there is audited as `agent-manager.denied`
    exactly like theirs. Everything after that is `agent_relay`: timeouts, disconnect
    cancel, header allow-listing, the derived session key and the per-turn audit row.
    """
    turn = await agent_relay.open_turn(mgr, name, request)
    try:
        target = await _guarded(mgr, request, "agent.relay", name,
                                lambda: agents.relay_target(mgr.owner, name, mgr.raven))
    except BaseException:
        turn.abandon()
        raise
    return await agent_relay.relay(turn, target, request)
