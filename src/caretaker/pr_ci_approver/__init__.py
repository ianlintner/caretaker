"""pr_ci_approver — surface and optionally auto-approve stuck bot CI runs.

GitHub Actions blocks workflow runs triggered by bot actors (Copilot,
dependabot, github-actions[bot]) with ``conclusion=action_required``
until an owner approves them via the Actions UI. Without intervention
these runs sit forever, silently stalling caretaker's review → merge
loop because PRs never go green.

This agent:
1. Lists recent workflow runs filtered to ``status=action_required``.
2. Filters to runs whose actor is on the ``allowed_actors`` whitelist
   AND whose event is in ``trigger_events``.
3. Surfaces stuck runs in the run summary (and the digest).
4. Optionally approves them via the REST API when
   ``config.pr_ci_approver.auto_approve = true``.

See ``docs/qa-findings-2026-04-23.md`` finding #7 for the motivating
scenario.
"""

from caretaker.pr_ci_approver.agent import PRCIApproverAgent

__all__ = ["PRCIApproverAgent"]
