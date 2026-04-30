"""Backward-compat shim — webhook dedup lives in ``state.webhook_dedup`` now.

Import from :mod:`caretaker.state.webhook_dedup` directly:

    from caretaker.state.webhook_dedup import LocalDedup, RedisDedup, build_dedup
"""

from caretaker.state.webhook_dedup import LocalDedup, RedisDedup, build_dedup

__all__ = ["LocalDedup", "RedisDedup", "build_dedup"]
