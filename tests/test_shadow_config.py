"""Tests for ``AgenticConfig`` + YAML round-trip."""

from __future__ import annotations

import tempfile

from caretaker.config import AgenticConfig, AgenticDomainConfig, MaintainerConfig


class TestAgenticConfigDefaults:
    def test_every_domain_defaults_to_off(self) -> None:
        cfg = AgenticConfig()
        domains = (
            "readiness",
            "ci_triage",
            "review_classification",
            "issue_triage",
            "cascade",
            "stuck_pr",
            "bot_identity",
            "dispatch_guard",
            "executor_routing",
            "crystallizer_category",
        )
        for name in domains:
            domain = getattr(cfg, name)
            assert isinstance(domain, AgenticDomainConfig)
            assert domain.mode == "off"

    def test_maintainer_config_embeds_agentic(self) -> None:
        cfg = MaintainerConfig()
        assert cfg.agentic.readiness.mode == "off"
        assert cfg.agentic.crystallizer_category.mode == "off"


class TestAgenticConfigYAMLRoundTrip:
    def test_readiness_shadow_loads(self) -> None:
        yaml_content = """
version: v1
agentic:
  readiness:
    mode: shadow
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            cfg = MaintainerConfig.from_yaml(f.name)

        assert cfg.agentic.readiness.mode == "shadow"
        # Other domains untouched.
        assert cfg.agentic.ci_triage.mode == "off"
        assert cfg.agentic.stuck_pr.mode == "off"

    def test_multiple_domains_load(self) -> None:
        # ``off`` is a YAML 1.1 boolean literal — quote it so pydantic
        # sees the string ``"off"`` rather than ``False``.
        yaml_content = """
version: v1
agentic:
  readiness:
    mode: shadow
  ci_triage:
    mode: enforce
  crystallizer_category:
    mode: "off"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            cfg = MaintainerConfig.from_yaml(f.name)

        assert cfg.agentic.readiness.mode == "shadow"
        assert cfg.agentic.ci_triage.mode == "enforce"
        assert cfg.agentic.crystallizer_category.mode == "off"

    def test_invalid_mode_raises(self) -> None:
        yaml_content = """
version: v1
agentic:
  readiness:
    mode: authoritative   # not a valid value
"""
        import pydantic

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                MaintainerConfig.from_yaml(f.name)
            except pydantic.ValidationError:
                pass
            else:
                raise AssertionError("expected ValidationError for unknown mode")
