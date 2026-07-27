"""Live tests against Forge. These spend real money (fractions of a cent per run).

Deliberately outside `tests/`, which pytest.ini scopes to the hermetic suite. The nine
scope items must stay provable with no provider account and no spend — that is item 8 —
so anything that talks to a real upstream lives here and runs only when asked, via
`make test-forge`.

Missing credentials fail loudly rather than skipping. A skipped test that silently
reports success is how you end up believing an upstream works when nobody has checked.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parent.parent / "bundle"


def _env() -> dict:
    out = {}
    env_file = BUNDLE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    out.update({k: v for k, v in os.environ.items() if k.startswith("FORGE_")})
    return out


@pytest.fixture(scope="session")
def env() -> dict:
    e = _env()
    for required in ("FORGE_BASE_URL", "FORGE_API_KEY", "FORGE_ACCOUNT_ID"):
        if not e.get(required):
            pytest.fail(
                f"{required} is not configured in bundle/.env — "
                "run `make forge-config` to load it from 1Password"
            )
    return e


@pytest.fixture(scope="session")
def forge_url(env) -> str:
    return env["FORGE_BASE_URL"].rstrip("/")


@pytest.fixture(scope="session")
def forge_headers(env) -> dict:
    return {"Authorization": f"Bearer {env['FORGE_API_KEY']}"}


@pytest.fixture(scope="session")
def gateway_url(env) -> str:
    return f"http://localhost:{env.get('GATEWAY_PORT', '4000')}"


@pytest.fixture(scope="session")
def virtual_key(env, gateway_url) -> str:
    """A dedicated virtual key, minted for this run and revoked after.

    Traffic must go surface -> virtual key -> our gateway -> Forge; using the gateway
    master key would skip exactly the part we built.

    It mints its own rather than borrowing the chat surface's key from .env, because the
    hermetic suite's exit-path test revokes every key and re-mints them. Sharing that key
    made this suite fail intermittently when run after `make test` — which is the obvious
    order to run them in, and an intermittent failure that clears on a rerun is the worst
    kind: it teaches you to rerun instead of to look.
    """
    import httpx

    alias = "forge-live-test::terminal"
    master = {"Authorization": f"Bearer {env['GATEWAY_MASTER_KEY']}"}

    # Clear any leftover from an interrupted run so the alias is never doubly minted.
    httpx.post(f"{gateway_url}/key/delete", headers=master,
               json={"key_aliases": [alias]}, timeout=60)

    created = httpx.post(
        f"{gateway_url}/key/generate", headers=master,
        json={"key_alias": alias, "metadata": {"surface": "terminal", "issuer": "live-tests"}},
        timeout=60,
    )
    if created.status_code != 200:
        pytest.fail(f"could not mint a virtual key ({created.status_code}); is `make up` done? "
                    f"{created.text[:200]}")

    yield created.json()["key"]

    httpx.post(f"{gateway_url}/key/delete", headers=master,
               json={"key_aliases": [alias]}, timeout=60)


@pytest.fixture(scope="session")
def control_plane_url(env) -> str:
    return f"http://localhost:{env.get('CONTROL_PLANE_PORT', '8081')}"


@pytest.fixture(scope="session")
def admin_headers(env) -> dict:
    return {"Authorization": f"Bearer {env['CONTROL_PLANE_ADMIN_TOKEN']}"}


@pytest.fixture(scope="session")
def forge_admin_key(env) -> str:
    """Setup-time credential, fetched on demand and never persisted.

    Needed to read /v1/usage and /v1/pricing, which is how these tests reconcile our
    computed cost against what Forge actually billed.
    """
    if env.get("FORGE_ADMIN_KEY"):
        return env["FORGE_ADMIN_KEY"]
    r = subprocess.run(
        ["op", "item", "get", "Forge / enterprise-ai-framework",
         "--vault", "3dl-ops", "--fields", "FORGE_ADMIN_KEY", "--reveal"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        pytest.fail(
            "Forge admin key unavailable; sign in with `op signin`. "
            "It is needed to read usage for the reconciliation check."
        )
    return r.stdout.strip()


def forge_usage(forge_url: str, admin_key: str, account_id: str) -> list[dict]:
    """All usage events for the account.

    Queried without `since`/`until` on purpose: those filters are a known Forge bug
    (forge-f22) that returns an empty list for any range, including ranges that match
    everything. It fails closed and silent — an empty result reads as "no spend" rather
    than as an error — so filtering happens client side.
    """
    import httpx

    r = httpx.get(
        f"{forge_url}/v1/usage",
        params={"account_id": account_id},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("data", body) if isinstance(body, dict) else body
