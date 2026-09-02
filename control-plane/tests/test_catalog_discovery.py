"""Model discovery tests — app/catalog.py + the agents picker wiring (item 8e0).

catalog.model_ids reads the router's OpenRouter-parity /v1/models and must degrade to an
empty tuple on any failure so a picker falls back to its default rather than breaking.
allowed_models() must PREFER live discovery and fall back to the env list only when the
catalog is empty/unreachable.
"""

from __future__ import annotations

import httpx
import pytest

from app import catalog


def _client_returning(handler):
    real = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return factory


def test_model_ids_parses_catalog_in_order(monkeypatch):
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [
            {"id": "glm-5.2@deepinfra"}, {"id": "qwen3-coder-480b"}, {"id": "deepseek-v4-pro"},
        ]})

    monkeypatch.setattr(catalog.httpx, "Client", _client_returning(handler))
    assert catalog.model_ids("http://freerouter:8080") == (
        "glm-5.2@deepinfra", "qwen3-coder-480b", "deepseek-v4-pro",
    )


def test_model_ids_empty_catalog_is_empty_tuple(monkeypatch):
    monkeypatch.setattr(catalog.httpx, "Client",
                        _client_returning(lambda r: httpx.Response(200, json={"data": []})))
    assert catalog.model_ids("http://freerouter:8080") == ()


def test_model_ids_unreachable_router_degrades_to_empty(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setattr(catalog.httpx, "Client", _client_returning(boom))
    assert catalog.model_ids("http://freerouter:8080") == ()


def test_model_ids_skips_entries_without_id(monkeypatch):
    monkeypatch.setattr(catalog.httpx, "Client", _client_returning(
        lambda r: httpx.Response(200, json={"data": [{"id": "ok"}, {"name": "no-id"}, {}]})))
    assert catalog.model_ids("http://freerouter:8080") == ("ok",)


def test_catalog_url_precedence(monkeypatch):
    monkeypatch.delenv("CATALOG_URL", raising=False)
    monkeypatch.delenv("FREEROUTER_URL", raising=False)
    assert catalog.catalog_url() == "http://freerouter:8080"
    monkeypatch.setenv("FREEROUTER_URL", "http://fr:9000/")
    assert catalog.catalog_url() == "http://fr:9000"
    monkeypatch.setenv("CATALOG_URL", "http://router.example/")
    assert catalog.catalog_url() == "http://router.example"


# --- the agents picker prefers discovery, falls back to the static list -----------------

agents = pytest.importorskip("app.agents", reason="agents.py imports unavailable in this env")


def test_allowed_models_prefers_discovery(monkeypatch):
    monkeypatch.setattr(agents.catalog, "model_ids", lambda: ("a@prov", "b@prov"))
    monkeypatch.setenv("AGENT_MODELS", "stale-1,stale-2")
    assert agents.allowed_models() == ("a@prov", "b@prov")


def test_allowed_models_falls_back_to_env_when_catalog_empty(monkeypatch):
    monkeypatch.setattr(agents.catalog, "model_ids", lambda: ())
    monkeypatch.setenv("AGENT_MODELS", "glm-5.2@deepinfra, glm-4.7@deepinfra")
    assert agents.allowed_models() == ("glm-5.2@deepinfra", "glm-4.7@deepinfra")


def test_allowed_models_falls_back_to_default_when_nothing(monkeypatch):
    monkeypatch.setattr(agents.catalog, "model_ids", lambda: ())
    monkeypatch.delenv("AGENT_MODELS", raising=False)
    assert agents.allowed_models() == (agents.DEFAULT_MODEL,)
