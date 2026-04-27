"""Bootstrap Agent — auto-scaffolds caretaker setup files in newly-installed repos.

When the caretaker GitHub App is installed on a new repo (or an existing
installation gains access to a new repo), this agent opens a setup PR
that drops the thin streaming workflow, default config, agent persona
files, version pin, and copilot-instructions append — and sets the
``CARETAKER_BACKEND_URL`` repository variable. The repo owner reviews
and merges; from that point forward, caretaker operates on the repo via
webhooks.

Replaces the legacy SETUP_AGENT.md flow that required ``@copilot`` to
scaffold each repo manually.
"""

from caretaker.bootstrap_agent.adapter import BootstrapAgentAdapter
from caretaker.bootstrap_agent.agent import BootstrapAgent

__all__ = ["BootstrapAgent", "BootstrapAgentAdapter"]
