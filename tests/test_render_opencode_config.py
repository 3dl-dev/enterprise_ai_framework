"""Tests for the opencode model-list renderer (item enterpriseaiframework-75c)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "render_opencode_config", REPO / "bundle" / "bin" / "render_opencode_config.py"
)
roc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(roc)


def _template():
    return {
        "provider": {
            "enterprise-ai": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "{env:OPENAI_API_BASE}"},
                "models": {"glm-5.2@deepinfra": {"name": "GLM 5.2", "limit": {"context": 1, "output": 1}}},
            }
        },
        "model": "enterprise-ai/glm-5.2@deepinfra",
        "instructions": ["/etc/opencode/PLATFORM.md"],
        "mcp": {"echo": {"type": "remote"}},
    }


def test_models_block_is_regenerated_from_catalog():
    catalog = [
        {"id": "qwen3-coder@deepinfra", "name": "Qwen3 Coder", "context_length": 262144},
        {"id": "deepseek-v4@deepinfra", "name": "DeepSeek V4", "context_length": 1048576},
    ]
    out = roc.render_opencode_config(_template(), catalog)
    models = out["provider"]["enterprise-ai"]["models"]
    assert list(models) == ["qwen3-coder@deepinfra", "deepseek-v4@deepinfra"]
    assert models["qwen3-coder@deepinfra"]["name"] == "Qwen3 Coder"
    assert models["qwen3-coder@deepinfra"]["limit"]["context"] == 262144
    assert models["qwen3-coder@deepinfra"]["limit"]["output"] == roc.OUTPUT_CAP


def test_name_is_humanized_and_context_defaults_when_absent():
    out = roc.render_opencode_config(_template(), [{"id": "glm-5.2@deepinfra"}])
    entry = out["provider"]["enterprise-ai"]["models"]["glm-5.2@deepinfra"]
    assert entry["name"] == "Glm 5.2"
    assert entry["limit"]["context"] == roc.DEFAULT_CONTEXT


def test_default_model_preserved_when_still_in_catalog():
    out = roc.render_opencode_config(
        _template(), [{"id": "glm-5.2@deepinfra"}, {"id": "other@deepinfra"}]
    )
    assert out["model"] == "enterprise-ai/glm-5.2@deepinfra"


def test_default_model_repointed_when_gone_from_catalog():
    out = roc.render_opencode_config(_template(), [{"id": "brand-new@deepinfra"}])
    assert out["model"] == "enterprise-ai/brand-new@deepinfra"


def test_empty_catalog_leaves_template_untouched():
    tpl = _template()
    out = roc.render_opencode_config(tpl, [])
    assert out is tpl  # graceful: keep the baked models rather than blanking the picker


def test_non_model_config_is_preserved():
    out = roc.render_opencode_config(_template(), [{"id": "x@deepinfra"}])
    assert out["instructions"] == ["/etc/opencode/PLATFORM.md"]
    assert out["mcp"] == {"echo": {"type": "remote"}}
    assert out["provider"]["enterprise-ai"]["npm"] == "@ai-sdk/openai-compatible"


def test_render_does_not_mutate_the_input_template():
    tpl = _template()
    roc.render_opencode_config(tpl, [{"id": "x@deepinfra"}])
    assert list(tpl["provider"]["enterprise-ai"]["models"]) == ["glm-5.2@deepinfra"]
