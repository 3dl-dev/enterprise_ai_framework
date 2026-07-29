"""Integration tests run against the running bundle.

These are the tests the sealed estimate is adjudicated against — each one maps to a
numbered scope item in docs/evidence/conventional-estimate.md. They deliberately talk to
the real stack over the network rather than mocking it: an item proven against a mock is
not proven.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parent.parent / "bundle"


def _load_env() -> dict:
    env_file = BUNDLE / ".env"
    if not env_file.exists():
        pytest.exit("bundle/.env missing — run `make up` first", returncode=2)
    out = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


@pytest.fixture(scope="session")
def env() -> dict:
    return _load_env()


@pytest.fixture(scope="session")
def gateway_url(env) -> str:
    return f"http://localhost:{env.get('GATEWAY_PORT', '4000')}"


@pytest.fixture(scope="session")
def control_plane_url(env) -> str:
    return f"http://localhost:{env.get('CONTROL_PLANE_PORT', '8081')}"


@pytest.fixture(scope="session")
def idp_url(env) -> str:
    return f"http://localhost:{env.get('IDP_PORT', '8082')}"


@pytest.fixture(scope="session")
def fakeprovider_url(env) -> str:
    """The stand-in upstream, reached directly rather than through the gateway.

    Needed by the cache tests: whether the provider was called is the only thing that
    distinguishes a cached reply from a fresh one, because every byte of the reply is a
    pure function of the request. See fakeprovider/app.py.
    """
    return f"http://localhost:{env.get('FAKEPROVIDER_PORT', '8090')}"


@pytest.fixture(scope="session")
def admin_headers(env) -> dict:
    return {"Authorization": f"Bearer {env['CONTROL_PLANE_ADMIN_TOKEN']}"}


@pytest.fixture(scope="session")
def master_headers(env) -> dict:
    return {"Authorization": f"Bearer {env['GATEWAY_MASTER_KEY']}"}


def compose(*args: str, check=True) -> subprocess.CompletedProcess:
    """Drive the bundle from a test. Used by the restart and revocation tests."""
    return subprocess.run(
        ["docker", "compose", "-f", str(BUNDLE / "docker-compose.yml"),
         "--env-file", str(BUNDLE / ".env"), *args],
        capture_output=True, text=True, check=check,
    )


def portal_get(path: str, username: str) -> tuple[int, str]:
    """GET a portal path from inside the control-plane container, as `username`.

    WHY IT HAS TO BE DONE THIS WAY. The portal takes the caller's identity from the
    headers its authenticating proxy sets, and portal.py trusts those headers only when
    the connection comes from loopback — otherwise anybody who could reach the published
    port could name themselves anybody. The compose bundle ships no oauth2-proxy, so from
    outside the container there is no way for a test to *be* somebody, and the published
    port answers every portal route with a 401. Reaching in over `compose exec` is what
    the sidecar would otherwise do, and it is the reason the whole /portal/api/admin/*
    surface had never been touched by a test.

    Returns (status, body) and does not raise on 4xx: the 404 a non-operator gets is a
    thing tests need to assert on, not an accident.

    urllib and not httpx/curl on purpose — the control-plane image has neither.
    """
    script = (
        "import json,sys,urllib.request,urllib.error\n"
        f"req=urllib.request.Request({json.dumps('http://localhost:8000' + path)},"
        f" headers={{'x-auth-request-preferred-username': {json.dumps(username)}}})\n"
        "try:\n"
        "    r=urllib.request.urlopen(req); out={'status':r.status,'body':r.read().decode()}\n"
        "except urllib.error.HTTPError as e:\n"
        "    out={'status':e.code,'body':e.read().decode()}\n"
        # One line, last line, so nothing the container writes to stdout first can be
        # mistaken for the payload.
        "sys.stdout.write('\\n'+json.dumps(out))\n"
    )
    out = compose("exec", "-T", "control-plane", "python", "-c", script)
    envelope = json.loads(out.stdout.strip().splitlines()[-1])
    return envelope["status"], envelope["body"]


def idp_admin_token(env) -> str:
    import httpx

    r = httpx.post(
        f"http://localhost:{env.get('IDP_PORT', '8082')}"
        "/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": env.get("IDP_ADMIN_USER", "admin"),
            "password": env["IDP_ADMIN_PASSWORD"],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def set_user_enabled(env, username: str, enabled: bool) -> None:
    """Flip the enabled flag in the IdP — the trigger for propagated revocation."""
    import httpx

    idp = f"http://localhost:{env.get('IDP_PORT', '8082')}"
    realm = env.get("IDP_REALM", "enterprise-ai")
    headers = {"Authorization": f"Bearer {idp_admin_token(env)}"}

    found = httpx.get(
        f"{idp}/admin/realms/{realm}/users",
        headers=headers, params={"username": username, "exact": "true"}, timeout=30,
    ).json()
    if not found:
        raise AssertionError(f"user {username} not found in realm {realm}")

    r = httpx.put(
        f"{idp}/admin/realms/{realm}/users/{found[0]['id']}",
        headers={**headers, "Content-Type": "application/json"},
        json={"enabled": enabled},
        timeout=30,
    )
    r.raise_for_status()


@pytest.fixture(scope="session")
def chat_url(env) -> str:
    return f"http://localhost:{env.get('CHAT_PORT', '3080')}"


@pytest.fixture(scope="session")
def chat_session(chat_url, env):
    """One signed-in chat session for the whole run.

    Session-scoped deliberately: the chat surface rate-limits login attempts, and a
    per-test login trips it partway through the suite and turns every later test red for
    the wrong reason.
    """
    import oidc_login

    client = oidc_login.login(chat_url, DOGFOOD_USER, DOGFOOD_PASSWORD)
    yield client
    client.close()


# The bootstrap realm user, created by `make up`. Read from .env rather than hardcoded so
# the suite tracks whatever the bundle actually provisioned.
DOGFOOD_USER = _load_env().get("BOOTSTRAP_USER", "baron")
DOGFOOD_PASSWORD = _load_env().get("BOOTSTRAP_PASSWORD", "")


@pytest.fixture(scope="session")
def stack_up(control_plane_url, admin_headers):
    """Fail fast and clearly if the bundle is not running, rather than as N cryptic errors."""
    import httpx

    try:
        r = httpx.get(f"{control_plane_url}/ready", headers=admin_headers, timeout=10)
    except Exception as exc:
        pytest.exit(f"bundle not reachable at {control_plane_url}: {exc}", returncode=2)
    if not r.json().get("ready"):
        pytest.exit(f"bundle not ready: {r.json()}", returncode=2)
    return True
