"""Identity provider (Keycloak) admin client.

Identity is the source of truth for who exists and who is enabled. The control plane
never keeps its own user list as an authority — it mirrors, and the mirror is
reconciled on every sync so a disable in the IdP propagates to every surface
(scope item 6).
"""

import os

import httpx


def base_url() -> str:
    return os.environ.get("IDP_URL", "http://identity:8080").rstrip("/")


def realm() -> str:
    return os.environ.get("IDP_REALM", "enterprise-ai")


async def _admin_token() -> str:
    """Service-account token for the control plane's own confidential client."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{base_url()}/realms/{realm()}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ.get("IDP_CLIENT_ID", "control-plane"),
                "client_secret": os.environ["IDP_CLIENT_SECRET"],
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def list_users() -> list[dict]:
    """Every user in the realm, with the enabled flag that drives revocation."""
    token = await _admin_token()
    users: list[dict] = []
    first, page_size = 0, 100
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                f"{base_url()}/admin/realms/{realm()}/users",
                headers={"Authorization": f"Bearer {token}"},
                params={"first": first, "max": page_size},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            users.extend(batch)
            if len(batch) < page_size:
                break
            first += page_size
    return [
        {
            "idp_user_id": u["id"],
            "username": u.get("username", ""),
            "email": u.get("email"),
            "enabled": bool(u.get("enabled", False)),
        }
        for u in users
        # Service accounts are not people and must not be issued surface keys.
        if not u.get("username", "").startswith("service-account-")
    ]


async def health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base_url()}/realms/{realm()}")
            return r.status_code == 200
    except Exception:
        return False
