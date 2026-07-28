"""The portal: one front door for the person using the platform.

WHY IT LIVES INSIDE THE CONTROL PLANE

The standing constraint is that twelve contracts must never become twelve consoles. The
surfaces had drifted into four separate front doors — chat on one origin, a workspace on
an IP and a port, published work on a third path, and Keycloak's account console on a
fourth that nothing linked to. Adding a *separate* portal service would have made five.

So the portal is the face of the thing that already administers all of it. The control
plane knows identity, keys, budgets and spend; it needed a page, not a sibling.

HOW A USER IS IDENTIFIED, AND WHY IT IS SAFE

An oauth2-proxy sidecar in this pod authenticates against Keycloak and forwards the user
in `X-Forwarded-User` / `X-Auth-Request-Preferred-Username`. Those headers are trivially
forgeable by anything that can reach this service directly, and this service is reachable
by other pods in the namespace.

The discriminator is the source address. The sidecar shares this pod's network namespace,
so its requests arrive from 127.0.0.1. A request routed to the Service from any other pod
arrives from that pod's own address. Identity headers are therefore honoured ONLY from
loopback and ignored everywhere else — the same reasoning that puts ttyd on `--interface
lo` in the workspace pods.

`/admin/*` is unaffected and still requires the shared admin token, so authenticating at
the portal does not confer any operator capability. Authentication is not authorisation.
"""

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import chat_identity, gateway, issuance, metering

router = APIRouter()

STATIC = Path(__file__).resolve().parent / "portal_static"

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
PUBLISHED_URL = os.environ.get("PUBLISHED_INTERNAL_URL", "http://published")
IDP_URL = os.environ.get("IDP_PUBLIC_URL", "")
IDP_REALM = os.environ.get("IDP_REALM", "enterprise-ai")



def require_user(request: Request) -> str:
    """The signed-in username, or 401. See the module docstring for why loopback matters."""
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1"):
        # Not from the sidecar. Whatever identity headers this carries were written by
        # something that is not our authenticator, so they mean nothing.
        raise HTTPException(
            403,
            "the portal is only reachable through its authenticating proxy",
        )
    user = (
        request.headers.get("x-auth-request-preferred-username")
        or request.headers.get("x-forwarded-preferred-username")
        or request.headers.get("x-forwarded-user")
        or ""
    ).strip()
    if not user:
        raise HTTPException(401, "not signed in")
    return user


# ---------------------------------------------------------------- the page

@router.get("/portal", include_in_schema=False)
async def portal_root():
    # Trailing slash, so the page's relative asset URLs resolve under /portal/ rather
    # than against the origin root, where nothing serves them.
    return RedirectResponse("/portal/", status_code=307)


@router.get("/portal/", include_in_schema=False)
async def portal_index(user: str = Depends(require_user)):
    return FileResponse(STATIC / "index.html")


@router.get("/portal/static/{name}", include_in_schema=False)
async def portal_static(name: str, user: str = Depends(require_user)):
    # Resolve then verify containment, rather than inspecting the string.
    target = (STATIC / name).resolve()
    try:
        target.relative_to(STATIC.resolve())
    except ValueError:
        raise HTTPException(403, "denied")
    if not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(target)


# ---------------------------------------------------------------- me

@router.get("/portal/api/me")
async def me(request: Request, user: str = Depends(require_user)):
    """Everything the portal needs to render, scoped to the caller and nobody else."""
    email = (request.headers.get("x-auth-request-email")
             or request.headers.get("x-forwarded-email") or "")

    account_url = (
        f"{PUBLIC_BASE_URL}/realms/{IDP_REALM}/account" if PUBLIC_BASE_URL else ""
    )
    return {
        "username": user,
        "email": email,
        "links": {
            "chat": PUBLIC_BASE_URL or "/",
            "workspace": await _workspace_url(user),
            "published": f"{PUBLIC_BASE_URL}/live/{user}/" if PUBLIC_BASE_URL else "",
            # Keycloak's own console. It has existed and worked all along; nothing
            # linked to it, which for a user is the same as it not existing.
            "account": account_url,
            "password": f"{account_url}#/security/signingin" if account_url else "",
            "signout": "/portal/oauth2/sign_out",
        },
    }


