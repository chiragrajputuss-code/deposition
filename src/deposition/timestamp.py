"""RFC 3161 timestamps on a trace's head hash (ADR 014). Standard library only.

A timestamp authority (TSA) signs a statement that it saw a hash no later than
time T. Timestamping the chain head outsources "when did this trace exist?" to
a party that is not whoever holds the trace. It does not prove the recorded
events happened, or happened at T - only that the trace existed by then.

Only the 32-byte head hash leaves the machine. The request is built in DER by
hand; the response is stored byte for byte as ``run_<id>.tsr``. Checking the
TSA's signature needs CMS and X.509, which the standard library lacks, so this
module parses only enough DER to compare the token's message imprint with the
head hash and leaves the signature to ``openssl ts -verify`` - run by the user,
or by ``depo verify --tsa-ca`` with a CA file the user chose. A timestamp is
described as "unedited since T", never as verification (ADR 016).
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import canonical_json, now_ts

__all__ = [
    "TIMEOUT_S",
    "TimestampError",
    "TimestampResult",
    "TimestampToken",
    "build_request",
    "parse_response",
    "request_timestamp",
    "timestamp_head",
    "verify_timestamp",
]

#: Short on purpose, and never retried: a slow authority must not hold anything up.
TIMEOUT_S = 5.0

STATUS_VERSION = "0.1"

SHA256_OID = "2.16.840.1.101.3.4.2.1"
_SIGNED_DATA_OID = "1.2.840.113549.1.7.2"
_TST_INFO_OID = "1.2.840.113549.1.9.16.1.4"

NONE = "none"
UNAVAILABLE = "unavailable"
UNCHECKED = "unchecked"
ANCHORED = "anchored"
IMPRINT_MISMATCH = "imprint_mismatch"
BAD_TIMESTAMP = "bad_timestamp"
MALFORMED = "malformed"

#: Present and wrong - positive evidence against the trace, so verify fails.
FAILED = {IMPRINT_MISMATCH, BAD_TIMESTAMP, MALFORMED}


class TimestampError(Exception):
    """A timestamp request failed, or a response could not be parsed."""


# -- DER, just enough of it ----------------------------------------------------


def _der_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(content)) + content


def _der_int(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _tlv(0x02, raw)


def _der_oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        out.extend(reversed(chunk))
    return _tlv(0x06, bytes(out))


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
    """The TLVs directly inside ``data``, as ``(tag, content)``."""
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


# -- request ---------------------------------------------------------------------


def build_request(head_hash: str, *, nonce: int | None, cert_req: bool = True) -> bytes:
    """A ``TimeStampReq`` for ``head_hash``, byte-identical to ``openssl ts -query
    -sha256 -digest <head_hash> -cert`` given the same nonce.

    The imprint is the head hash itself: it *is* the SHA-256 of the last
    event's canonical bytes, so no further hashing is needed or honest.
    """
    algorithm = _tlv(0x30, _der_oid(SHA256_OID) + b"\x05\x00")
    imprint = _tlv(0x30, algorithm + _tlv(0x04, bytes.fromhex(head_hash)))
    body = _der_int(1) + imprint
    if nonce is not None:
        body += _der_int(nonce)
    if cert_req:
        body += b"\x01\x01\xff"
    return _tlv(0x30, body)


def request_timestamp(url: str, request: bytes, *, timeout: float = TIMEOUT_S) -> bytes:
    """POST a request to a TSA and return the raw response. Raises TimestampError."""
    http = urllib.request.Request(
        url,
        data=request,
        method="POST",
        headers={
            "Content-Type": "application/timestamp-query",
            "Accept": "application/timestamp-reply",
        },
    )
    try:
        with urllib.request.urlopen(http, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                raise TimestampError(f"authority answered HTTP {response.status}")
            return response.read()
    except TimestampError:
        raise
    except Exception as exc:  # noqa: BLE001 - every network failure is "unavailable"
        raise TimestampError(f"{type(exc).__name__}: {exc}") from exc


# -- response --------------------------------------------------------------------


@dataclass(frozen=True)
class TimestampToken:
    """What the response says, read without checking the TSA's signature."""

    status: int
    imprint_algorithm: str
    imprint: str
    gen_time: str | None
    nonce: int | None


def parse_response(der: bytes) -> TimestampToken:
    """Read a ``TimeStampResp`` down to its ``TSTInfo``. Raises TimestampError."""
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


# -- sidecars ----------------------------------------------------------------------


def tsr_path(trace_path: str | os.PathLike[str]) -> Path:
    return Path(trace_path).with_suffix(".tsr")


def status_path(trace_path: str | os.PathLike[str]) -> Path:
    return Path(trace_path).with_suffix(".timestamp.json")


def _write_status(trace_path: str | os.PathLike[str], status: dict[str, Any]) -> None:
    target = status_path(trace_path)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(canonical_json(status) + "\n", encoding="utf-8")
    tmp.replace(target)


