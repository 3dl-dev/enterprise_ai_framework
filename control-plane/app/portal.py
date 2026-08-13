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

import hashlib
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import agent_usage, agents, chat_identity, db, gateway, issuance, metering

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


def _asset_version(name: str) -> str:
    """A short content hash used to cache-bust a static asset's URL.

    Changes exactly when the file's bytes change, so a deploy's new app.js/style.css reach
    an already-visited browser instead of being masked by the previous copy.
    """
    try:
        return hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:12]
    except OSError:
        return "0"


@router.get("/portal/", include_in_schema=False)
async def portal_index(user: str = Depends(require_user)):
    # Stamp the asset URLs with a content hash so the browser fetches the CURRENT app.js and
    # style.css after a deploy. FileResponse sends no Cache-Control, so a bare
    # `/portal/static/app.js` is served from heuristic cache after a redeploy — which is how
    # a freshly-deployed index.html (new Agents tab) ends up running the OLD app.js that has
    # no handler for it, and the tab does nothing. The hash in the query changes the URL, so
    # the cache misses and the new file loads. index.html itself is served no-store so these
    # hashed URLs are never themselves stale.
    html = (STATIC / "index.html").read_text()
    for asset in ("app.js", "style.css"):
        html = html.replace(
            f"/portal/static/{asset}",
            f"/portal/static/{asset}?v={_asset_version(asset)}",
        )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/portal/static/{name}", include_in_schema=False)
async def portal_static(request: Request, name: str, user: str = Depends(require_user)):
    # Resolve then verify containment, rather than inspecting the string.
    target = (STATIC / name).resolve()
    try:
        target.relative_to(STATIC.resolve())
    except ValueError:
        raise HTTPException(403, "denied")
    if not target.is_file():
        raise HTTPException(404, "not found")
    # A request that carries the content-hash query (?v=...) names an immutable byte-for-byte
    # version — safe to cache hard. A bare request (no version) must revalidate, so an old
    # bookmark or a hand-typed URL can never pin a stale asset.
    cache = ("public, max-age=31536000, immutable"
             if request.query_params.get("v") else "no-cache")
    return FileResponse(target, headers={"Cache-Control": cache})


# WHO IS AN OPERATOR
#
# An explicit list, not a Keycloak role, and not "whoever holds the admin token".
#
# The admin token is a shared static secret that no browser has (finding 11); it stays the
# only way to reach /admin/*. This list governs a strictly READ-ONLY view of the same
# numbers, so that seeing the bill does not require handing somebody the credential that
# can also revoke every key in the deployment.
#
# Empty by default. A deployment that forgets to set it gets no operator console, which is
# the safe direction to fail.
ADMINS = {a.strip() for a in os.environ.get("PORTAL_ADMINS", "").split(",") if a.strip()}


