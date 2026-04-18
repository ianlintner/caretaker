"""caretaker: Autonomous GitHub repository maintenance powered by Copilot."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("caretaker")
except PackageNotFoundError:
    __version__ = "unknown"
