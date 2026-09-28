"""Hash chain: build and verify.

Rules (frozen in ``docs/decisions/002-schema-v0.md``):

* An event's ``hash`` is ``SHA-256(canonical_json(event without "hash"))``.
  ``prev_hash`` is inside that input, which is what links the chain.
* ``run_start`` is ``seq`` 0 and its ``prev_hash`` is :data:`GENESIS_HASH`
  (64 zeros). Event ``seq`` 1 therefore carries the hash of the ``run_start``
  header, as the spec requires.
* ``seq`` increases by exactly 1 per event, with no gaps and no reordering.
* Verification replays the whole chain and fails loudly on any gap, edit or
  reorder, naming the first event where the trace stops being trustworthy.

This module is one of the two places where a bug destroys the product's
credibility. Every change here ships with tests in the same commit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .schema import (
    Event,
    EventType,
    SchemaError,
    canonical_bytes,
    now_ts,
    sha256_hex,
    validate_event,
)

__all__ = [
    "GENESIS_HASH",
    "ChainError",
    "ChainResult",
    "ChainVerificationError",
    "ChainBuilder",
    "event_hash",
    "seal",
    "verify",
    "verify_file",
    "read_jsonl",
]

#: ``prev_hash`` of the ``run_start`` header - the anchor of every chain.
GENESIS_HASH = "0" * 64


def event_hash(event: dict[str, Any]) -> str:
    """SHA-256 over the event's canonical JSON, excluding its own ``hash`` field."""
    payload = {k: v for k, v in event.items() if k != "hash"}
    if "prev_hash" not in payload:
        raise SchemaError("cannot hash an event with no prev_hash")
    return sha256_hex(canonical_bytes(payload))


def seal(event: Event | dict[str, Any], prev_hash: str) -> dict[str, Any]:
    """Attach ``prev_hash`` and ``hash`` to an event and return its dict form."""
    data = event.to_dict() if isinstance(event, Event) else dict(event)
    data.pop("hash", None)
    data["prev_hash"] = prev_hash
    validate_event(data, sealed=False)
    data["hash"] = event_hash(data)
    return data


@dataclass(frozen=True)
class ChainError:
    """One reason a trace cannot be trusted."""

    code: str
    message: str
    seq: int | None = None
    line: int | None = None

    def __str__(self) -> str:
        where = f"line {self.line}" if self.line is not None else "trace"
        if self.seq is not None:
            where += f" (seq {self.seq})"
        return f"{where}: {self.code} - {self.message}"


@dataclass
class ChainResult:
    """Outcome of verifying a trace."""

    ok: bool
    events_checked: int = 0
    run_id: str | None = None
    head_hash: str | None = None
    errors: list[ChainError] = field(default_factory=list)

    def raise_if_broken(self) -> None:
        if not self.ok:
            raise ChainVerificationError(self)

    def summary(self) -> str:
        if self.ok:
            return (
                f"OK  {self.events_checked} events verified"
                f"{f' for {self.run_id}' if self.run_id else ''}"
            )
        first = self.errors[0] if self.errors else None
        return f"BROKEN  {len(self.errors)} problem(s); first: {first}"


class ChainVerificationError(Exception):
    """Raised by :meth:`ChainResult.raise_if_broken`."""

    def __init__(self, result: ChainResult) -> None:
        super().__init__(result.summary())
        self.result = result


