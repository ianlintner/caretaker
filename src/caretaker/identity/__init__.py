"""Single source of truth for bot-login identity detection.

Public API:

* :func:`is_automated` — synchronous deterministic allowlist check.
* :func:`classify_identity` — async classifier with optional LLM fallback.
* :class:`BotIdentity` — the structured verdict returned by the classifier.
"""

from caretaker.identity.bot import (
    BotFamily,
    BotIdentity,
    classify_identity,
    deterministic_family,
    is_automated,
)

__all__ = [
    "BotFamily",
    "BotIdentity",
    "classify_identity",
    "deterministic_family",
    "is_automated",
]
