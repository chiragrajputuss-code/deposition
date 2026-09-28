"""Exporter protocol.

An exporter receives already-sealed events. It must never mutate them: the hash
covers the event exactly as the chain built it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Exporter(Protocol):
    def export(self, event: dict[str, Any]) -> None:
        """Handle one sealed event. Must not raise into the host agent."""

    def flush(self, timeout: float = 5.0) -> None:
        """Block until buffered events are durable, or ``timeout`` elapses."""

    def close(self, timeout: float = 5.0) -> None:
        """Flush and release resources."""
