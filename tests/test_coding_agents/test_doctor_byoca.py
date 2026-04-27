"""Doctor check for BYOCA executor / PR-reviewer config validity."""

from __future__ import annotations

from caretaker.config import (
    ExecutorConfig,
    MaintainerConfig,
    OpenCodeExecutorConfig,
    PRReviewerConfig,
)
from caretaker.doctor import Severity, check_coding_agent_config


def _maintainer_config(
    *,
    executor: ExecutorConfig,
    pr_reviewer: PRReviewerConfig | None = None,
) -> MaintainerConfig:
    cfg = MaintainerConfig()
    cfg.executor = executor
    if pr_reviewer is not None:
        cfg.pr_reviewer = pr_reviewer
    return cfg


def test_default_config_is_ok() -> None:
    rows = check_coding_agent_config(_maintainer_config(executor=ExecutorConfig()))
    assert all(r.severity == Severity.OK for r in rows)


def test_provider_set_but_disabled_warns() -> None:
    cfg = _maintainer_config(
        executor=ExecutorConfig(provider="opencode", opencode=OpenCodeExecutorConfig(enabled=False))
    )
    rows = check_coding_agent_config(cfg)
    assert any(r.severity == Severity.WARN and "enabled=False" in r.detail for r in rows)


def test_unknown_provider_warns() -> None:
    cfg = _maintainer_config(executor=ExecutorConfig(provider="hermes"))
    rows = check_coding_agent_config(cfg)
    assert any(r.severity == Severity.WARN and "does not match any" in r.detail for r in rows)


def test_unknown_complex_reviewer_warns() -> None:
    cfg = _maintainer_config(
        executor=ExecutorConfig(),
        pr_reviewer=PRReviewerConfig(complex_reviewer="hermes"),
    )
    rows = check_coding_agent_config(cfg)
    assert any(r.severity == Severity.WARN and "complex_reviewer" in r.detail for r in rows)


def test_opencode_enabled_is_ok() -> None:
    cfg = _maintainer_config(
        executor=ExecutorConfig(provider="opencode", opencode=OpenCodeExecutorConfig(enabled=True)),
        pr_reviewer=PRReviewerConfig(complex_reviewer="opencode"),
    )
    rows = check_coding_agent_config(cfg)
    assert all(r.severity == Severity.OK for r in rows)
