"""Tests for the aider model-metadata renderer (item enterpriseaiframework-987 / -037)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "render_aider_metadata", REPO / "bundle" / "bin" / "render_aider_metadata.py"
)
ram = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ram)


def _base():
    return {
        "_comment": ["existing explanation"],
        "openai/glm-5.2@deepinfra": {
            "max_input_tokens": 200000,
            "max_output_tokens": 32768,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
            "litellm_provider": "openai",
            "mode": "chat",
        },
        "openai/fake-large": {
            "max_input_tokens": 32000,
            "max_output_tokens": 4096,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
            "litellm_provider": "openai",
            "mode": "chat",
        },
    }


def test_a_novel_catalog_model_surfaces_with_no_hand_edit():
    """The item's own done-condition: a model never seen before appears after a render."""
    out = ram.render_model_metadata(
        _base(), [{"id": "brand-new-model@deepinfra", "context_length": 262144}]
    )
    assert "openai/brand-new-model@deepinfra" in out
    entry = out["openai/brand-new-model@deepinfra"]
    assert entry["max_input_tokens"] == 262144
    assert entry["max_output_tokens"] == ram.DEFAULT_OUTPUT
    assert entry["input_cost_per_token"] == 0
    assert entry["output_cost_per_token"] == 0
    assert entry["litellm_provider"] == "openai"


def test_context_length_defaults_when_catalog_omits_it():
    out = ram.render_model_metadata(_base(), [{"id": "x@deepinfra"}])
    assert out["openai/x@deepinfra"]["max_input_tokens"] == ram.DEFAULT_CONTEXT


def test_models_dropped_from_the_catalog_are_dropped_from_the_table():
    # Regenerated from the catalog every render, like the opencode/gateway renderers —
    # a model the router no longer advertises should not linger with a stale window.
    out = ram.render_model_metadata(_base(), [{"id": "only-one@deepinfra", "context_length": 100000}])
    assert "openai/glm-5.2@deepinfra" not in out


def test_fake_large_is_always_pinned_even_though_no_real_catalog_ever_lists_it():
    out = ram.render_model_metadata(_base(), [{"id": "x@deepinfra", "context_length": 100000}])
    assert out["openai/fake-large"] == _base()["openai/fake-large"]


def test_comment_key_is_preserved():
    out = ram.render_model_metadata(_base(), [{"id": "x@deepinfra"}])
    assert out["_comment"] == ["existing explanation"]


def test_empty_catalog_leaves_the_table_untouched():
    base = _base()
    out = ram.render_model_metadata(base, [])
    assert out is base  # graceful: keep the baked table rather than blanking it


def test_entries_without_id_are_skipped():
    out = ram.render_model_metadata(_base(), [{"context_length": 100000}, {"id": "ok@deepinfra"}])
    assert "openai/ok@deepinfra" in out
    assert len(out) == 3  # _comment + ok + pinned fake-large


def test_render_does_not_mutate_the_input_base():
    base = _base()
    ram.render_model_metadata(base, [{"id": "x@deepinfra"}])
    assert "openai/x@deepinfra" not in base
    assert "openai/glm-5.2@deepinfra" in base