def timestamp_head(
    url: str,
    head_hash: str,
    trace_path: str | os.PathLike[str],
    *,
    timeout: float | None = None,
) -> str:
    """Timestamp a finished trace's head and write its sidecars. Never raises.

    Returns the status written: ``granted`` or ``unavailable``.
    """
    nonce = secrets.randbits(64)
    status: dict[str, Any] = {
        "v": STATUS_VERSION,
        "url": url,
        "head_hash": head_hash,
        "nonce": str(nonce),
        "requested_at": now_ts(),
        "status": "pending",
    }
    try:
        _write_status(trace_path, status)
        raw = request_timestamp(
            url,
            build_request(head_hash, nonce=nonce),
            timeout=TIMEOUT_S if timeout is None else timeout,
        )
        token = parse_response(raw)
        if token.imprint_algorithm != SHA256_OID or token.imprint != head_hash:
            raise TimestampError("authority returned a token for a different hash")
        if token.nonce != nonce:
            raise TimestampError("authority returned a token with a different nonce")
        tsr_path(trace_path).write_bytes(raw)
        status.update(status="granted", gen_time=token.gen_time)
    except Exception as exc:  # noqa: BLE001 - a timestamp failure never reaches the host
        status.update(status="unavailable", reason=str(exc) or type(exc).__name__)
    try:
        _write_status(trace_path, status)
    except OSError:
        pass
    return status["status"]


# -- verification ------------------------------------------------------------------


@dataclass
class TimestampResult:
    status: str
    message: str
    gen_time: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in FAILED


def _read_status(trace_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    try:
        data = json.loads(status_path(trace_path).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def openssl_verify_command(head_hash: str, tsr: str | os.PathLike[str], ca_file: str) -> str:
    return shlex.join(
        ["openssl", "ts", "-verify", "-digest", head_hash, "-in", str(tsr), "-CAfile", ca_file]
    )


def verify_timestamp(
    trace_path: str | os.PathLike[str],
    head_hash: str | None,
    *,
    ca_file: str | None = None,
) -> TimestampResult:
    """Check a trace's timestamp sidecar against its head. Never raises."""
    tsr = tsr_path(trace_path)
    recorded = _read_status(trace_path)

    if not tsr.exists():
        if recorded is None:
            return TimestampResult(NONE, "none (no .tsr beside this trace)")
        if recorded.get("status") == "pending":
            return TimestampResult(UNAVAILABLE, "unavailable - the request never completed")
        return TimestampResult(
            UNAVAILABLE, f"unavailable - {recorded.get('reason') or 'no token was returned'}"
        )

    try:
        token = parse_response(tsr.read_bytes())
    except (OSError, TimestampError) as exc:
        return TimestampResult(MALFORMED, f"cannot read {tsr.name}: {exc}")

    if head_hash is None:
        return TimestampResult(
            UNCHECKED,
            "the chain did not verify far enough to compare the timestamp against",
            token.gen_time,
        )
    if token.imprint_algorithm != SHA256_OID or token.imprint != head_hash:
        return TimestampResult(
            IMPRINT_MISMATCH,
            f"timestamp covers {token.imprint[:12]}... but this trace ends at "
            f"{head_hash[:12]}... - the trace was rewritten after it was timestamped",
            token.gen_time,
        )
    if recorded is not None and recorded.get("nonce") not in (None, str(token.nonce)):
        return TimestampResult(
            MALFORMED, "the token's nonce does not match the request recorded beside it"
        )

    if ca_file is None:
        return TimestampResult(
            UNCHECKED,
            f"the authority asserts the trace was unedited since {token.gen_time}; its token "
            "covers this trace's head, but the authority's signature is not checked - check it "
            "with:\n     " + openssl_verify_command(head_hash, tsr, "<the TSA's CA certificate>"),
            token.gen_time,
        )

    openssl = shutil.which("openssl")
    if openssl is None:
        return TimestampResult(
            UNCHECKED,
            "imprint matches, but openssl is not on PATH to check the token against --tsa-ca",
            token.gen_time,
        )
    command = shlex.split(openssl_verify_command(head_hash, tsr, ca_file))
    command[0] = openssl
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return TimestampResult(UNCHECKED, f"could not run openssl: {exc}", token.gen_time)
    if proc.returncode == 0 and "Verification: OK" in proc.stdout:
        return TimestampResult(
            ANCHORED,
            f"unedited since {token.gen_time} - the authority's token checks out against "
            f"{Path(ca_file).name}",
            token.gen_time,
        )
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return TimestampResult(
        BAD_TIMESTAMP,
        f"openssl rejected the token against {Path(ca_file).name}: "
        f"{detail[-1] if detail else 'verification failed'}",
        token.gen_time,
    )
