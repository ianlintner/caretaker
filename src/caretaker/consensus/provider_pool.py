"""Capability-tag → concrete model resolver.

Pool entries are operator-defined: tags like ``fast``, ``reasoning_anthropic``,
``reasoning_alt``, ``cheap`` map to concrete model strings the LLM router
understands. Per-site consensus config (``ConsensusDomainConfig.primary`` /
``escalation``) accepts either a tag or a literal model string. The pool
treats any value not present as a key as a literal and passes it through.
"""

from __future__ import annotations


class ProviderPoolError(ValueError):
    """Raised on invalid pool construction or impossible resolution."""


class ProviderPool:
    """Resolves capability tags to concrete model strings.

    The pool is a thin tag dictionary. Resolution is total: any value that
    isn't a known tag is returned unchanged on the assumption it's a literal
    model string — the LLM router validates literals at call time.
    """

    def __init__(self, pool: dict[str, str]) -> None:
        for tag, model in pool.items():
            if not isinstance(model, str) or not model:
                raise ProviderPoolError(
                    f"pool tag {tag!r} maps to invalid value {model!r}; "
                    "every tag must resolve to a non-empty model string"
                )
        # Defensive copy so downstream mutations don't bleed back.
        self._pool: dict[str, str] = dict(pool)

    def resolve(self, value: str) -> tuple[str, str]:
        """Return ``(concrete_model, tag_or_literal)``.

        ``tag_or_literal`` is the original input — used by ``ModelAttempt``
        to record what the operator typed, even when it was a literal.
        """
        if not value:
            raise ProviderPoolError("cannot resolve empty model reference")
        if value in self._pool:
            return self._pool[value], value
        # Treat as literal; LLM router validates at call time.
        return value, value

    def resolve_distinct(self, value: str, *, different_from: str) -> tuple[str, str]:
        """Resolve ``value``; raise when the result equals ``different_from``.

        Used by AlwaysTwoModels strategy to enforce that two voting models
        are concretely different. The caller has already resolved the first
        model (``different_from``); this guards the second slot.
        """
        model, tag = self.resolve(value)
        if model == different_from:
            raise ProviderPoolError(
                f"{value!r} resolves to {model!r} which equals different_from "
                f"({different_from!r}); two-model agreement requires distinct concrete models"
            )
        return model, tag


__all__ = ["ProviderPool", "ProviderPoolError"]
