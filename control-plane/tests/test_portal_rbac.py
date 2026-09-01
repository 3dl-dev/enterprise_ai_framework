"""RBAC for the operator console (item enterpriseaiframework-d70).

Operator access is granted by a Keycloak realm role delivered through the authenticating
proxy's groups header, with the PORTAL_ADMINS username allowlist kept as a transition
fallback. These tests lock in: roles are honoured only from the trusted proxy; either a
role OR the allowlist grants the console; neither yields the console-hiding 404.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import portal


class FakeRequest:
    def __init__(self, host="127.0.0.1", headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


def test_roles_read_from_groups_header_on_the_trusted_proxy(monkeypatch):
    req = FakeRequest(headers={"x-auth-request-groups": "/operators, users"})
    assert portal.roles(req) == {"operators", "users"}  # leading slash stripped


def test_roles_ignored_off_the_trusted_proxy():
    req = FakeRequest(host="10.0.0.9", headers={"x-auth-request-groups": "admin"})
    assert portal.roles(req) == set()  # forged header from a non-proxy source means nothing


def test_admin_role_grants_the_console_without_the_allowlist(monkeypatch):
    monkeypatch.setattr(portal, "ADMINS", set())
    monkeypatch.setattr(portal, "ADMIN_ROLES", {"operator"})
    req = FakeRequest(headers={"x-auth-request-groups": "operator"})
    assert portal.require_admin_user(req, user="carol") == "carol"


def test_allowlist_is_the_fallback_when_no_roles(monkeypatch):
    monkeypatch.setattr(portal, "ADMINS", {"julie"})
    monkeypatch.setattr(portal, "ADMIN_ROLES", {"operator"})
    req = FakeRequest(headers={})  # proxy passes no groups yet
    assert portal.require_admin_user(req, user="julie") == "julie"


def test_neither_role_nor_allowlist_hides_the_console(monkeypatch):
    monkeypatch.setattr(portal, "ADMINS", {"julie"})
    monkeypatch.setattr(portal, "ADMIN_ROLES", {"operator"})
    req = FakeRequest(headers={"x-auth-request-groups": "users"})
    with pytest.raises(HTTPException) as exc:
        portal.require_admin_user(req, user="mallory")
    assert exc.value.status_code == 404
