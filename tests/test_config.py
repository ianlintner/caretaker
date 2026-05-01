"""Tests for configuration loading."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from caretaker.config import MaintainerConfig


class TestMaintainerConfig:
    def test_defaults(self) -> None:
        config = MaintainerConfig()
        assert config.version == "v1"
        assert config.pr_agent.enabled is True
        assert config.pr_agent.auto_merge.copilot_prs is True
        assert config.pr_agent.auto_merge.dependabot_prs is True
        assert config.pr_agent.auto_merge.human_prs is False
        assert config.pr_agent.copilot.max_retries == 2
        assert config.pr_agent.ci.close_managed_prs_on_backlog is False
        assert config.issue_agent.enabled is True
        assert config.issue_agent.auto_assign_bugs is True
        assert config.issue_agent.auto_assign_features is False
        assert config.charlie_agent.enabled is True
        assert config.charlie_agent.stale_days == 14
        assert config.upgrade_agent.enabled is True
        assert config.upgrade_agent.strategy == "auto-minor"

    def test_from_yaml(self) -> None:
        yaml_content = """
version: v1
pr_agent:
  auto_merge:
    copilot_prs: false
    merge_method: merge
  copilot:
    max_retries: 3
  ci:
    close_managed_prs_on_backlog: true
issue_agent:
  auto_assign_features: true
charlie_agent:
  stale_days: 21
  close_duplicate_prs: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = MaintainerConfig.from_yaml(f.name)

        assert config.pr_agent.auto_merge.copilot_prs is False
        assert config.pr_agent.auto_merge.merge_method == "merge"
        assert config.pr_agent.copilot.max_retries == 3
        assert config.pr_agent.ci.close_managed_prs_on_backlog is True
        assert config.issue_agent.auto_assign_features is True
        assert config.charlie_agent.stale_days == 21
        assert config.charlie_agent.close_duplicate_prs is False
        # Defaults still apply for unspecified fields
        assert config.pr_agent.auto_merge.dependabot_prs is True

    def test_from_empty_yaml(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("")
            f.flush()
            config = MaintainerConfig.from_yaml(f.name)

        assert config.version == "v1"
        assert config.pr_agent.enabled is True

    def test_partial_yaml(self) -> None:
        yaml_content = """
escalation:
  targets:
    - "@lead-dev"
  stale_days: 14
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = MaintainerConfig.from_yaml(f.name)

        assert config.escalation.targets == ["@lead-dev"]
        assert config.escalation.stale_days == 14
        assert config.pr_agent.enabled is True

    def test_unsupported_version_raises(self) -> None:
        yaml_content = """
version: v2
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with pytest.raises(ValueError, match="Unsupported config version"):
                MaintainerConfig.from_yaml(f.name)

    def test_unknown_top_level_key_raises(self) -> None:
        yaml_content = """
version: v1
unknown_key: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with pytest.raises(Exception, match="."):
                MaintainerConfig.from_yaml(f.name)

    def test_unknown_nested_key_raises(self) -> None:
        yaml_content = """
version: v1
pr_agent:
  ci:
    not_a_real_field: 123
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with pytest.raises(Exception, match="."):
                MaintainerConfig.from_yaml(f.name)

    def test_non_mapping_yaml_root_raises(self) -> None:
        yaml_content = """
- just
- a
- list
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with pytest.raises(ValueError, match="must be a mapping"):
                MaintainerConfig.from_yaml(f.name)

    def test_schema_file_exists(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        schema_path = repo_root / "schema" / "config.v1.schema.json"
        assert schema_path.exists()


def test_llm_config_accepts_openrouter_provider():
    """LLMConfig must accept provider='openrouter' as a recognized value."""
    from caretaker.config import LLMConfig

    config = LLMConfig(
        provider="openrouter", default_model="openrouter/anthropic/claude-sonnet-4.6"
    )
    assert config.provider == "openrouter"


def test_openrouter_provider_rejects_bare_default_model():
    """provider='openrouter' must reject default_model lacking the openrouter/ prefix."""
    from pydantic import ValidationError

    from caretaker.config import LLMConfig

    with pytest.raises(ValidationError, match="openrouter/"):
        LLMConfig(provider="openrouter", default_model="claude-sonnet-4-5")


def test_openrouter_provider_rejects_bare_feature_model():
    """provider='openrouter' must reject feature_models entries lacking the prefix."""
    from pydantic import ValidationError

    from caretaker.config import FeatureModelConfig, LLMConfig

    with pytest.raises(ValidationError, match="openrouter/"):
        LLMConfig(
            provider="openrouter",
            default_model="openrouter/anthropic/claude-sonnet-4.6",
            feature_models={
                "ci_log_analysis": FeatureModelConfig(model="claude-haiku-4-5"),
            },
        )


def test_openrouter_provider_rejects_bare_fallback_model():
    """provider='openrouter' must reject fallback_models entries lacking the prefix."""
    from pydantic import ValidationError

    from caretaker.config import LLMConfig

    with pytest.raises(ValidationError, match="openrouter/"):
        LLMConfig(
            provider="openrouter",
            default_model="openrouter/anthropic/claude-sonnet-4.6",
            fallback_models=["claude-haiku-4-5"],
        )


def test_openrouter_provider_accepts_all_prefixed_models():
    """A fully-prefixed OpenRouter config validates cleanly."""
    from caretaker.config import FeatureModelConfig, LLMConfig

    config = LLMConfig(
        provider="openrouter",
        default_model="openrouter/anthropic/claude-sonnet-4.6",
        feature_models={
            "ci_log_analysis": FeatureModelConfig(model="openrouter/deepseek/deepseek-r1"),
        },
        fallback_models=["openrouter/google/gemini-2.0-flash"],
    )
    assert config.provider == "openrouter"


def test_anthropic_provider_still_accepts_bare_models():
    """Strict prefix check must NOT apply when provider != openrouter."""
    from caretaker.config import LLMConfig

    config = LLMConfig(provider="anthropic", default_model="claude-sonnet-4-5")
    assert config.default_model == "claude-sonnet-4-5"


def test_litellm_provider_still_accepts_mixed_prefixes():
    """provider='litellm' must accept any prefix shape; only openrouter is strict."""
    from caretaker.config import LLMConfig

    config = LLMConfig(
        provider="litellm",
        default_model="azure_ai/gpt-4o",
        fallback_models=["claude-sonnet-4-5", "openai/gpt-4o-mini"],
    )
    assert config.provider == "litellm"


def test_openrouter_validator_lists_all_offenders():
    """The error message lists every offending field, not just the first."""
    from pydantic import ValidationError

    from caretaker.config import FeatureModelConfig, LLMConfig

    with pytest.raises(ValidationError) as exc_info:
        LLMConfig(
            provider="openrouter",
            default_model="claude-sonnet-4-5",  # offender 1
            feature_models={
                "ci_log_analysis": FeatureModelConfig(model="gpt-4o"),  # offender 2
            },
            fallback_models=["claude-haiku-4-5"],  # offender 3
        )
    msg = str(exc_info.value)
    assert "llm.default_model" in msg
    assert "llm.feature_models.ci_log_analysis.model" in msg
    assert "llm.fallback_models[0]" in msg
