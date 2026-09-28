"""Optional OpenTelemetry bridge.

Stub - Phase 4. Schema v0 already mirrors the OTel GenAI semantic conventions
(``gen_ai.request.model``, token-usage attributes), so this maps events to spans
without reshaping anything. Requires the ``otel`` extra.
"""

from __future__ import annotations

from typing import Any


class OtelExporter:
    def __init__(self) -> None:
        raise NotImplementedError("the OpenTelemetry bridge ships in Phase 4")

    def export(self, event: dict[str, Any]) -> None:  # pragma: no cover - stub
        raise NotImplementedError

    def flush(self, timeout: float = 5.0) -> None:  # pragma: no cover - stub
        raise NotImplementedError

    def close(self, timeout: float = 5.0) -> None:  # pragma: no cover - stub
        raise NotImplementedError