class ChainBuilder:
    """Builds a chain event by event.

    Not thread-safe by design: the SDK funnels every event through a single
    writer thread, so serialising here too would only hide ordering bugs.
    """

    def __init__(self, run_id: str, *, prev_hash: str = GENESIS_HASH, next_seq: int = 0) -> None:
        self.run_id = run_id
        self.prev_hash = prev_hash
        self.next_seq = next_seq

    @property
    def head_hash(self) -> str:
        return self.prev_hash

    def append(
        self,
        type: EventType | str,
        body: dict[str, Any] | None = None,
        *,
        caused_by: Iterable[int] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        """Seal one event onto the chain and return it."""
        event = Event(
            run_id=self.run_id,
            seq=self.next_seq,
            type=EventType(type),
            body=body or {},
            ts=ts or now_ts(),
            caused_by=[int(s) for s in caused_by] if caused_by else None,
        )
        sealed = seal(event, self.prev_hash)
        self.prev_hash = sealed["hash"]
        self.next_seq += 1
        return sealed


def read_jsonl(path: str | os.PathLike[str]) -> Iterator[tuple[int, Any]]:
    """Yield ``(line_number, parsed)`` for each non-empty line of a trace file.

    A line that is not valid JSON yields the raw string, so verification can
    report it rather than crash.
    """
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield lineno, json.loads(raw)
            except json.JSONDecodeError as exc:
                yield lineno, f"__unparseable__:{exc}"


def verify(events: Iterable[Any], *, lines: Iterable[int] | None = None) -> ChainResult:
    """Replay a chain and report every way it fails to hold together.

    Verification never raises on bad input: a malformed trace is a result with
    errors, not an exception. Only the caller decides that is fatal.
    """
    result = ChainResult(ok=True)
    expected_prev = GENESIS_HASH
    expected_seq = 0
    seen_run_end = False
    line_iter = iter(lines) if lines is not None else None

    def fail(code: str, message: str, seq: int | None = None, line: int | None = None) -> None:
        result.ok = False
        result.errors.append(ChainError(code=code, message=message, seq=seq, line=line))

    for index, event in enumerate(events):
        line = next(line_iter, None) if line_iter is not None else index + 1

        if isinstance(event, str) and event.startswith("__unparseable__:"):
            fail("unparseable", event.split(":", 1)[1].strip(), line=line)
            break

        try:
            validate_event(event, sealed=True)
        except SchemaError as exc:
            seq = event.get("seq") if isinstance(event, dict) else None
            fail("invalid_event", str(exc), seq=seq, line=line)
            break

        seq = event["seq"]
        result.events_checked += 1

        if result.run_id is None:
            result.run_id = event["run_id"]
            if event["type"] != EventType.RUN_START.value:
                fail("bad_header", f"first event is {event['type']!r}, expected 'run_start'",
                     seq=seq, line=line)
        elif event["run_id"] != result.run_id:
            fail("run_id_mismatch",
                 f"event belongs to run {event['run_id']!r}, not {result.run_id!r}",
                 seq=seq, line=line)
            break

        if seq != expected_seq:
            # A gap, a duplicate or a reorder - all of them mean the log is not
            # the log that was written.
            fail("seq_break", f"expected seq {expected_seq}, found {seq}", seq=seq, line=line)
            break

        if event["prev_hash"] != expected_prev:
            fail(
                "prev_hash_mismatch",
                f"prev_hash {event['prev_hash'][:12]}... does not match preceding "
                f"event's hash {expected_prev[:12]}...",
                seq=seq,
                line=line,
            )
            break

        recomputed = event_hash(event)
        if recomputed != event["hash"]:
            fail(
                "hash_mismatch",
                f"event content does not match its hash (recomputed {recomputed[:12]}..., "
                f"stored {event['hash'][:12]}...) - this event was edited after it was written",
                seq=seq,
                line=line,
            )
            break

        if seen_run_end:
            fail("events_after_run_end", "event recorded after run_end", seq=seq, line=line)
            break
        if event["type"] == EventType.RUN_END.value:
            seen_run_end = True

        for ref in event.get("caused_by") or []:
            if ref < 0 or ref >= seq:
                fail(
                    "bad_causality",
                    f"caused_by references seq {ref}, which is not an earlier event",
                    seq=seq,
                    line=line,
                )

        expected_prev = event["hash"]
        expected_seq = seq + 1
        result.head_hash = event["hash"]

    if result.events_checked == 0 and not result.errors:
        fail("empty_trace", "trace contains no events")

    return result


def verify_file(path: str | os.PathLike[str]) -> ChainResult:
    """Verify a ``.jsonl`` trace on disk."""
    pairs = list(read_jsonl(path))
    if not pairs:
        return ChainResult(
            ok=False, errors=[ChainError("empty_trace", f"{path} contains no events")]
        )
    lines = [lineno for lineno, _ in pairs]
    return verify([payload for _, payload in pairs], lines=lines)
