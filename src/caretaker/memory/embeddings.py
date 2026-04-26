"""Embedder protocol + LiteLLM-backed implementation — T-E2 Part C.

Introduced as a thin seam so :class:`caretaker.memory.retriever.MemoryRetriever`
can rank cross-run memory hits by cosine similarity without pinning us to a
specific embedding provider. The protocol is deliberately minimal:

* One method: ``embed(text)`` returning a dense ``list[float]`` vector.
* Async so callers can fan out retrieval concurrently with the main LLM call.
* No batching API yet — retrieval budgets are tiny (top-3 across a few
  hundred nodes at most), and ranking latency dominates the cost picture.
  We can add ``embed_many`` later without breaking existing implementations.

The :class:`LiteLLMEmbedder` is an opt-in concrete backend that dispatches to
``litellm.aembedding``. It fails closed — if LiteLLM is not installed or no
embedding model is configured the embedder reports ``available = False`` and
callers can fall through to the Jaccard word-overlap heuristic in the
retriever. That keeps the "ship Jaccard-only" fallback path alive for
installs that haven't wired embeddings into their LLMConfig.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from caretaker.config import LLMConfig

logger = logging.getLogger(__name__)


@runtime_checkable
class Embedder(Protocol):
    """Minimal surface for dense text embeddings.

    The retriever only needs vector output for cosine similarity; any
    provider that can produce a ``list[float]`` of a stable dimension
    satisfies the protocol. Callers are responsible for choosing a model
    that returns a consistent vector width across calls.
    """

    @property
    def available(self) -> bool:
        """Whether the embedder can service a request right now.

        Implementations should fail open: missing credentials or missing
        optional packages must return ``False`` rather than raising, so
        callers can fall back to Jaccard ranking without a try/except
        wrapper on every call.
        """

    async def embed(self, text: str) -> list[float]:
        """Produce a dense embedding for ``text``.

        May return an empty list when ``available`` is False. Callers
        that treat empty vectors as "fall back to Jaccard" get a safe
        degradation path on provider outages.
        """


class LiteLLMEmbedder:
    """LiteLLM-backed :class:`Embedder`.

    Dispatches to ``litellm.aembedding`` so operators can point at any of
    the embedding endpoints LiteLLM supports (OpenAI ``text-embedding-*``,
    Voyage, Cohere, Vertex, Bedrock, Azure OpenAI, Ollama, ...). The model
    string is passed through verbatim; if no model is configured we
    default to ``text-embedding-3-small`` — the cheapest widely-available
    option and a reasonable baseline for short caretaker summaries.

    The embedder is always constructible so the retriever can reach for
    it unconditionally, but ``available`` stays False when either the
    LiteLLM package is missing or no obvious provider credential is set.
    The retriever falls back to Jaccard in that case.
    """

    name = "litellm"

    _DEFAULT_MODEL = "text-embedding-3-small"

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._model = model or self._DEFAULT_MODEL
        self._timeout = timeout
        self._aembedding: Any = None
        try:
            from litellm import aembedding

            self._aembedding = aembedding
        except ImportError as exc:  # pragma: no cover - optional dep path
            logger.debug("LiteLLM not installed for embeddings: %s", exc)

    @classmethod
    def from_config(cls, config: LLMConfig) -> LiteLLMEmbedder:
        """Build a LiteLLMEmbedder using the main :class:`LLMConfig` timeout.

        We deliberately do not pull a model name out of ``LLMConfig`` —
        the Phase 2 config schema does not yet carry an
        ``embedding_model`` field. Operators who want a specific model
        can construct ``LiteLLMEmbedder(model=...)`` directly when wiring
        the retriever. This helper keeps the default path aligned with
        the main LLM's timeout so retrieval shares the same SLO.
        """
        return cls(timeout=config.timeout_seconds)

    @property
    def available(self) -> bool:
        if self._aembedding is None:
            return False
        # Mirror :class:`caretaker.llm.provider.LiteLLMProvider.available`:
        # at least one provider credential must be present so callers can
        # fail-open without surfacing an auth error on every dispatch.
        import os

        return any(
            os.environ.get(key)
            for key in (
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "AZURE_API_KEY",
                "AZURE_AI_API_KEY",
                "COHERE_API_KEY",
                "VOYAGE_API_KEY",
                "VERTEX_PROJECT",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "AWS_ACCESS_KEY_ID",
                "MISTRAL_API_KEY",
                "OLLAMA_API_BASE",
            )
        )

    async def embed(self, text: str) -> list[float]:
        if not self.available or self._aembedding is None:
            return []
        try:
            response = await self._aembedding(
                model=self._model,
                input=[text],
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - fail open on provider errors
            logger.info("LiteLLMEmbedder.embed failed for model=%s: %s", self._model, exc)
            return []

        # LiteLLM's EmbeddingResponse exposes ``data = [{"embedding": [...]}, ...]``.
        data = getattr(response, "data", None) or []
        if not data:
            return []
        first = data[0]
        if isinstance(first, dict):
            vector = first.get("embedding")
        else:
            vector = getattr(first, "embedding", None)
        if not isinstance(vector, list):
            return []
        # Normalise to plain floats so callers never round-trip Decimal or
        # numpy scalars through the graph writer.
        return [float(v) for v in vector]


__all__ = ["Embedder", "LiteLLMEmbedder"]
