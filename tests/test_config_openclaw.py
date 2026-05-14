# tests/test_config_openclaw.py
from caretaker.config import AutoFixConfig, OpenclaWHttpConfig, PRReviewerConfig


def test_openclaw_http_config_defaults() -> None:
    cfg = OpenclaWHttpConfig()
    assert cfg.enabled is False
    assert cfg.base_url == ""
    assert cfg.api_key == ""
    assert cfg.model == "openclaw/default"
    assert cfg.timeout_seconds == 300
    assert cfg.keep_workdir_on_failure is False


def test_pr_reviewer_config_has_openclaw_http_field() -> None:
    cfg = PRReviewerConfig()
    assert hasattr(cfg, "openclaw_http")
    assert isinstance(cfg.openclaw_http, OpenclaWHttpConfig)


def test_auto_fix_config_has_pre_escalation_agent_field() -> None:
    cfg = AutoFixConfig()
    assert cfg.pre_escalation_agent == ""


def test_auto_fix_config_pre_escalation_agent_roundtrip() -> None:
    cfg = AutoFixConfig(pre_escalation_agent="openclaw_http")
    assert cfg.pre_escalation_agent == "openclaw_http"
