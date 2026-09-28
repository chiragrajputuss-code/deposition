"""Hosted mode: batched upload to POST /api/v0/ingest.

Stub - Phase 2 (weeks 9-16). The contract is fixed now so the SDK's public API
does not change when it lands: gzip batches of sealed events, bearer token auth,
idempotent by ``(run_id, seq)``, retried with backoff on 5xx, dropped on 4xx
other than 402 (quota), which surfaces one clear warning.
"""

from __future__ import annotations

from typing import Any


class HttpExporter:
    DEFAULT_ENDPOINT = "https://api.deposition.dev/api/v0/ingest"

    def __init__(self, token: str, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self.token = token
        self.endpoint = endpoint
        raise NotImplementedError("hosted mode ships in Phase 2; use local mode for now")

    def export(self, event: dict[str, Any]) -> None:  # pragma: no cover - stub
        raise NotImplementedError

    def flush(self, timeout: float = 5.0) -> None:  # pragma: no cover - stub
        raise NotImplementedError

    def close(self, timeout: float = 5.0) -> None:  # pragma: no cover - stub
        raise NotImplementedError
