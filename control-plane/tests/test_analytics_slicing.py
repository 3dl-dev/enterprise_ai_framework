"""Slicing — the same corpus grouped by any dimension, with tenant isolation.

Proves the report can compare models, surfaces and users off one record set, and that a
tenant-scoped slice never carries another tenant's numbers.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import slicing  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "analytics")


def _jsonl(name):
    with open(os.path.join(FIX, name)) as f:
        return [json.loads(line) for line in f if line.strip()]


def _corpus():
    recs = _jsonl("opencode_expected.jsonl") + _jsonl("librechat_expected.jsonl")
    return [r for r in recs if r["k"] == "turn"], [r for r in recs if r["k"] == "session"]


def test_slice_by_surface_and_by_model():
    turns, sess = _corpus()
    by_surface = slicing.slice_metrics(turns, sess, "surface", min_n=1)
    assert set(by_surface["meta"]["groups"]) == {"terminal", "chat"}
    assert by_surface["meta"]["dimension"] == "surface"

    by_model = slicing.slice_metrics(turns, sess, "model", min_n=1)
    assert set(by_model["meta"]["groups"]) == {"glm-5.2", "gpt-fake"}


def test_unknown_dimension_fails_loudly():
    turns, sess = _corpus()
    try:
        slicing.slice_metrics(turns, sess, "nonsense", min_n=1)
    except KeyError:
        return
    raise AssertionError("expected KeyError for an unknown dimension")


def test_tenant_scope_isolates():
    turns, sess = _corpus()  # all tenant "camp"
    # inject one other-tenant turn+session on a distinct model.
    other_t = {"k": "turn", "tenant": "acme", "surface": "terminal", "sess": "z",
               "principal": "zoe", "model": "other-model", "tools": 0, "tc": {}, "dispatch": 0,
               "post_disp": None, "ends_q": 0, "nudge": 0, "interrupted": 0, "out_tok": 0,
               "text_chars": 0, "n_asst": 1, "style": None, "ts": "2026-08-01T12:00:00+00:00"}
    other_s = {"k": "session", "tenant": "acme", "surface": "terminal", "sess": "z",
               "principal": "zoe", "model": "other-model", "n_subagents": 0, "agents_per_wave": 0,
               "subagent_output_tokens": 0, "code": None}

    scoped = slicing.slice_metrics(turns + [other_t], sess + [other_s], "model",
                                   tenant="camp", min_n=1)
    assert scoped["meta"]["tenant"] == "camp"
    # the other tenant's model must not appear in a camp-scoped slice.
    assert "other-model" not in scoped["meta"]["groups"]

    # grouping BY tenant globally shows both as separate columns (comparison, not leak).
    by_tenant = slicing.slice_metrics(turns + [other_t], sess + [other_s], "tenant", min_n=1)
    assert set(by_tenant["meta"]["groups"]) == {"camp", "acme"}


def test_available_dimensions():
    turns, _ = _corpus()
    dims = slicing.available_dimensions(turns, min_distinct=2)
    # two surfaces and two models are present; effort is unset everywhere, config unsupported.
    assert "surface" in dims and "model" in dims
    assert "config" not in dims and "effort" not in dims
