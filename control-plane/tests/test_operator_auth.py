"""require_operator: operators ACT through the console via role, token stays for CLI (c44)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import main, portal


class FakeRequest:
    def __init__(self, host="127.0.0.1", headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


def _creds(tok):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok)


def test_admin_token_authorizes(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_ADMIN_TOKEN", "sekret")
    assert main.require_operator(FakeRequest(), _creds("sekret")) == "admin-token"


def test_bad_token_without_role_is_denied(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_ADMIN_TOKEN", "sekret")
    with pytest.raises(HTTPException) as e:
        main.require_operator(FakeRequest(), _creds("wrong"))
    assert e.value.status_code == 403


def test_operator_role_via_proxy_authorizes_and_names_the_operator(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_ADMIN_TOKEN", "sekret")
    monkeypatch.setattr(portal, "ADMIN_ROLES", {"operator"})
    req = FakeRequest(headers={
        "x-auth-request-groups": "operator",
        "x-auth-request-preferred-username": "carol",
    })
    # no bearer token — a browser operator carries only the proxy identity
    assert main.require_operator(req, None) == "carol"


def test_signed_in_non_operator_cannot_act(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_ADMIN_TOKEN", "sekret")
    monkeypatch.setattr(portal, "ADMIN_ROLES", {"operator"})
    req = FakeRequest(headers={
        "x-auth-request-groups": "users",
        "x-auth-request-preferred-username": "dave",
    })
    with pytest.raises(HTTPException) as e:
        main.require_operator(req, None)
    assert e.value.status_code == 403


def test_role_claimed_off_the_trusted_proxy_is_ignored(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_ADMIN_TOKEN", "sekret")
    monkeypatch.setattr(portal, "ADMIN_ROLES", {"operator"})
    # forged groups header from a non-loopback source → roles() returns empty → 403
    req = FakeRequest(host="10.0.0.5", headers={"x-auth-request-groups": "operator"})
    with pytest.raises(HTTPException) as e:
        main.require_operator(req, None)
    assert e.value.status_code == 403