async def _workspace_url(user: str) -> str:
    """This user's workspace, on this origin.

    Always the same path for everybody. It used to be a per-user NodePort on a LAN
    address, which meant a hand-maintained map here, a link that only worked from one
    network, and — for anyone else — a page that hung rather than failed. The proxy picks
    the pod from the authenticated name, so there is nothing per-user in the URL.
    """
    return "/workshop/"


@router.get("/portal/api/spend")
async def my_spend(since: str | None = None, user: str = Depends(require_user)):
    """This user's own spend, by surface.

    Built on the same query that produces the operator's bill, so the two can never
    disagree — and filtered here rather than in SQL so that the chat surface's identifiers
    get translated first. A chat row is keyed by LibreChat's internal id, and filtering
    before translation would silently drop every chat row from the user's own total.
    """
    rows = await metering.spend_by_user_and_surface(since)
    mine: dict[str, dict] = {}
    total = {"requests": 0, "spend": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    for r in rows:
        who = chat_identity.resolve(r.get("username") or "")
        if who != user:
            continue
        surface = r.get("surface") or "(unknown)"
        acc = mine.setdefault(surface, {"surface": surface, "requests": 0, "spend": 0.0,
                                        "prompt_tokens": 0, "completion_tokens": 0})
        for k in ("requests", "spend", "prompt_tokens", "completion_tokens"):
            acc[k] += r.get(k) or 0
            total[k] += r.get(k) or 0
    return {
        "username": user,
        "since": since,
        "by_surface": sorted(mine.values(), key=lambda x: -x["spend"]),
        "total": total,
    }


@router.get("/portal/api/keys")
async def my_keys(user: str = Depends(require_user)):
    """The caller's own virtual keys. Never the secret — only what it is and what it may spend."""
    keys = await gateway.list_keys()
    out = []
    for k in keys:
        alias = k.get("key_alias") or ""
        owner, _, surface = alias.partition("::")
        if owner != user:
            continue
        out.append({
            "alias": alias,
            "surface": surface or "(unknown)",
            "max_budget": k.get("max_budget"),
            "spend": k.get("spend"),
            "created_at": str(k.get("created_at") or ""),
        })
    return {"username": user, "keys": sorted(out, key=lambda x: x["surface"])}


@router.post("/portal/api/keys/rotate")
async def rotate_my_key(body: dict, user: str = Depends(require_user)):
    """Replace one of the caller's own keys and hand back the new secret exactly once.

    The surface is taken from the body but the OWNER never is — it is always the
    authenticated caller. That is the whole reason this endpoint can exist next to an
    admin API: there is no parameter here that can be pointed at somebody else.
    """
    surface = (body or {}).get("surface", "").strip()
    if surface not in gateway.SURFACES:
        raise HTTPException(400, f"unknown surface: {surface}")
    # actor is the user, principal is the user. Same call the operator API makes, so a
    # self-service rotation cannot skip the enabled check or leave the ledger's token
    # hash stale — the two failure modes that made this worth sharing rather than copying.
    issued = await issuance.issue(user, surface, actor=user)
    return {
        "alias": issued["key_alias"],
        # Shown once and never retrievable. The ledger stores the hash, not this.
        "key": issued["key"],
        "rotated": issued["rotated"],
        "note": "Copy it now — it is not shown again.",
    }


@router.get("/portal/api/published")
async def my_published(user: str = Depends(require_user)):
    """What this user has shared, read from the published volume's own listing."""
    items: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            resp = await c.get(f"{PUBLISHED_URL}/listing/{user}/")
        if resp.status_code == 200:
            for entry in resp.json():
                if entry.get("type") == "directory":
                    name = entry.get("name")
                    items.append({
                        "name": name,
                        "url": f"{PUBLIC_BASE_URL}/live/{user}/{name}/" if PUBLIC_BASE_URL else "",
                        "modified": entry.get("mtime", ""),
                    })
    except Exception:
        # Nothing published yet is the common case and reads as an empty list, not an
        # error. A published volume that is briefly unreachable should look the same to
        # the page rather than turning the whole portal into an error state.
        pass
    return {"username": user, "projects": sorted(items, key=lambda x: x["name"])}
