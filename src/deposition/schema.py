"""Trace schema v0 - the foundation of the product.

One run = one append-only event log. Each line of a ``.jsonl`` trace is one
event. The SDK writes this schema, the viewer renders it, the platform indexes
it, and the hash chain (see :mod:`deposition.chain`) guarantees it.

Field names mirror the OpenTelemetry GenAI semantic conventions wherever they
overlap (``gen_ai.request.model``, token-usage attributes) so an OTel bridge
stays cheap - see ``docs/decisions/002-schema-v0.md``.

Nothing in this module may depend on anything outside the standard library.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "BLOB_THRESHOLD_BYTES",
    "EventType",
    "Event",
    "SchemaError",
    "canonical_json",
    "canonical_bytes",
    "sha256_hex",
    "now_ts",
    "validate_event",
    "blob_ref",
    "is_blob_ref",
]

#: Bumped on every schema change, with a migration note in ``docs/decisions/``.
SCHEMA_VERSION = "1.0"

#: Versions this SDK can verify. The writer only ever emits the newest; the
#: reader accepts every version that ever existed, because a record that its
#: own tool can no longer check is the one outcome an evidence product must
#: never produce (ADR 008).
SUPPORTED_VERSIONS = ("0.1", "1.0")

#: Bodies larger than this are externalised as content-addressed blobs.
BLOB_THRESHOLD_BYTES = 64 * 1024

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_TS = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})\Z")

#: Envelope keys, in the order they are written for human readability. The hash
#: is computed over canonical (key-sorted) JSON, so this order is cosmetic only.
ENVELOPE_KEYS = ("v", "run_id", "seq", "ts", "type", "body", "caused_by", "prev_hash", "hash")


class SchemaError(ValueError):
    """Raised when an event does not conform to trace schema v0."""


class EventType(str, Enum):
    """Event types defined by schema v0."""

    RUN_START = "run_start"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    DECISION = "decision"
    RETRIEVAL = "retrieval"
    ERROR = "error"
    ANNOTATION = "annotation"
    RUN_END = "run_end"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: What each event type's ``body`` is expected to carry. Advisory in v0: unknown
#: keys are allowed so instrumentation can move faster than the schema, but the
#: viewer and platform only promise to render these.
BODY_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.RUN_START: ("agent", "sdk_version", "env", "config"),
    EventType.LLM_CALL: (
        "provider",
        "gen_ai.request.model",
        "request",
        "messages",
        "response",
        "usage",
        "latency_ms",
        "cost_usd",
    ),
    EventType.TOOL_CALL: ("tool", "arguments", "result", "error", "latency_ms"),
    EventType.DECISION: ("label", "detail"),
    EventType.RETRIEVAL: ("query", "source", "doc_ids"),
    EventType.ERROR: ("exception", "message", "stack"),
    EventType.ANNOTATION: ("note", "author"),
    EventType.RUN_END: ("status", "totals"),
}


def canonical_json(obj: Any, *, version: str = SCHEMA_VERSION) -> str:
    """Canonical JSON: the exact byte-level contract the hash chain depends on.

    v1.0 is RFC 8785 (JCS): numbers serialise the way every JavaScript engine
    prints them, keys sort by UTF-16 code unit, so a verifier in any language
    reproduces the same bytes and therefore the same hash. v0.1 was Python's
    ``json.dumps(sort_keys=True)``, whose float formatting no other runtime can
    reproduce - ADR 008 records the measurement that forced the change. The old
    serialiser is kept forever, selected by each event's own ``v`` field, so no
    existing trace ever becomes unverifiable by its own tool.
    """
    if version == "0.1":
        return json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    return _jcs(obj)


def canonical_bytes(obj: Any, *, version: str = SCHEMA_VERSION) -> bytes:
    return canonical_json(obj, version=version).encode("utf-8")


# -- RFC 8785 ----------------------------------------------------------------


def _decimal_parts(value: float) -> tuple[str, int]:
    """Shortest round-trip digits of ``abs(value)`` and its decimal position.

    Python's ``repr`` and ECMAScript's ToString both emit the shortest decimal
    that uniquely names the double, so the digit sequences agree; only the
    *formatting* differs, which :func:`_es_number` applies.
    """
    text = repr(value)
    mantissa, _, exponent = text.lower().partition("e")
    shift = int(exponent) if exponent else 0
    int_part, _, frac_part = mantissa.partition(".")
    digits = (int_part + frac_part).lstrip("0")
    if int_part.lstrip("0"):
        position = len(int_part) + shift
    else:
        position = -(len(frac_part) - len(frac_part.lstrip("0"))) + shift
    return digits.rstrip("0") or "0", position


def _es_number(value: float) -> str:
    """A finite float, printed exactly as ECMAScript ``Number::toString`` prints it.

    The rules (ECMA-262 ss 6.1.6.1.20, via RFC 8785 ss 3.2.2.3): plain notation
    within [1e-6, 1e21), exponent notation outside it, no leading zero on the
    exponent, and negative zero prints as ``0`` - which is also what
    ``JSON.stringify`` does, and not what Python does.
    """
    if value != value or value in (float("inf"), float("-inf")):
        raise SchemaError("canonical JSON forbids NaN and Infinity")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits, position = _decimal_parts(abs(value))
    count = len(digits)
    if count <= position <= 21:
        body = digits + "0" * (position - count)
    elif 0 < position <= 21:
        body = digits[:position] + "." + digits[position:]
    elif -6 < position <= 0:
        body = "0." + "0" * (-position) + digits
    else:
        exponent = position - 1
        head = digits[0] + ("." + digits[1:] if count > 1 else "")
        body = f"{head}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"
    return sign + body


def _utf16_key(key: str) -> bytes:
    # JCS sorts keys by UTF-16 code unit; big-endian bytes compare identically.
    return key.encode("utf-16-be")


def _jcs(obj: Any) -> str:
    """RFC 8785 serialisation. ``bool`` is checked before ``int`` deliberately.

    Integers serialise exactly at any size. Beyond 2**53 that exceeds what a
    JavaScript verifier can parse losslessly (I-JSON's interoperable range), so
    the schema keeps its own counters well inside it.
    """
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if obj is None:
        return "null"
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return _es_number(obj)
    if isinstance(obj, dict):
        pairs = sorted(((str(k), v) for k, v in obj.items()), key=lambda kv: _utf16_key(kv[0]))
        return (
            "{"
            + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _jcs(v) for k, v in pairs)
            + "}"
        )
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_jcs(v) for v in obj) + "]"
    raise SchemaError(f"not JSON-serialisable: {type(obj).__name__}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_ts() -> str:
    """RFC 3339 / ISO 8601 timestamp in UTC with millisecond precision."""
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond // 1000:03d}Z"


def blob_ref(sha256: str, size: int, encoding: str = "utf-8") -> dict[str, Any]:
    """A reference replacing an oversized payload, stored at ``blobs/<sha256>``."""
    return {"$blob": sha256, "size": size, "encoding": encoding}


def is_blob_ref(value: Any) -> bool:
    return isinstance(value, dict) and "$blob" in value and len(value) <= 3


@dataclass
class Event:
    """One line of a trace.

    ``prev_hash`` and ``hash`` are filled by :mod:`deposition.chain` when the
    event is sealed; they are ``None`` on an event that has not been chained yet.
    """

    run_id: str
    seq: int
    type: EventType
    body: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_ts)
    v: str = SCHEMA_VERSION
    caused_by: list[int] | None = None
    prev_hash: str | None = None
    hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Dict form, omitting unset optional fields so they never enter the hash."""
        out: dict[str, Any] = {
            "v": self.v,
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "type": str(self.type),
            "body": self.body,
        }
        if self.caused_by:
            out["caused_by"] = list(self.caused_by)
        if self.prev_hash is not None:
            out["prev_hash"] = self.prev_hash
        if self.hash is not None:
            out["hash"] = self.hash
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        validate_event(data, sealed=False)
        return cls(
            run_id=data["run_id"],
            seq=data["seq"],
            type=EventType(data["type"]),
            body=data.get("body", {}),
            ts=data["ts"],
            v=data["v"],
            caused_by=list(data["caused_by"]) if data.get("caused_by") else None,
            prev_hash=data.get("prev_hash"),
            hash=data.get("hash"),
        )

    def to_json_line(self) -> str:
        """The trace file stores canonical JSON, so a line is its own hash input."""
        return canonical_json(self.to_dict())


def validate_event(data: Any, *, sealed: bool = True) -> None:
    """Validate one event dict against schema v0.

    ``sealed=True`` additionally requires ``prev_hash`` and ``hash`` to be
    present and well-formed - the state every event in a written trace is in.
    Raises :class:`SchemaError` with a message naming the offending field.
    """
    if not isinstance(data, dict):
        raise SchemaError(f"event must be a JSON object, got {type(data).__name__}")

    for key in ("v", "run_id", "seq", "ts", "type"):
        if key not in data:
            raise SchemaError(f"missing required field {key!r}")

    if data["v"] not in SUPPORTED_VERSIONS:
        raise SchemaError(
            f"unsupported schema version {data['v']!r} "
            f"(this SDK writes v{SCHEMA_VERSION} and reads {', '.join(SUPPORTED_VERSIONS)})"
        )

    if not isinstance(data["run_id"], str) or not data["run_id"]:
        raise SchemaError("run_id must be a non-empty string")

    # bool is a subclass of int; a boolean seq is a bug, not a sequence number.
    if not isinstance(data["seq"], int) or isinstance(data["seq"], bool) or data["seq"] < 0:
        raise SchemaError("seq must be a non-negative integer")

    if not isinstance(data["ts"], str) or not _TS.match(data["ts"]):
        raise SchemaError(f"ts must be an ISO 8601 timestamp, got {data['ts']!r}")

    try:
        EventType(data["type"])
    except ValueError:
        raise SchemaError(f"unknown event type {data['type']!r}") from None

    body = data.get("body", {})
    if not isinstance(body, dict):
        raise SchemaError("body must be a JSON object")

    caused_by = data.get("caused_by")
    if caused_by is not None:
        if not isinstance(caused_by, list) or not all(
            isinstance(s, int) and not isinstance(s, bool) for s in caused_by
        ):
            raise SchemaError("caused_by must be a list of integer seq values")
        for s in caused_by:
            if s >= data["seq"]:
                raise SchemaError(
                    f"caused_by references seq {s}, which is not earlier than seq {data['seq']}"
                )

    for key in ("prev_hash", "hash"):
        value = data.get(key)
        if value is None:
            if sealed:
                raise SchemaError(f"missing required field {key!r}")
            continue
        if not isinstance(value, str) or not _HEX64.match(value):
            raise SchemaError(f"{key} must be 64 lowercase hex characters")

    unknown = set(data) - set(ENVELOPE_KEYS)
    if unknown:
        raise SchemaError(f"unknown envelope field(s): {', '.join(sorted(unknown))}")
