#!/usr/bin/env python3
"""Verify a Deposition trace without installing Deposition.

    python verify_standalone.py run_<id>.jsonl [--pubkey <hex|file>] [--json]

One file, Python 3.10+ standard library only. It does not import the
``deposition`` package and needs nothing from PyPI, so someone who does not
trust that package can read this file and check a trace with it. The code
below is a deliberate second copy of ``src/deposition/schema.py`` and
``src/deposition/chain.py`` - same function names, same order of checks, same
error codes - and ``tests/test_verify_standalone.py`` fails if the two copies
ever disagree.

What a trace shows, if this script says so:

* The file was not altered after it was written. No event was edited,
  removed or reordered. On its own, the chain shows this only against someone
  who does not hold the file: anyone who does can edit an event and re-seal
  the rest.
* It was signed by a specific key, if you pin that key. Without ``--pubkey``
  the result is "signed by an unverified key", never "verified".
* What the run was authorised to do was sealed together with what it did. This
  script checks the seal, not the mandate; ``depo audit`` checks conduct
  against it.
* Which tool definitions the model was shown, as digests, for calls made
  through the OpenAI or Anthropic SDK - sealed by the same chain.
* That the trace existed no later than a time asserted by an independent
  timestamp authority, if a ``.tsr`` is beside it. This script checks the
  token covers this trace's head and prints the ``openssl ts -verify``
  command for the authority's signature; it never says "verified" for it.

What a trace does not show:

* That the recorded events happened. The process that writes the trace is the
  process that runs the tools. A compromised or buggy host can produce a
  complete, internally consistent, correctly signed chain for a run that never
  occurred.
* That the signing key was used only by you, that an external side effect
  succeeded, or when the events happened - event timestamps are asserted by
  the machine that wrote them.
* Completeness where events were dropped. Dropped events are counted, and
  this script reports the count.

Signatures: if the ``cryptography`` package happens to be importable, the
signature is checked with it. If not - or with ``--openssl`` - this script
still checks everything that needs no cryptography (head hash, run id, which
key), writes the exact signed bytes, the signature and the public key to a
temporary directory, and prints the ``openssl`` command that checks the
signature itself, so you can use a tool you already trust. That needs
OpenSSL 3.0 or newer; macOS's built-in ``/usr/bin/openssl`` is LibreSSL and
cannot do it.

Exit codes match ``depo verify``: 0 verified, 1 broken, 2 could not run.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2

# -- schema.py ----------------------------------------------------------------

SCHEMA_VERSION = "1.0"
SUPPORTED_VERSIONS = ("0.1", "1.0")

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_TS = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})\Z")

ENVELOPE_KEYS = ("v", "run_id", "seq", "ts", "type", "body", "caused_by", "prev_hash", "hash")

EVENT_TYPES = (
    "run_start",
    "llm_call",
    "tool_call",
    "decision",
    "retrieval",
    "error",
    "annotation",
    "run_end",
)


class SchemaError(ValueError):
    """Raised when an event does not conform to the trace schema."""


def canonical_json(obj: Any, *, version: str = SCHEMA_VERSION) -> str:
    """v1.0 is RFC 8785 (JCS); v0.1 was Python's ``json.dumps(sort_keys=True)``."""
    if version == "0.1":
        return json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    return _jcs(obj)


def canonical_bytes(obj: Any, *, version: str = SCHEMA_VERSION) -> bytes:
    return canonical_json(obj, version=version).encode("utf-8")


def _decimal_parts(value: float) -> tuple[str, int]:
    """Shortest round-trip digits of ``abs(value)`` and its decimal position."""
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
    """A finite float, printed exactly as ECMAScript ``Number::toString`` prints it."""
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
    return key.encode("utf-16-be")


def _jcs(obj: Any) -> str:
    """RFC 8785 serialisation. ``bool`` is checked before ``int`` deliberately."""
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


def validate_event(data: Any, *, sealed: bool = True) -> None:
    """Validate one event dict against the schema, naming the offending field."""
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

    if not isinstance(data["seq"], int) or isinstance(data["seq"], bool) or data["seq"] < 0:
        raise SchemaError("seq must be a non-negative integer")

    if not isinstance(data["ts"], str) or not _TS.match(data["ts"]):
        raise SchemaError(f"ts must be an ISO 8601 timestamp, got {data['ts']!r}")

    if data["type"] not in EVENT_TYPES:
        raise SchemaError(f"unknown event type {data['type']!r}")

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


