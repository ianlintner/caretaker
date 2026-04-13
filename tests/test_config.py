"""Tests for configuration loading."""

from __future__ import annotations

import tempfile
from pathlib import Path

from project_maintainer.config import MaintainerConfig


class TestMaintainerConfig:
    def test_defaults(self) -> None:
        config = MaintainerConfig()
        assert config.version == "v1"
        assert config.pr_agent.enabled is True
        assert config.pr_agent.auto_merge.copilot_prs is True
        assert config.pr_agent.auto_merge.dependabot_prs is True
        assert config.pr_agent.auto_merge.human_prs is False
        assert config.pr_agent.copilot.max_retries == 2
        assert config.issue_agent.enabled is True
        assert config.issue_agent.auto_assign_bugs is True
        assert config.issue_agent.auto_assign_features is False
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
issue_agent:
  auto_assign_features: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = MaintainerConfig.from_yaml(f.name)

        assert config.pr_agent.auto_merge.copilot_prs is False
        assert config.pr_agent.auto_merge.merge_method == "merge"
        assert config.pr_agent.copilot.max_retries == 3
        assert config.issue_agent.auto_assign_features is True
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
