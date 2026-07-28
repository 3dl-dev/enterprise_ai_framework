"""The portal's identity boundary.

The portal decides who you are from headers its oauth2-proxy sidecar sets. Those headers
are forgeable by anything that can open a socket to this service, and this service is
reachable from every pod in the namespace. The only thing standing between "signed in as
baron" and "claims to be baron" is that the sidecar shares the pod's network namespace,
so its requests arrive from 127.0.0.1 and nobody else's do.

That makes `require_user` a security control rather than a convenience, and it is tested
here as one: every case below is an attempt to be somebody without going through the
proxy. If these tests are ever "simplified" into checking that a valid header works, the
control is gone and nothing will say so.
"""

import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The portal module pulls in siblings that want a live database at import time. Only
# require_user is under test, so those are stubbed rather than started — the alternative
# is a test that needs Postgres to prove a header check, which nobody would then run.
for name in ("app.db", "app.gateway", "app.metering", "app.issuance", "app.chat_identity"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
sys.modules["app.gateway"].SURFACES = ("chat", "ide", "terminal")

from app.portal import require_user  # noqa: E402


@pytest.fixture()
def client():
    """A tiny app exposing require_user directly, so nothing else can mask the result."""
    api = FastAPI()

    @api.get("/whoami")
    def whoami(request: Request):
        try:
            return {"user": require_user(request)}
        except HTTPException as exc:
            return {"error": exc.status_code, "detail": exc.detail}

    # TestClient presents client.host == "testclient" unless told otherwise, which is
    # not an address at all. Pin it to loopback so this exercises the real predicate.
    return TestClient(api, client=("127.0.0.1", 43210))


def test_loopback_with_the_proxys_header_is_accepted(client):
    r = client.get("/whoami", headers={"X-Auth-Request-Preferred-Username": "baron"})
    assert r.json() == {"user": "baron"}, (
        "TestClient speaks to the app over loopback, which is what the sidecar does. "
        "If this fails the portal cannot authenticate anybody at all."
    )


def test_the_other_header_spellings_are_accepted(client):
    for header in ("X-Forwarded-Preferred-Username", "X-Forwarded-User"):
        r = client.get("/whoami", headers={header: "claire"})
        assert r.json() == {"user": "claire"}, f"{header} was not honoured"


def test_no_header_at_all_is_not_signed_in(client):
    assert client.get("/whoami").json()["error"] == 401


def test_an_empty_header_is_not_signed_in(client):
    r = client.get("/whoami", headers={"X-Auth-Request-Preferred-Username": "   "})
    assert r.json()["error"] == 401, "whitespace is not a username"


def test_a_request_from_another_pod_is_refused_even_with_a_perfect_header(monkeypatch):
    """The whole control, stated as a test.

    A pod in the namespace can reach this Service and set any header it likes. It must
    get nothing, no matter how convincing the header is.
    """
    api = FastAPI()

    @api.get("/whoami")
    def whoami(request: Request):
        try:
            return {"user": require_user(request)}
        except HTTPException as exc:
            return {"error": exc.status_code, "detail": exc.detail}

    client = TestClient(api, client=("127.0.0.1", 43210))
    # Override the peer address to imitate a request that arrived over the pod network
    # rather than through the shared namespace.
    import starlette.requests

    original = starlette.requests.Request.client.fget
    monkeypatch.setattr(
        starlette.requests.Request, "client",
        property(lambda self: types.SimpleNamespace(host="10.42.0.99", port=54321)),
    )
    try:
        r = client.get("/whoami", headers={"X-Auth-Request-Preferred-Username": "baron"})
        assert r.json()["error"] == 403, (
            "a forged identity header from off-pod was accepted — any workload in the "
            "namespace can now read anybody's spend and rotate anybody's key"
        )
    finally:
        monkeypatch.setattr(starlette.requests.Request, "client", property(original))


def test_a_request_with_no_peer_address_is_refused(monkeypatch):
    """`request.client` is None for some transports. That must fail closed, not open."""
    api = FastAPI()

    @api.get("/whoami")
    def whoami(request: Request):
        try:
            return {"user": require_user(request)}
        except HTTPException as exc:
            return {"error": exc.status_code, "detail": exc.detail}

    import starlette.requests

    original = starlette.requests.Request.client.fget
    monkeypatch.setattr(starlette.requests.Request, "client", property(lambda self: None))
    try:
        r = TestClient(api, client=("127.0.0.1", 43210)).get(
            "/whoami", headers={"X-Auth-Request-Preferred-Username": "baron"})
        assert r.json()["error"] == 403
    finally:
        monkeypatch.setattr(starlette.requests.Request, "client", property(original))