# -- chain.py -----------------------------------------------------------------

GENESIS_HASH = "0" * 64


def event_hash(event: dict[str, Any]) -> str:
    """SHA-256 over the event's canonical JSON, excluding its own ``hash`` field.

    The event's own ``v`` picks the canonicalisation (ADR 008).
    """
    payload = {k: v for k, v in event.items() if k != "hash"}
    if "prev_hash" not in payload:
        raise SchemaError("cannot hash an event with no prev_hash")
    version = payload.get("v") if isinstance(payload.get("v"), str) else None
    return sha256_hex(canonical_bytes(payload, version=version or "1.0"))


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

    def summary(self) -> str:
        if self.ok:
            return (
                f"OK  {self.events_checked} events verified"
                f"{f' for {self.run_id}' if self.run_id else ''}"
            )
        first = self.errors[0] if self.errors else None
        return f"BROKEN  {len(self.errors)} problem(s); first: {first}"


def read_jsonl(path: str | os.PathLike[str]) -> list[Any]:
    """Each non-empty line parsed; a line that is not JSON becomes a marker string."""
    out: list[Any] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                out.append(f"__unparseable__:{exc}")
    return out


def verify(events: list[Any]) -> ChainResult:
    """Replay a chain and report the first way it fails to hold together."""
    result = ChainResult(ok=True)
    expected_prev = GENESIS_HASH
    expected_seq = 0
    seen_run_end = False

    def fail(code: str, message: str, seq: int | None = None, line: int | None = None) -> None:
        result.ok = False
        result.errors.append(ChainError(code=code, message=message, seq=seq, line=line))

    for index, event in enumerate(events):
        line = index + 1

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
            if event["type"] != "run_start":
                fail(
                    "bad_header",
                    f"first event is {event['type']!r}, expected 'run_start'",
                    seq=seq,
                    line=line,
                )
        elif event["run_id"] != result.run_id:
            fail(
                "run_id_mismatch",
                f"event belongs to run {event['run_id']!r}, not {result.run_id!r}",
                seq=seq,
                line=line,
            )
            break

        if seq != expected_seq:
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
        if event["type"] == "run_end":
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


