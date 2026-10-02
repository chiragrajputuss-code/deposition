"""``deposition`` command line: view, verify, audit, diff, keygen.

Stdlib only. ``verify`` is the one that has to be beyond reproach - it is the
command a user runs when they need to show someone else that a trace was not
altered, so its exit code is part of its contract:

* ``0`` - every trace given verified
* ``1`` - at least one trace is broken, or carries a signature that fails
* ``2`` - the command could not run (bad arguments, missing file)

An *absent* signature is not a failure by default - most traces are unsigned and
the chain still says something - but a signature that does not check out is,
because that is positive evidence of tampering. ``--require-signature`` turns
"unsigned" and "signed by a key I cannot check" into failures too.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__, signing
from .chain import ChainResult, read_jsonl, verify
from .mandate import audit as audit_mandate
from .schema import SCHEMA_VERSION, is_blob_ref
from .signing import SignatureError, SignatureResult

__all__ = ["main"]

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2

DEFAULT_PORT = 7878


# -- shared helpers ---------------------------------------------------------


def _load(path: str) -> list[Any]:
    """Read a trace file, or exit(2) with a readable message."""
    if not os.path.exists(path):
        _die(f"no such trace file: {path}")
    if os.path.isdir(path):
        _die(f"{path} is a directory; pass a .jsonl trace file")
    try:
        return [payload for _, payload in read_jsonl(path)]
    except OSError as exc:
        _die(f"cannot read {path}: {exc}")


def _die(message: str) -> None:
    print(f"deposition: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def _summarize(event: dict[str, Any]) -> str:
    """A one-line label for an event, used by diff and by error messages."""
    body = event.get("body") or {}
    kind = event.get("type", "?")
    for key in ("tool", "label", "gen_ai.request.model", "agent", "status", "query", "exception"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return f"{kind}({value})"
    return str(kind)


def _signature(event: dict[str, Any]) -> str:
    """What makes two events 'the same step' across runs.

    Deliberately coarse: type plus the name of the thing acted on. Timestamps,
    token counts and latencies differ on every run and would drown the diff.
    """
    return _summarize(event)


# -- verify -----------------------------------------------------------------


#: Statuses that mean a signature is present and wrong - always a failure.
_SIGNATURE_FAILED = {
    signing.BAD_SIGNATURE,
    signing.HEAD_MISMATCH,
    signing.KEY_MISMATCH,
    signing.MALFORMED,
}


def _check_signature(path: str, result: ChainResult, pinned: str | None) -> SignatureResult:
    try:
        sidecar = signing.read_sidecar(path)
    except SignatureError as exc:
        return SignatureResult(signing.MALFORMED, str(exc))
    return signing.verify_sidecar(
        sidecar,
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=pinned,
    )


def _signature_ok(signature: SignatureResult, *, required: bool) -> bool:
    if signature.status in _SIGNATURE_FAILED:
        return False
    return signature.trusted if required else True


def cmd_verify(args: argparse.Namespace) -> int:
    pinned = None
    if args.pubkey:
        try:
            pinned = signing.load_public_key(args.pubkey)
        except SignatureError as exc:
            _die(str(exc))

    results: list[tuple[str, ChainResult, SignatureResult]] = []
    for path in args.trace:
        result = verify(_load(path))
        results.append((path, result, _check_signature(path, result, pinned)))

    required = args.require_signature
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "trace": path,
                        "ok": result.ok and _signature_ok(signature, required=required),
                        "chain_ok": result.ok,
                        "run_id": result.run_id,
                        "events_checked": result.events_checked,
                        "head_hash": result.head_hash,
                        "signature": {
                            "status": signature.status,
                            "message": signature.message,
                            "public_key": signature.public_key,
                            "signed_at": signature.signed_at,
                        },
                        "errors": [
                            {"code": e.code, "message": e.message, "seq": e.seq, "line": e.line}
                            for e in result.errors
                        ],
                    }
                    for path, result, signature in results
                ],
                indent=2,
            )
        )
    else:
        for path, result, signature in results:
            print(f"{result.summary()}  [{path}]")
            if any(
                isinstance(e, dict) and e.get("v") == "0.1" for e in _load(path)
            ):
                # ADR 008: v0 predates RFC 8785 canonicalisation, so only this
                # SDK can verify it. Said out loud, never implied away.
                print("  note: schema v0.1 trace - verifiable by this SDK only, "
                      "predates RFC 8785 canonicalisation")
            for error in result.errors:
                print(f"  {error}")
            if not result.ok:
                print(
                    "  This trace is not evidence of what happened. "
                    "Everything before the reported event is still intact."
                )
            # "!!" is reserved for a signature that actually failed; an absent or
            # uncheckable one is a gap, not an alarm, and must not read like one.
            if signature.ok:
                marker = "OK "
            elif signature.status in _SIGNATURE_FAILED:
                marker = "!! "
            else:
                marker = "-- "
            print(f"  {marker}signature: {signature.message}")
            if result.ok and signature.status == signing.HEAD_MISMATCH:
                # The chain is internally consistent and still not the trace that
                # was signed: exactly the edit-and-reseal the signature exists for.
                print(
                    "  The chain re-seals cleanly, so it was rebuilt after signing. "
                    "Trust the signature, not the chain."
                )

    return (
        EXIT_OK
        if all(
            result.ok and _signature_ok(signature, required=required)
            for _, result, signature in results
        )
        else EXIT_BROKEN
    )


# -- keygen -----------------------------------------------------------------


def cmd_keygen(args: argparse.Namespace) -> int:
    try:
        key = signing.generate_key()
        path = key.write(args.out)
    except SignatureError as exc:
        _die(str(exc))
    except OSError as exc:
        _die(f"cannot write {args.out}: {exc}")
    print(f"private key  {path}  (keep this secret, mode 0600)")
    print(f"public key   {path}.pub")
    print(f"             {key.public_hex}")
    print(
        "\nGive the public key to whoever verifies your traces, by some route they\n"
        "already trust, and have them run:  depo verify run.jsonl --pubkey <key>\n"
        "A signature checked against a key that travelled with the trace proves nothing."
    )
    return EXIT_OK


# -- audit ------------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    events = _load(args.trace)

    # A broken chain is not auditable: the verdict is about the record, and
    # this record is not one.
    chain = verify(events)
    if not chain.ok:
        print(f"{chain.summary()}  [{args.trace}]")
        print("  Cannot audit: the chain does not hold, so these events are not the record.")
        return EXIT_BROKEN

    result = audit_mandate(events)
    print(f"{result.summary()}  [{args.trace}]")

    if result.mandate is None:
        print("  Record a mandate with deposition.init(mandate={...}) to make runs auditable.")
        return EXIT_USAGE

    if result.mandate.get("issuer"):
        print(f"  issued by: {result.mandate['issuer']}")
    for violation in result.violations:
        print(f"  !! {violation}")
    if result.checked:
        print(f"  checked: {', '.join(sorted(result.checked))}")
    if result.unchecked:
        # Never silent about blind spots: an audit that skips a rule quietly is
        # the overclaim this product exists to end.
        print(f"  NOT checkable by this version: {', '.join(sorted(result.unchecked))}")
    if result.dropped:
        print(
            f"  {result.dropped} event(s) were dropped before reaching the trace - "
            "compliance cannot be attested on a record that admits gaps."
        )
    return EXIT_OK if result.attestable else EXIT_BROKEN


# -- view -------------------------------------------------------------------


def cmd_view(args: argparse.Namespace) -> int:
    _load(args.trace)  # fail fast on a missing or unreadable file

    try:
        from .viewer import serve
    except ImportError as exc:  # pragma: no cover - depends on the environment
        _die(
            f"the local viewer needs its extra dependencies ({exc}).\n"
            "  Install them with:  pip install 'deposition[viewer]'"
        )

    pinned = None
    if args.pubkey:
        try:
            pinned = signing.load_public_key(args.pubkey)
        except SignatureError as exc:
            _die(str(exc))

    return serve(
        args.trace,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        public_key=pinned,
    )


# -- diff -------------------------------------------------------------------


def cmd_diff(args: argparse.Namespace) -> int:
    left, right = _load(args.left), _load(args.right)
    left_events = [e for e in left if isinstance(e, dict)]
    right_events = [e for e in right if isinstance(e, dict)]

    matcher = difflib.SequenceMatcher(
        None, [_signature(e) for e in left_events], [_signature(e) for e in right_events]
    )

    changes: list[str] = []
    first_divergence: int | None = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            # Same step, but its payload may still differ - that is often the
            # interesting part, so compare bodies within an aligned block.
            for offset in range(i2 - i1):
                lhs, rhs = left_events[i1 + offset], right_events[j1 + offset]
                if _body_differs(lhs, rhs):
                    if first_divergence is None:
                        first_divergence = lhs.get("seq")
                    changes.append(
                        f"  ~ seq {lhs.get('seq')}/{rhs.get('seq')}  {_signature(lhs)}  "
                        f"body differs"
                    )
            continue
        if first_divergence is None:
            first_divergence = (
                left_events[i1].get("seq") if i1 < len(left_events) else right_events[j1].get("seq")
            )
        for event in left_events[i1:i2]:
            changes.append(f"  - seq {event.get('seq')}  {_summarize(event)}")
        for event in right_events[j1:j2]:
            changes.append(f"  + seq {event.get('seq')}  {_summarize(event)}")

    print(f"--- {args.left}  ({len(left_events)} events)")
    print(f"+++ {args.right}  ({len(right_events)} events)")
    if not changes:
        print("no differences in the recorded steps")
        return EXIT_OK

    print(f"first divergence at seq {first_divergence}")
    for line in changes:
        print(line)
    return EXIT_OK


def _body_differs(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare bodies ignoring the fields that differ on every run by nature."""
    return _comparable(left.get("body") or {}) != _comparable(right.get("body") or {})


