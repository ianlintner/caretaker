"""Pluggable third-party PR-reviewer backends.

Each module in this package exposes a module-level ``SPEC`` of type
:class:`HandoffReviewerSpec` that ``handoff_reviewer._build_specs``
registers at import time. ``local_subprocess`` backends additionally
expose an async ``run`` callable wired into ``SPEC.runner``.

Adding a backend:

1. Create ``my_tool.py`` with a ``SPEC`` and (for local_subprocess) a
   ``run(...)`` coroutine returning a ``ReviewResult``.
2. Append the import to ``handoff_reviewer._build_specs``.
3. Add a config block + label/mention fields if comment-triggered.
4. Add the backend name to ``PRReviewerConfig.enabled_backends`` (or
   leave it out so operators must opt in).
"""