def dropped_events(events: list[Any]) -> int:
    """What the recorder itself reported dropping, from ``run_end``'s totals."""
    for event in events:
        if isinstance(event, dict) and event.get("type") == "run_end":
            body = event.get("body") if isinstance(event.get("body"), dict) else {}
            totals = body.get("totals") if isinstance(body.get("totals"), dict) else {}
            try:
                return int(totals.get("dropped") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


# -- signing.py ---------------------------------------------------------------

ALGORITHM = "ed25519"

UNSIGNED = "unsigned"
UNAVAILABLE = "unavailable"
MALFORMED = "malformed"
BAD_SIGNATURE = "bad_signature"
HEAD_MISMATCH = "head_mismatch"
KEY_MISMATCH = "key_mismatch"
UNPINNED = "unpinned"
VERIFIED = "verified"

_SIGNATURE_FAILED = {BAD_SIGNATURE, HEAD_MISMATCH, KEY_MISMATCH, MALFORMED}

#: DER prefix of an Ed25519 SubjectPublicKeyInfo (RFC 8410); the 32 raw key
#: bytes follow it. Enough to hand openssl a PEM without any ASN.1 library.
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


class SidecarError(Exception):
    """A sidecar or public key could not be read or parsed."""


@dataclass
class SignatureResult:
    status: str
    message: str
    public_key: str | None = None
    signed_at: str | None = None
    openssl_command: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == VERIFIED


def sidecar_path(trace_path: str | os.PathLike[str]) -> Path:
    return Path(trace_path).with_suffix(".sig")


def read_sidecar(trace_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    target = sidecar_path(trace_path)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SidecarError(f"cannot read {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise SidecarError(f"{target} does not contain a signature object")
    return data


def load_public_key(source: str) -> str:
    """A pinned public key given as 64 hex characters or a path to them."""
    text = str(source).strip()
    candidate = text
    path = Path(text)
    if len(text) != 64 and path.exists():
        try:
            candidate = path.read_text("utf-8").strip()
        except OSError as exc:
            raise SidecarError(f"cannot read public key {path}: {exc}") from exc
    candidate = candidate.strip().lower()
    if len(candidate) != 64:
        raise SidecarError(f"public key must be 64 hex characters (32 bytes), got {len(candidate)}")
    try:
        bytes.fromhex(candidate)
    except ValueError as exc:
        raise SidecarError("public key is not valid hex") from exc
    return candidate


def _signable(claims: dict[str, Any]) -> bytes:
    """The bytes actually signed: every claim except the signature itself."""
    return canonical_bytes({k: v for k, v in claims.items() if k != "signature"})


def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        return None
    return ed25519


def _openssl_files(claims: dict[str, Any], public_key: str) -> str:
    """Write what openssl needs to check the signature; return the command."""
    folder = Path(tempfile.mkdtemp(prefix="deposition-verify-"))
    signed = folder / "signed.bin"
    signature = folder / "signature.bin"
    pem = folder / "public_key.pem"
    signed.write_bytes(_signable(claims))
    signature.write_bytes(bytes.fromhex(claims["signature"]))
    der = _ED25519_SPKI_PREFIX + bytes.fromhex(public_key)
    pem.write_text(
        "-----BEGIN PUBLIC KEY-----\n"
        + base64.b64encode(der).decode("ascii")
        + "\n-----END PUBLIC KEY-----\n",
        encoding="ascii",
    )
    return shlex.join(
        [
            "openssl",
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            str(pem),
            "-rawin",
            "-in",
            str(signed),
            "-sigfile",
            str(signature),
        ]
    )


def verify_sidecar(
    sidecar: dict[str, Any] | None,
    *,
    head_hash: str | None,
    run_id: str | None = None,
    expected_public_key: str | None = None,
    use_openssl: bool = False,
) -> SignatureResult:
    """Check a sidecar against the chain that was just verified. Never raises."""
    if sidecar is None:
        return SignatureResult(UNSIGNED, "unsigned (no .sig file beside this trace)")

    claims = {k: sidecar.get(k) for k in sidecar}
    signature = claims.get("signature")
    public_key = claims.get("public_key")
    for name, value in (
        ("signature", signature),
        ("public_key", public_key),
        ("head_hash", claims.get("head_hash")),
    ):
        if not isinstance(value, str) or not value:
            return SignatureResult(MALFORMED, f"signature file has no usable {name}")
    if claims.get("alg") != ALGORITHM:
        return SignatureResult(MALFORMED, f"unsupported signature algorithm {claims.get('alg')!r}")

    signed_at = claims.get("signed_at") if isinstance(claims.get("signed_at"), str) else None

    def result(status: str, message: str, command: str | None = None) -> SignatureResult:
        return SignatureResult(status, message, public_key, signed_at, command)

    ed25519 = None if use_openssl else _ed25519()
    try:
        raw_key = bytes.fromhex(public_key)
        raw_signature = bytes.fromhex(signature)
    except ValueError:
        return result(MALFORMED, "signature or public key is not valid hex")

    if ed25519 is not None:
        try:
            key = ed25519.Ed25519PublicKey.from_public_bytes(raw_key)
            key.verify(raw_signature, _signable(claims))
        except ValueError:
            return result(MALFORMED, "signature or public key is not valid hex")
        except Exception:  # noqa: BLE001 - InvalidSignature lives in a lazily imported module
            return result(
                BAD_SIGNATURE,
                "signature does not match this signature file - it was altered or forged",
            )

    # Whether or not the signature itself could be checked here, these need no
    # cryptography: is it about *this* trace, and signed by the expected key?
    if head_hash is not None and claims["head_hash"] != head_hash:
        return result(
            HEAD_MISMATCH,
            f"signature covers head {claims['head_hash'][:12]}... but this trace ends at "
            f"{head_hash[:12]}... - the trace was rewritten after it was signed",
        )
    if run_id is not None and claims.get("run_id") not in (None, run_id):
        return result(
            HEAD_MISMATCH,
            f"signature belongs to run {claims.get('run_id')!r}, not {run_id!r}",
        )

    if ed25519 is None:
        if expected_public_key is not None and expected_public_key.lower() != public_key.lower():
            return result(
                KEY_MISMATCH,
                f"signed by {public_key[:12]}..., not the expected key "
                f"{expected_public_key[:12]}...",
            )
        command = _openssl_files(claims, public_key)
        pinned = (
            "against the key you pinned"
            if expected_public_key is not None
            else "against the key the .sig file carries itself (unpinned - pass --pubkey)"
        )
        return result(
            UNAVAILABLE,
            "signature not checked here (no cryptography package); head, run and key "
            f"match - check the signature itself {pinned} with OpenSSL 3.0+:\n"
            f"     {command}",
            command,
        )

    if expected_public_key is None:
        return result(
            UNPINNED,
            f"signed by an unverified key {public_key[:12]}... - pass --pubkey to check it "
            "against the key you expect",
        )
    if expected_public_key.lower() != public_key.lower():
        return result(
            KEY_MISMATCH,
            f"signed by {public_key[:12]}..., not the expected key {expected_public_key[:12]}...",
        )
    return result(VERIFIED, f"signature verified against {public_key[:12]}...")


# -- timestamp.py: just enough DER to read an RFC 3161 token -------------------

SHA256_OID = "2.16.840.1.101.3.4.2.1"
_SIGNED_DATA_OID = "1.2.840.113549.1.7.2"
_TST_INFO_OID = "1.2.840.113549.1.9.16.1.4"

TS_NONE = "none"
TS_UNAVAILABLE = "unavailable"
TS_UNVERIFIED = "unverified"
TS_IMPRINT_MISMATCH = "imprint_mismatch"
TS_MALFORMED = "malformed"
TS_FAILED = {TS_IMPRINT_MISMATCH, "bad_timestamp", TS_MALFORMED}


class TimestampError(Exception):
    """A response could not be parsed."""


def _decode_oid(content: bytes) -> str:
    if not content:
        raise TimestampError("empty OID")
    parts = [content[0] // 40, content[0] % 40]
    value = 0
    for byte in content[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(value)
            value = 0
    return ".".join(str(p) for p in parts)


def _children(data: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(data):
        if i + 2 > len(data):
            raise TimestampError("truncated DER")
        tag, first = data[i], data[i + 1]
        i += 2
        if first & 0x80:
            count = first & 0x7F
            if count == 0 or count > 4 or i + count > len(data):
                raise TimestampError("unsupported DER length")
            length = int.from_bytes(data[i : i + count], "big")
            i += count
        else:
            length = first
        if i + length > len(data):
            raise TimestampError("truncated DER")
        out.append((tag, data[i : i + length]))
        i += length
    return out


def _only(data: bytes, tag: int, what: str) -> bytes:
    items = _children(data)
    if len(items) != 1 or items[0][0] != tag:
        raise TimestampError(f"expected a single {what}")
    return items[0][1]


@dataclass(frozen=True)
class TimestampToken:
    status: int
    imprint_algorithm: str
    imprint: str
    gen_time: str | None
    nonce: int | None


def parse_response(der: bytes) -> TimestampToken:
    """Read a ``TimeStampResp`` down to its ``TSTInfo``, without checking its signature."""
    response = _children(_only(der, 0x30, "TimeStampResp"))
    if not response or response[0][0] != 0x30:
        raise TimestampError("missing PKIStatusInfo")
    status_items = _children(response[0][1])
    if not status_items or status_items[0][0] != 0x02:
        raise TimestampError("missing PKIStatus")
    status = int.from_bytes(status_items[0][1], "big", signed=True)
    if status not in (0, 1):
        raise TimestampError(f"authority refused the request (PKIStatus {status})")
    if len(response) < 2 or response[1][0] != 0x30:
        raise TimestampError("granted, but no timeStampToken")

    content_info = _children(response[1][1])
    if (
        len(content_info) < 2
        or content_info[0][0] != 0x06
        or _decode_oid(content_info[0][1]) != _SIGNED_DATA_OID
    ):
        raise TimestampError("timeStampToken is not CMS SignedData")
    signed_data = _children(_only(content_info[1][1], 0x30, "SignedData"))
    encap = next((c for t, c in signed_data if t == 0x30), None)
    if encap is None:
        raise TimestampError("SignedData has no encapsulated content")
    encap_items = _children(encap)
    if (
        len(encap_items) < 2
        or encap_items[0][0] != 0x06
        or _decode_oid(encap_items[0][1]) != _TST_INFO_OID
    ):
        raise TimestampError("token does not carry a TSTInfo")
    tst_der = _only(encap_items[1][1], 0x04, "eContent")

    tst = _children(_only(tst_der, 0x30, "TSTInfo"))
    if len(tst) < 5 or tst[2][0] != 0x30:
        raise TimestampError("TSTInfo is incomplete")
    imprint_items = _children(tst[2][1])
    if len(imprint_items) != 2 or imprint_items[1][0] != 0x04:
        raise TimestampError("messageImprint is malformed")
    algorithm_items = _children(imprint_items[0][1])
    if not algorithm_items or algorithm_items[0][0] != 0x06:
        raise TimestampError("messageImprint has no algorithm")

    gen_time = tst[4][1].decode("ascii", "replace") if tst[4][0] == 0x18 else None
    nonce = next(
        (int.from_bytes(c, "big", signed=True) for t, c in tst[5:] if t == 0x02),
        None,
    )
    return TimestampToken(
        status=status,
        imprint_algorithm=_decode_oid(algorithm_items[0][1]),
        imprint=imprint_items[1][1].hex(),
        gen_time=gen_time,
        nonce=nonce,
    )


@dataclass
class TimestampResult:
    status: str
    message: str
    gen_time: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in TS_FAILED


def openssl_verify_command(head_hash: str, tsr: str | os.PathLike[str], ca_file: str) -> str:
    return shlex.join(
        ["openssl", "ts", "-verify", "-digest", head_hash, "-in", str(tsr), "-CAfile", ca_file]
    )


def verify_timestamp(trace_path: str | os.PathLike[str], head_hash: str | None) -> TimestampResult:
    """Check the ``.tsr`` beside a trace against its head; the token's signature
    is left to ``openssl ts -verify``, whose command this prints. Never raises."""
    tsr = Path(trace_path).with_suffix(".tsr")
    try:
        recorded = json.loads(Path(trace_path).with_suffix(".timestamp.json").read_text("utf-8"))
        recorded = recorded if isinstance(recorded, dict) else None
    except (OSError, ValueError):
        recorded = None

    if not tsr.exists():
        if recorded is None:
            return TimestampResult(TS_NONE, "none (no .tsr beside this trace)")
        if recorded.get("status") == "pending":
            return TimestampResult(TS_UNAVAILABLE, "unavailable - the request never completed")
        return TimestampResult(
            TS_UNAVAILABLE,
            f"unavailable - {recorded.get('reason') or 'no token was returned'}",
        )

    try:
        token = parse_response(tsr.read_bytes())
    except (OSError, TimestampError) as exc:
        return TimestampResult(TS_MALFORMED, f"cannot read {tsr.name}: {exc}")

    if head_hash is None:
        return TimestampResult(
            TS_UNVERIFIED,
            "the chain did not verify far enough to compare the timestamp against",
            token.gen_time,
        )
    if token.imprint_algorithm != SHA256_OID or token.imprint != head_hash:
        return TimestampResult(
            TS_IMPRINT_MISMATCH,
            f"timestamp covers {token.imprint[:12]}... but this trace ends at "
            f"{head_hash[:12]}... - the trace was rewritten after it was timestamped",
            token.gen_time,
        )
    if recorded is not None and recorded.get("nonce") not in (None, str(token.nonce)):
        return TimestampResult(
            TS_MALFORMED, "the token's nonce does not match the request recorded beside it"
        )
    return TimestampResult(
        TS_UNVERIFIED,
        f"the authority asserts {token.gen_time}; imprint matches this trace's head, "
        "signature not checked - verify it with:\n     "
        + openssl_verify_command(head_hash, tsr, "<the TSA's CA certificate>"),
        token.gen_time,
    )


# -- cli.py: verify -----------------------------------------------------------


def _die(message: str) -> None:
    print(f"deposition: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def _load(path: str) -> list[Any]:
    if not os.path.exists(path):
        _die(f"no such trace file: {path}")
    if os.path.isdir(path):
        _die(f"{path} is a directory; pass a .jsonl trace file")
    try:
        return read_jsonl(path)
    except OSError as exc:
        _die(f"cannot read {path}: {exc}")
    return []  # pragma: no cover - _die raised


def _check_signature(
    path: str, result: ChainResult, pinned: str | None, use_openssl: bool
) -> SignatureResult:
    try:
        sidecar = read_sidecar(path)
    except SidecarError as exc:
        return SignatureResult(MALFORMED, str(exc))
    return verify_sidecar(
        sidecar,
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=pinned,
        use_openssl=use_openssl,
    )


def _signature_ok(signature: SignatureResult, *, required: bool) -> bool:
    if signature.status in _SIGNATURE_FAILED:
        return False
    return signature.ok if required else True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_standalone.py",
        description="Verify a Deposition trace with the Python standard library only.",
    )
    parser.add_argument("trace", nargs="+", help="one or more .jsonl trace files")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--pubkey",
        metavar="KEY",
        help="public key to check the signature against: 64 hex characters or a file holding them",
    )
    parser.add_argument(
        "--require-signature",
        action="store_true",
        help="fail unless the trace carries a signature that verifies against --pubkey",
    )
    parser.add_argument(
        "--openssl",
        action="store_true",
        help="do not use the cryptography package even if installed; print an openssl command",
    )
    args = parser.parse_args(argv)

    pinned = None
    if args.pubkey:
        try:
            pinned = load_public_key(args.pubkey)
        except SidecarError as exc:
            _die(str(exc))

    checked = []
    for path in args.trace:
        events = _load(path)
        result = verify(events)
        signature = _check_signature(path, result, pinned, args.openssl)
        stamp = verify_timestamp(path, result.head_hash)
        checked.append((path, events, result, signature, stamp))

    required = args.require_signature

    def passed(result: ChainResult, signature: SignatureResult, stamp: TimestampResult) -> bool:
        return result.ok and _signature_ok(signature, required=required) and not stamp.failed

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "trace": path,
                        "ok": passed(result, signature, stamp),
                        "chain_ok": result.ok,
                        "run_id": result.run_id,
                        "events_checked": result.events_checked,
                        "head_hash": result.head_hash,
                        "dropped": dropped_events(events),
                        "signature": {
                            "status": signature.status,
                            "message": signature.message,
                            "public_key": signature.public_key,
                            "signed_at": signature.signed_at,
                        },
                        "timestamp": {
                            "status": stamp.status,
                            "message": stamp.message,
                            "gen_time": stamp.gen_time,
                        },
                        "openssl_command": signature.openssl_command,
                        "errors": [
                            {"code": e.code, "message": e.message, "seq": e.seq, "line": e.line}
                            for e in result.errors
                        ],
                    }
                    for path, events, result, signature, stamp in checked
                ],
                indent=2,
            )
        )
    else:
        for path, events, result, signature, stamp in checked:
            print(f"{result.summary()}  [{path}]")
            if any(isinstance(e, dict) and e.get("v") == "0.1" for e in events):
                print(
                    "  note: schema v0.1 trace - predates RFC 8785 canonicalisation, so only "
                    "Python's own serialiser (this script or the SDK) reproduces its hashes"
                )
            for error in result.errors:
                print(f"  {error}")
            if not result.ok:
                print(
                    "  This trace is not evidence of what happened. "
                    "Everything before the reported event is still intact."
                )
            dropped = dropped_events(events)
            if dropped:
                print(f"  -- {dropped} dropped event(s) - the record admits gaps")
            print(
                f"  {_marker(signature.ok, signature.status in _SIGNATURE_FAILED)}"
                f"signature: {signature.message}"
            )
            if result.ok and signature.status == HEAD_MISMATCH:
                print(
                    "  The chain re-seals cleanly, so it was rebuilt after signing. "
                    "Trust the signature, not the chain."
                )
            print(f"  {_marker(False, stamp.failed)}timestamp: {stamp.message}")

    return (
        EXIT_OK
        if all(passed(result, signature, stamp) for _, _, result, signature, stamp in checked)
        else EXIT_BROKEN
    )


def _marker(ok: bool, failed: bool) -> str:
    return "OK " if ok else "!! " if failed else "-- "


if __name__ == "__main__":
    raise SystemExit(main())
