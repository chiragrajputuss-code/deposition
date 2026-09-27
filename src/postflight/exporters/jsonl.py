"""Local mode: append-only ``.jsonl`` trace plus content-addressed blobs.

Writing happens on a single background thread. The host agent enqueues and
returns; a slow or broken disk must never become the agent's problem.
"""

from __future__ import annotations

import os
import queue
import threading
from pathlib import Path
from typing import Any

from ..schema import (
    BLOB_THRESHOLD_BYTES,
    blob_ref,
    canonical_bytes,
    canonical_json,
    sha256_hex,
)

_SENTINEL = object()


class JsonlExporter:
    """Appends sealed events to ``<dir>/<run_id>.jsonl``.

    Oversized values inside ``body`` are replaced by blob references and written
    to ``<dir>/blobs/<sha256>`` - it keeps traces scannable and de-duplicates the
    repeated context that dominates agent runs.
    """

    def __init__(
        self,
        run_id: str,
        directory: str | os.PathLike[str] = "./postflight",
        *,
        blob_threshold: int = BLOB_THRESHOLD_BYTES,
        queue_size: int = 10_000,
    ) -> None:
        self.directory = Path(directory)
        self.blob_dir = self.directory / "blobs"
        self.path = self.directory / f"{run_id}.jsonl"
        self.blob_threshold = blob_threshold
        self.dropped = 0

        self.directory.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(
            target=self._run, name=f"postflight-writer-{run_id}", daemon=True
        )
        self._thread.start()

    # -- public API ---------------------------------------------------------

    def export(self, event: dict[str, Any]) -> None:
        try:
            self._idle.clear()
            self._queue.put_nowait(event)
        except queue.Full:
            # Losing a trace is bad; stalling the user's agent is worse.
            self.dropped += 1
            self._idle.set()

    def flush(self, timeout: float = 5.0) -> None:
        self._idle.wait(timeout)
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except (OSError, ValueError):
            pass

    def close(self, timeout: float = 5.0) -> None:
        self._queue.put(_SENTINEL)
        self._thread.join(timeout)
        try:
            self._fh.flush()
            self._fh.close()
        except (OSError, ValueError):
            pass

    # -- writer thread ------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                self._idle.set()
                return
            try:
                self._write(item)
            except Exception:  # noqa: BLE001 - never surface into the host agent
                self.dropped += 1
            finally:
                if self._queue.empty():
                    self._idle.set()

    def _write(self, event: dict[str, Any]) -> None:
        self._fh.write(canonical_json(event) + "\n")
        self._fh.flush()

    # -- blob externalisation ----------------------------------------------

    def externalize(self, value: Any) -> Any:
        """Replace oversized leaves with blob refs, writing the blobs to disk.

        Called *before* the event is sealed - the hash must cover the reference
        that is actually stored, not the payload it stands in for.
        """
        if isinstance(value, dict):
            if "$blob" in value:
                return value
            return {k: self.externalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.externalize(v) for v in value]
        if isinstance(value, str) and len(value.encode("utf-8")) > self.blob_threshold:
            return self._store_blob(value.encode("utf-8"), encoding="utf-8")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return value

    def externalize_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """Externalise a whole body, including the case where it is large as a whole."""
        body = {k: self.externalize(v) for k, v in body.items()}
        if len(canonical_bytes(body)) > self.blob_threshold:
            for key in sorted(body, key=lambda k: len(canonical_bytes(body[k])), reverse=True):
                if len(canonical_bytes(body)) <= self.blob_threshold:
                    break
                if not isinstance(body[key], dict) or "$blob" not in body[key]:
                    body[key] = self._store_blob(canonical_bytes(body[key]), encoding="json")
        return body

    def _store_blob(self, data: bytes, *, encoding: str) -> dict[str, Any]:
        digest = sha256_hex(data)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        target = self.blob_dir / digest
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)  # atomic: a reader never sees a half-written blob
        return blob_ref(digest, len(data), encoding)
