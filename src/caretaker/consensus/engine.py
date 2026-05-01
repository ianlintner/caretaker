"""ConsensusEngine — public API for tiered-consensus decisions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypeVar

from caretaker.consensus.metrics import CONSENSUS_DECISION_SECONDS
from caretaker.consensus.strategies import (
    AlwaysTwoModels,
    Strategy,
    StrategyContext,
    TieredConfidence,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from caretaker.consensus.provider_pool import ProviderPool
    from caretaker.consensus.result import ConsensusResult
    from caretaker.llm.claude import ClaudeClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseModel")

StrategyName = Literal["tiered_confidence", "always_two_models"]


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """Per-decision-site engine configuration.

    Built once per orchestrator startup from
    :class:`~caretaker.config.ConsensusDomainConfig` and stored on
    :class:`EngineConfig.sites` keyed by site name.
    """

    strategy: StrategyName
    primary: str
    escalation: list[str]
    confidence_threshold: float
    agreement_fields: list[str]


@dataclass(frozen=True)
class EngineConfig:
    """Top-level engine configuration."""

    pool: ProviderPool
    sites: dict[str, SiteConfig] = field(default_factory=dict)


# Registry — maps strategy name → constructor. Adding a new strategy is
# (a) implement it in strategies.py, (b) add an entry here.
_STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "tiered_confidence": TieredConfidence,
    "always_two_models": AlwaysTwoModels,
}


class ConsensusEngine:
    """Routes a decision to the configured strategy and returns the verdict."""

    def __init__(self, *, config: EngineConfig, claude: ClaudeClient) -> None:
        self._config = config
        self._claude = claude

    def has_site(self, name: str) -> bool:
        """Return True when ``name`` is a configured decision site.

        Call sites that wire the engine into existing single-model paths
        check this before delegating, so they stay on the legacy direct
        path when the engine is configured but not for their site.
        """
        return name in self._config.sites

    def site_names(self) -> frozenset[str]:
        """Return the names of all configured decision sites.

        Public accessor used by the orchestrator's startup log line so
        callers don't need to read the private ``_config`` attribute.
        """
        return frozenset(self._config.sites)

    async def decide(
        self,
        *,
        site_name: str,
        schema: type[T],
        system_prompt: str,
        user_prompt: str,
        feature: str,
        max_tokens: int = 2000,
    ) -> ConsensusResult[T]:
        """Run the configured strategy for ``site_name`` and return the result.

        Raises:
            KeyError: ``site_name`` is not configured in :attr:`EngineConfig.sites`.
            ValueError: configured strategy name is unknown.
            ConsensusUnavailable: every model attempt errored.
        """
        try:
            site = self._config.sites[site_name]
        except KeyError as exc:
            raise KeyError(
                f"consensus engine has no configuration for site {site_name!r}; "
                f"known sites: {sorted(self._config.sites)}"
            ) from exc

        strategy_cls = _STRATEGY_REGISTRY.get(site.strategy)
        if strategy_cls is None:
            raise ValueError(
                f"unknown consensus strategy {site.strategy!r} for site {site_name!r}; "
                f"known strategies: {sorted(_STRATEGY_REGISTRY)}"
            )

        ctx = StrategyContext(
            site_name=site_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            feature=feature,
            primary=site.primary,
            escalation=site.escalation,
            confidence_threshold=site.confidence_threshold,
            agreement_fields=site.agreement_fields,
            pool=self._config.pool,
            claude=self._claude,
            max_tokens=max_tokens,
        )
        with CONSENSUS_DECISION_SECONDS.labels(site=site_name, strategy=site.strategy).time():
            result: ConsensusResult[T] = await strategy_cls().run(ctx)
        # Stamp the trace onto the contextvar so the wrapping
        # ``@shadow_decision`` decorator can serialise it onto the
        # ShadowDecisionRecord — see consensus.trace_context.
        from caretaker.consensus.trace_context import current_trace_var

        current_trace_var.set(result.trace)
        return result


__all__ = [
    "ConsensusEngine",
    "EngineConfig",
    "SiteConfig",
    "StrategyName",
]