_VOLATILE = {"latency_ms", "duration_ms", "env", "sdk_version", "cost_usd", "usage", "totals"}


def _comparable(body: Any) -> Any:
    if isinstance(body, dict):
        if is_blob_ref(body):
            return {"$blob": body["$blob"]}  # compare by content hash, not by size metadata
        return {k: _comparable(v) for k, v in sorted(body.items()) if k not in _VOLATILE}
    if isinstance(body, list):
        return [_comparable(v) for v in body]
    return body


# -- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        # Left to argparse so `depo --help` says "depo", not "deposition".
        prog=None,
        description="Verbatim, tamper-evident records of AI agent runs: replay, verify and diff.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"deposition {__version__} (trace schema v{SCHEMA_VERSION})",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_view = sub.add_parser("view", help="replay a run in the local viewer")
    p_view.add_argument("trace", help="path to a .jsonl trace file")
    p_view.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    p_view.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (localhost only)")
    p_view.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p_view.add_argument(
        "--pubkey",
        metavar="KEY",
        help="public key the signature must match: 64 hex characters or a file holding them",
    )
    p_view.set_defaults(func=cmd_view)

    p_verify = sub.add_parser("verify", help="check a trace's hash chain")
    p_verify.add_argument("trace", nargs="+", help="one or more .jsonl trace files")
    p_verify.add_argument("--json", action="store_true", help="machine-readable output")
    p_verify.add_argument(
        "--pubkey",
        metavar="KEY",
        help="public key to check the signature against: 64 hex characters or a file holding them",
    )
    p_verify.add_argument(
        "--require-signature",
        action="store_true",
        help="fail unless the trace carries a signature that verifies against --pubkey",
    )
    p_verify.set_defaults(func=cmd_verify)

    p_keygen = sub.add_parser("keygen", help="generate an Ed25519 signing key")
    p_keygen.add_argument(
        "--out",
        default="deposition-signing-key.pem",
        help="where to write the private key (default: ./deposition-signing-key.pem)",
    )
    p_keygen.set_defaults(func=cmd_keygen)

    p_audit = sub.add_parser("audit", help="check a trace against the mandate it carries")
    p_audit.add_argument("trace", help="a .jsonl trace file whose run_start records a mandate")
    p_audit.set_defaults(func=cmd_audit)

    p_diff = sub.add_parser("diff", help="compare the steps of two runs")
    p_diff.add_argument("left", help="baseline trace")
    p_diff.add_argument("right", help="trace to compare against it")
    p_diff.set_defaults(func=cmd_diff)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
