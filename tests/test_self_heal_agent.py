"""Tests for self-heal failure classification."""

from caretaker.self_heal_agent.agent import FailureKind, _classify_failure


def test_unknown_classification_uses_full_log_for_error_message() -> None:
    error_line = "2026-04-14T23:22:52.7183931Z ##[error]Process completed with exit code 1."
    root_dir = "/opt/hostedtoolcache/Python/3.12.13/x64"
    noisy_tail = "\n".join(
        [
            f"2026-04-14T23:22:53.2130099Z   Python_ROOT_DIR: {root_dir}",
            f"2026-04-14T23:22:53.2130495Z   Python2_ROOT_DIR: {root_dir}",
            f"2026-04-14T23:22:53.2130875Z   Python3_ROOT_DIR: {root_dir}",
        ]
    )
    log_text = f"{error_line}\n" + ("x" * 5000) + f"\n{noisy_tail}"

    kind, title, details = _classify_failure("maintain", log_text)

    assert kind == FailureKind.UNKNOWN
    assert "Process completed with exit code 1" in title
    assert "Process completed with exit code 1" in details
    assert "Python_ROOT_DIR" not in title
