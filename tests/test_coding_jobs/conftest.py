"""Shared fixtures for coding-jobs tests.

``azure-servicebus`` is an optional extra (``pip install caretaker[asb]``) and
is not present in the standard dev environment.  The tests that exercise
``AsbCodingQueue`` fully mock the ASB *client* and *sender*, but
``enqueue``/``schedule_retry`` still do a deferred
``from azure.servicebus import ServiceBusMessage`` that would blow up without
the real package.

This conftest installs a lightweight stub for ``azure.servicebus`` before any
test in this directory is collected, so those tests can run without the extra.
The stub ``ServiceBusMessage`` records the kwargs it was constructed with and
exposes ``body`` as ``[body_bytes]`` — a single-element list — which matches
the real SDK's chunked-body interface that the tests consume via
``b"".join(sent.body)``.
"""

from __future__ import annotations

import sys
import types


def _install_azure_servicebus_stub() -> None:
    """Inject a minimal azure.servicebus stub into sys.modules."""
    if "azure.servicebus" in sys.modules:
        return  # real package (or another stub) already present

    class ServiceBusMessage:  # noqa: D101
        def __init__(
            self,
            body: bytes,
            *,
            message_id: str = "",
            time_to_live=None,
            application_properties: dict | None = None,
        ) -> None:
            # Expose body as a one-element list so b"".join(msg.body) works.
            self.body = [body]
            self.message_id = message_id
            self.time_to_live = time_to_live
            self.application_properties = application_properties or {}

    # Build minimal module hierarchy: azure → azure.servicebus
    azure_mod = sys.modules.get("azure") or types.ModuleType("azure")
    azure_mod.__path__ = []  # mark as namespace package
    asb_mod = types.ModuleType("azure.servicebus")
    asb_mod.ServiceBusMessage = ServiceBusMessage  # type: ignore[attr-defined]

    sys.modules.setdefault("azure", azure_mod)
    sys.modules["azure.servicebus"] = asb_mod
    azure_mod.servicebus = asb_mod  # type: ignore[attr-defined]


_install_azure_servicebus_stub()