def require_admin_user(request: Request, user: str = Depends(require_user)) -> str:
    if user not in ADMINS:
        # 404 rather than 403: a non-operator has no business learning that an operator
        # console exists at this path.
        raise HTTPException(404, "not found")
    return user


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
        # So the page knows whether to ask for the operator console at all. Without it the
        # page fetches, gets the correct 404, and every camper's console fills with a
        # failed request — indistinguishable from a real one when reading logs.
        "is_admin": user in ADMINS,
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
    disagree — and filtered here rather than in SQL because the query names the principal
    (`chat_identity.attribute`) after the database has answered. A chat row is keyed by
    LibreChat's internal id, and filtering in SQL, before that translation, would
    silently drop every chat row from the user's own total.
    """
    mine, total = await _spend_by_surface(user, since)
    agents_rows, agents_error = await _agent_usage_beside_spend(user, mine)
    return {
        "username": user,
        "since": since,
        "by_surface": sorted(mine.values(), key=lambda x: -x["spend"]),
        "total": total,
        # THE SECOND DIMENSION, ADDED BESIDE THE FIRST AND NEVER FOLDED INTO IT
        # (enterpriseaiframework-914, Contract 3 of the agents-surface record). Every key
        # above is exactly what it was before this landed — the inference query is
        # untouched and `total` still totals inference spend only. What is new is a
        # sibling list carrying USAGE QUANTITIES per agent: hours resident, CPU-core-hours
        # burnt, peak megabytes. There are no dollars in it, by Baron's ruling; the
        # hardware is owned and its cost is sunk, so a rate here would be an invented
        # number. See app/agent_usage.py.
        "by_agent": agents_rows,
        # Present only when the usage ledger could not be read. An empty `by_agent` with
        # no error means there are no agents; an empty one WITH an error means we do not
        # know, and saying so beats rendering zero.
        **({"agents_usage_error": agents_error} if agents_error else {}),
    }


async def _spend_by_surface(user: str, since: str | None) -> tuple[dict, dict]:
    """One user's inference spend, keyed by surface, plus their total.

    Lifted verbatim out of `my_spend` when the Agents tab needed the same numbers per
    agent row. It is the same filter over the same query for the same reason recorded
    there — the principal is named by `chat_identity.attribute` AFTER the database has
    answered, so filtering in SQL would silently drop the caller's own chat rows.
    """
    rows = await metering.spend_by_user_and_surface(since)
    mine: dict[str, dict] = {}
    total = {"requests": 0, "spend": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    for r in rows:
        who = r.get("username") or ""
        if who != user:
            continue
        surface = r.get("surface") or "(unknown)"
        acc = mine.setdefault(surface, {"surface": surface, "requests": 0, "spend": 0.0,
                                        "prompt_tokens": 0, "completion_tokens": 0})
        for k in ("requests", "spend", "prompt_tokens", "completion_tokens"):
            acc[k] += r.get(k) or 0
            total[k] += r.get(k) or 0
    return mine, total


async def _agent_usage_beside_spend(user: str, by_surface: dict) -> tuple[list[dict], str]:
    """Line each agent's USAGE up against that same agent's inference SPEND.

    The two dimensions are joined here, in the endpoint, and deliberately not in SQL: the
    inference numbers come out of the gateway's database and the usage numbers out of the
    control plane's, and a join across them would mean one query owning both — which is
    exactly how a change to the usage meter would come to move the operator's bill.

    The join key is Contract 1's per-instance surface, `agents/<name>`, which the alias
    grammar already puts on the spend row for free.

    A BYO agent is reported with `inference.on_ledger: false` rather than as a $0 row. A
    silent zero reads as "free" or "broken"; the label says "this agent's inference does
    not traverse our layer, by declaration", which is the whole basis on which Contract 4
    permits BYO at all.
    """
    try:
        rows = await agent_usage.usage_by_agent(user)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never swallowed
        return [], f"{type(exc).__name__}: {exc}"

    out = []
    for row in rows:
        spend_row = by_surface.get(row["surface"])
        integrated = row.get("model_source") != "byo"
        out.append({
            "agent": row["agent"],
            "surface": row["surface"],
            "model_source": row.get("model_source") or "",
            "running": row["running"],
            # Quantities. Named `usage` and not `cost` because that is what they are.
            "usage": {
                k: row[k] for k in (
                    "resident_seconds", "resident_hours",
                    "cpu_core_seconds", "cpu_core_hours",
                    "memory_peak_bytes", "memory_peak_mb",
                    "compute_source", "compute_measured",
                )
            },
            "inference": {
                "on_ledger": integrated,
                "requests": (spend_row or {}).get("requests", 0),
                "spend": (spend_row or {}).get("spend", 0.0),
                "prompt_tokens": (spend_row or {}).get("prompt_tokens", 0),
                "completion_tokens": (spend_row or {}).get("completion_tokens", 0),
                **({} if integrated else {
                    "note": "off-ledger by design — this agent uses the user's own "
                            "provider credential and its inference never traverses "
                            "this layer",
                }),
            },
        })
    return out, ""


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


# ---------------------------------------------------------------- agents
#
# THE THIRD SURFACE, AND THE FIRST ONE A USER CAN CREATE INFRASTRUCTURE WITH.
#
# Chat and Code are surfaces a user is GIVEN: an operator provisions the workspace and the
# portal points at it. An agent is a surface a user MAKES — a Deployment, a Service, a
# PersistentVolumeClaim and a Secret, applied to a live namespace by a control plane that
# holds namespaced write authority to do it (deploy/k8s/39-control-plane-rbac.yaml).
#
# So these four endpoints are the point at which "one control plane" starts to mean real
# privilege delegated on a user's behalf, and there is exactly one thing keeping one
# camper out of another camper's agent: OWNER IS ALWAYS `require_user()`. It is never a
# body field, never a query parameter, never a path segment. That is the same rule
# `/portal/api/keys/rotate` states above, and here it is enforced twice — the object name
# is DERIVED from the authenticated name, and then the object's own owner label is checked
# against it, because two different (user, name) pairs can derive the same object name
# when either half contains a hyphen. See app/agents.py for that collision written out.


@router.get("/portal/api/agents")
async def my_agents(user: str = Depends(require_user)):
    """The caller's own agents, each with BOTH metering dimensions beside its status.

    The two dimensions are the ones Contract 3 names and they are kept apart here exactly
    as `/portal/api/spend` keeps them apart: `inference` is dollars off the gateway ledger,
    `usage` is quantities — hours, CPU-core-hours, megabytes — off the -914 resident meter,
    and neither is folded into the other. A stopped agent shows frozen usage and unchanged
    spend, which is the whole observable claim of Contract 2's scale-to-zero.
    """
    rows = await agents.list_agents(user)

    spend, _ = await _spend_by_surface(user, None)
    try:
        usage = {u["agent"]: u for u in await agent_usage.usage_by_agent(user)}
        usage_error = ""
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never swallowed
        usage, usage_error = {}, f"{type(exc).__name__}: {exc}"

    out = []
    for row in rows:
        spend_row = spend.get(row["surface"])
        integrated = row.get("model_source") != "byo"
        metered = usage.get(row["name"])
        out.append({
            **row,
            "usage": {k: metered[k] for k in (
                "resident_seconds", "resident_hours", "cpu_core_seconds",
                "cpu_core_hours", "memory_peak_bytes", "memory_peak_mb",
                "compute_source", "compute_measured",
            )} if metered else None,
            "inference": {
                # A BYO agent is labelled off-ledger rather than rendered as $0: a silent
                # zero reads as "free" or "broken", and finding 4's leak was exactly an
                # unmetered path that rendered as healthy.
                "on_ledger": integrated,
                "requests": (spend_row or {}).get("requests", 0),
                "spend": (spend_row or {}).get("spend", 0.0),
                "prompt_tokens": (spend_row or {}).get("prompt_tokens", 0),
                "completion_tokens": (spend_row or {}).get("completion_tokens", 0),
            },
        })
    return {
        "username": user,
        "agents": out,
        "models": list(agents.allowed_models()),
        # The Agents-pillar type dimension (Contract A) the wizard offers, and its default,
        # kept beside `models` so the create form is built entirely from this one response.
        "types": list(agents.AGENT_TYPES),
        "default_type": agents.DEFAULT_AGENT_TYPE,
        **({"usage_error": usage_error} if usage_error else {}),
    }


@router.post("/portal/api/agents", status_code=201)
async def create_my_agent(body: dict, user: str = Depends(require_user)):
    """Create one agent for the caller. The name is theirs to choose; the owner is not."""
    name = ((body or {}).get("name") or "").strip()
    model = ((body or {}).get("model") or "").strip() or None
    # Contract A's type. None → agents.create defaults it to the incumbent `hermes`; an
    # unknown value is refused there, not here, so the one allowlist lives with the render.
    agent_type = ((body or {}).get("type") or "").strip() or None
    return await agents.create(user, name, model=model, agent_type=agent_type)


@router.post("/portal/api/agents/{name}/stop")
async def stop_my_agent(name: str, user: str = Depends(require_user)):
    """`replicas: 0`. The PVC is kept, the session is on it, and the meter freezes."""
    return await agents.scale(user, name, 0)


@router.post("/portal/api/agents/{name}/start")
async def start_my_agent(name: str, user: str = Depends(require_user)):
    """`replicas: 1`. The SAME agent resumes from the same volume, not a new one.

    Contract 2 lists `stopped -> running` as a transition, so it is here. Without it a
    stopped agent could only be revived with a kubeconfig, which would make Stop a
    one-way door dressed up as a pause.
    """
    return await agents.scale(user, name, 1)


@router.post("/portal/api/agents/{name}/connectors")
async def configure_my_agent_connector(name: str, body: dict,
                                       user: str = Depends(require_user)):
    """Wire the caller's OWN agent to their Slack, their Discord or their mailbox.

    THE LAST OPERATOR-ONLY STEP IN THIS SURFACE. -627 let a user create, stop and delete
    an agent from the browser, but the thing that makes a resident agent useful — a chat
    workspace it answers in, a mailbox it reads — could only be supplied by somebody with
    a kubeconfig running `deploy/bin/provision-agent.sh --slack-config-file`. So "the
    user stands one up themselves" was true right up to the point it mattered.

    The credential is the USER'S OWN — their tenant's Slack app, their bot token — which
    is the same shape as every other credential this platform handles: the customer's
    layer talking to the customer's providers with the customer's credentials.

    Set-once and never returned. There is no GET beside this that answers with a
    credential; `/portal/api/agents` reports only WHETHER each connector is configured,
    as a boolean read off the pod template. The body is
    `{"kind": "slack"|"discord"|"email", "values": {...}}`, and for callers that send the
    credential flat beside `kind` — which is how the item specified it and how a small
    form naturally serialises — the remaining top-level keys are taken as the values.
    Nothing is guessed beyond that: every key is checked against `agents.CONNECTORS`.
    """
    body = body or {}
    # Taken exactly as documented, never normalised. `kind` selects a schema and a Secret
    # name; a lower-casing shim here is one more place the three connector names are
    # spelled, and the refusal already lists the ones this surface carries.
    kind = body.get("kind")
    values = body.get("values")
    if values is None:
        values = {k: v for k, v in body.items() if k != "kind"}
    return await agents.configure_connector(user, name, kind, values)


@router.delete("/portal/api/agents/{name}")
async def delete_my_agent(name: str, user: str = Depends(require_user)):
    """The irreversible one: every object, then the volume, then the virtual key."""
    return await agents.delete(user, name)


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


# ---------------------------------------------------------------- operator

@router.get("/portal/api/admin/overview")
async def admin_overview(since: str | None = None,
                         admin: str = Depends(require_admin_user)):
    """Everyone's usage, in one call.

    The same query that produces a user's own bill, unfiltered — so the operator view and
    the personal view can never disagree about what somebody spent. Principals are named
    by the query itself (`chat_identity.attribute`); this page deliberately does not name
    them a second time, because a second copy of that step is precisely what let this view
    show names while `/admin/spend` showed hex (finding 34).
    """
    rows = await metering.spend_by_user_and_surface(since)
    people: dict[str, dict] = {}
    for r in rows:
        who = r.get("username") or chat_identity.UNATTRIBUTED
        acc = people.setdefault(who, {
            "username": who, "requests": 0, "spend": 0.0,
            "prompt_tokens": 0, "completion_tokens": 0, "surfaces": {},
        })
        surface = r.get("surface") or "(unknown)"
        s = acc["surfaces"].setdefault(surface, {"requests": 0, "spend": 0.0})
        for k in ("requests", "spend", "prompt_tokens", "completion_tokens"):
            acc[k] += r.get(k) or 0
        s["requests"] += r.get("requests") or 0
        s["spend"] += r.get("spend") or 0.0

    keys = await gateway.list_keys()
    budgets: dict[str, dict] = {}
    for k in keys:
        alias = k.get("key_alias") or ""
        owner, _, surface = alias.partition("::")
        # `chat-surface` is a shared surface key, not a person. It has to be skipped here
        # as well as in the spend loop, or it reappears as a row with no traffic — a name
        # in a list of people that is not one.
        if not owner or owner == "chat-surface":
            continue
        budgets.setdefault(owner, {})[surface] = {
            "max_budget": k.get("max_budget"), "spend": k.get("spend"),
        }

    # Somebody with a key and no traffic still belongs on this page — "who exists" and
    # "who spent" are different questions, and an operator needs the first one too.
    for owner in budgets:
        people.setdefault(owner, {"username": owner, "requests": 0, "spend": 0.0,
                                  "prompt_tokens": 0, "completion_tokens": 0, "surfaces": {}})

    for name, person in people.items():
        person["budgets"] = budgets.get(name, {})
        person["surfaces"] = [
            {"surface": s, **v} for s, v in sorted(person["surfaces"].items())
        ]

    unpriced = await metering.unpriced_models(since)
    # The resident dimension for everybody, beside the inference dimension for everybody.
    # A separate key rather than a field folded into `people`, for the same reason
    # `by_agent` is a sibling of `by_surface` above: `people[*].spend` means inference
    # spend and must go on meaning exactly that.
    try:
        agent_usage_rows = await agent_usage.usage_by_agent()
        agent_usage_error = ""
    except Exception as exc:  # noqa: BLE001
        agent_usage_rows, agent_usage_error = [], f"{type(exc).__name__}: {exc}"
    return {
        "since": since,
        "totals": await metering.totals(since),
        "people": sorted(people.values(), key=lambda p: -p["spend"]),
        # Usage quantities per agent — hours, CPU-core-hours, MB. No dollars: Baron's
        # ruling is that owned compute is metered, not priced (enterpriseaiframework-914).
        "agent_usage": agent_usage_rows,
        **({"agent_usage_error": agent_usage_error} if agent_usage_error else {}),
        # Travels with the number, not on a page nobody opens: a bill quietly missing a
        # model is worse than one that is obviously incomplete.
        "unpriced_models": unpriced,
        "viewer": admin,
    }


@router.get("/portal/api/admin/audit")
async def admin_audit(limit: int = 50, admin: str = Depends(require_admin_user)):
    """The tail of the audit trail, plus whether the chain still verifies."""
    limit = max(1, min(limit, 200))
    pool = await db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT seq, ts, actor, action, target FROM audit_event "
            "ORDER BY seq DESC LIMIT $1",
            limit,
        )
    # `detail` and `hash` are deliberately not selected. This is a read-only window for an
    # operator, not the export — /admin/export/audit is the thing that produces evidence,
    # and it still needs the admin token.
    return {
        "verified": await db.verify_chain(),
        "entries": [
            {"seq": r["seq"], "ts": str(r["ts"]), "actor": r["actor"],
             "action": r["action"], "target": r["target"]}
            for r in rows
        ],
    }
