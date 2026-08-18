"""The behavioural-analytics report page, in a real browser.

HTTP-level tests prove the endpoint slices and the file is served. They prove nothing about
whether analytics.html's JavaScript actually renders the rows or re-renders when the operator
switches the comparison dimension — and the page is entirely JavaScript. So this drives a real
Chromium against the page, with the data API mocked (route-intercepted) so it needs no cluster:
it asserts the rows render and that changing the dimension select genuinely re-renders, and it
fails on any console error, matching test_browser.py's ethos.

Runs under `make test-browser` (needs the playwright browser binaries), not the hermetic suite.
"""

import json
import os
import sys

import pytest
from playwright.sync_api import expect, sync_playwright

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "control-plane"))

from app.analytics import metrics  # noqa: E402

PAGE = os.path.join(ROOT, "control-plane", "app", "portal_static", "analytics.html")
STORE = os.path.join(ROOT, "control-plane", "tests", "fixtures", "analytics", "records_store.jsonl")


def _payloads():
    turns, sessions = [], []
    with open(STORE) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                (turns if r["k"] == "turn" else sessions).append(r)
    out = {}
    for dim, key in (("surface", lambda r: r["surface"]), ("model", lambda r: r["model"])):
        m = metrics.build_metrics(turns, sessions, key=key, min_n=1)
        m["dimensions"] = ["model", "surface"]
        out[dim] = m
    return out


@pytest.fixture(scope="module")
def payloads():
    return _payloads()


def test_page_renders_and_dimension_switch_rerenders(payloads):
    html = open(PAGE).read()
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        # serve the page itself and mock its data API off the same origin
        page.route("**/analytics.html", lambda route: route.fulfill(
            status=200, content_type="text/html", body=html))

        def api(route):
            dim = "surface" if "dimension=surface" in route.request.url else "model"
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payloads[dim]))

        page.route("**/portal/api/analytics*", api)
        page.goto("http://localhost/analytics.html")

        # default load is by model -> the two model columns render as table headers
        expect(page.locator("thead th", has_text="glm-5.2").first).to_be_visible()
        expect(page.locator("thead th", has_text="gpt-fake").first).to_be_visible()

        # switch the dimension -> the page re-renders with the surface columns
        page.select_option("#dimension", "surface")
        expect(page.locator("thead th", has_text="terminal").first).to_be_visible()
        expect(page.locator("thead th", has_text="chat").first).to_be_visible()
        # and the model columns are gone
        expect(page.locator("thead th", has_text="glm-5.2")).to_have_count(0)

        browser.close()

    assert not errors, f"console/page errors on the analytics page: {errors}"
