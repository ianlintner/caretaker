"""MemoryBackend implementations for caretaker agent state.

Phase 1 of the Azure backend adoption roadmap introduces a pluggable
``MemoryBackend`` protocol so the orchestrator can swap between storage
back-ends without changing any agent code.

Available back-ends:
    sqlite   — default; zero dependency; single-process SQLite.
               Works in GitHub Actions with ``actions/cache``.
    postgres — Neon / Supabase / any standard Postgres SaaS (Phase 1).
               Enabled via config: memory_store.backend = "postgres"

See docs/azure-backend-adoption-roadmap.md § Phase 1.
"""

from caretaker.state.backends.base import MemoryBackend
from caretaker.state.backends.factory import build_memory_backend
from caretaker.state.backends.sqlite_backend import SQLiteMemoryBackend

__all__ = ["MemoryBackend", "SQLiteMemoryBackend", "build_memory_backend"]
