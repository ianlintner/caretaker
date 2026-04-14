"""Log analyzer for CI build failures — extracts structured root-cause info."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Maximum log bytes to analyze (avoid sending huge logs to LLM)
MAX_LOG_BYTES = 16_000

# Patterns that indicate "interesting" failure lines worth surfacing
_FAILURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(error|Error|ERROR)\s*:", re.MULTILINE),
    re.compile(r"(FAILED|FAIL)\s+tests/", re.MULTILINE),
    re.compile(r"AssertionError", re.MULTILINE),
    re.compile(r"ImportError|ModuleNotFoundError", re.MULTILINE),
    re.compile(r"SyntaxError", re.MULTILINE),
    re.compile(r"exit code [1-9]", re.MULTILINE),
    re.compile(r"Process completed with exit code [1-9]", re.MULTILINE),
    re.compile(r"ruff|mypy|pytest", re.MULTILINE),
]

_PYTHON_FILE_PATTERN = re.compile(r"([\w/_\-]+\.py)(?::(\d+))?")


@dataclass
class FailureSummary:
    """Structured summary of a CI job failure."""

    job_name: str
    conclusion: str  # "failure" | "timed_out" etc.
    log_snippet: str = ""
    suspected_files: list[str] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)
    category: str = "unknown"  # "test_failure" | "lint" | "type_error" | "import" | "unknown"

    def to_markdown(self) -> str:
        files_md = "\n".join(f"- `{f}`" for f in self.suspected_files) or "_none identified_"
        errors_md = "\n".join(f"```\n{e}\n```" for e in self.error_lines[:5]) or "_see log snippet_"
        return (
            f"**Job:** `{self.job_name}` — conclusion: `{self.conclusion}`\n\n"
            f"**Category:** {self.category}\n\n"
            f"**Suspected files:**\n{files_md}\n\n"
            f"**Error lines:**\n{errors_md}\n\n"
            f"<details><summary>Log snippet</summary>\n\n```\n{self.log_snippet[:4000]}\n```\n\n</details>"  # noqa: E501
        )


def analyze_job_log(job_name: str, conclusion: str, raw_log: str) -> FailureSummary:
    """Parse raw CI job log and return a structured FailureSummary."""
    # Trim log to manageable size (tail — failures are usually at the end)
    log = raw_log[-MAX_LOG_BYTES:] if len(raw_log.encode()) > MAX_LOG_BYTES else raw_log

    error_lines: list[str] = []
    for pattern in _FAILURE_PATTERNS:
        for match in pattern.finditer(log):
            start = max(0, match.start() - 80)
            end = min(len(log), match.end() + 200)
            snippet = log[start:end].strip()
            if snippet and snippet not in error_lines:
                error_lines.append(snippet)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_errors: list[str] = []
    for e in error_lines:
        key = e[:120]
        if key not in seen:
            seen.add(key)
            unique_errors.append(e)

    # Extract python file paths mentioned in failures
    suspected_files: list[str] = []
    for m in _PYTHON_FILE_PATTERN.finditer(log):
        fname = m.group(1)
        if fname.startswith(("src/", "tests/")) and fname not in suspected_files:
            suspected_files.append(fname)

    # Determine category
    category = _categorize(job_name, log)

    # Build a short snippet — last N lines with error context
    lines = log.splitlines()
    snippet_lines = [ln for ln in lines if any(p.search(ln) for p in _FAILURE_PATTERNS)]
    log_snippet = "\n".join(snippet_lines[-60:]) if snippet_lines else "\n".join(lines[-60:])

    return FailureSummary(
        job_name=job_name,
        conclusion=conclusion,
        log_snippet=log_snippet,
        suspected_files=suspected_files[:10],
        error_lines=unique_errors[:10],
        category=category,
    )


def _categorize(job_name: str, log: str) -> str:
    name_lower = job_name.lower()
    if "lint" in name_lower or "ruff" in log[:2000]:
        return "lint"
    if "mypy" in name_lower or "mypy" in log[:2000]:
        return "type_error"
    if "test" in name_lower or "pytest" in log[:2000]:
        return "test_failure"
    if "ImportError" in log or "ModuleNotFoundError" in log:
        return "import_error"
    return "unknown"
