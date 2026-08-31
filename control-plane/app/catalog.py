"""Model discovery — the one catalog every picker reads (item enterpriseaiframework-8e0).

The design (docs/design/records/freerouter-reference-router.md, C2) removes per-surface
model lists: a model that appears in the router's `/v1/models` is immediately usable by
every consumer, with no rendered catalog and no re-wiring. This module is that single
read. It is deliberately graceful — a picker that cannot reach the router or finds an
empty catalog falls back to its configured default rather than breaking — so introducing
the spoke never makes a surface worse than the hardcoded list it replaces.

Source URL precedence: explicit arg → CATALOG_URL → FREEROUTER_URL → the in-cluster
freerouter service. The endpoint is the OpenRouter-parity `GET /v1/models`, whose envelope
is `{"data": [{"id": ...}, ...]}` (freerouter already filters non-chat-completable models
out of it — freerouter-ff3 — so an id returned here is chat-usable).
"""

from __future__ import annotations

import os

import httpx


def catalog_url() -> str:
    return (
        os.environ.get("CATALOG_URL")
        or os.environ.get("FREEROUTER_URL")
        or "http://freerouter:8080"
    ).rstrip("/")


def model_ids(base_url: str | None = None) -> tuple[str, ...]:
    """The model ids the router currently advertises, in catalog order.

    Returns an empty tuple on any failure (unreachable router, malformed body, empty
    catalog). Callers treat empty as "fall back to my configured default" — discovery is
    an enhancement over a static list, never a hard dependency that can break a picker.
    """
    url = (base_url or catalog_url()).rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{url}/v1/models")
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception:
        return ()
    return tuple(m["id"] for m in data if isinstance(m, dict) and m.get("id"))
