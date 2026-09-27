"""``postflight`` command line: view, verify, diff.

Three commands, stdlib only. ``verify`` is the one that has to be beyond
reproach - it is the command a user runs when they need to show someone else that
a trace was not altered, so its exit code is part of its contract:

* ``0`` - every trace given verified
* ``1`` - at least one trace is broken
* ``2`` - the command could not run (bad arguments, missing file)
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .chain import ChainResult, read_jsonl, verify
from .schema import SCHEMA_VERSION, is_blob_ref

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
    print(f"postflight: {message}", file=sys.stderr)
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


def cmd_verify(args: argparse.Namespace) -> int:
    results: list[tuple[str, ChainResult]] = []
    for path in args.trace:
        results.append((path, verify(_load(path))))

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "trace": path,
                        "ok": result.ok,
                        "run_id": result.run_id,
                        "events_checked": result.events_checked,
                        "head_hash": result.head_hash,
                        "errors": [
                            {"code": e.code, "message": e.message, "seq": e.seq, "line": e.line}
                            for e in result.errors
                        ],
                    }
                    for path, result in results
                ],
                indent=2,
            )
        )
    else:
        for path, result in results:
            print(f"{result.summary()}  [{path}]")
            for error in result.errors:
                print(f"  {error}")
            if not result.ok:
                print(
                    "  This trace is not evidence of what happened. "
                    "Everything before the reported event is still intact."
                )

    return EXIT_OK if all(result.ok for _, result in results) else EXIT_BROKEN


# -- view -------------------------------------------------------------------


def cmd_view(args: argparse.Namespace) -> int:
    _load(args.trace)  # fail fast on a missing or unreadable file

    try:
        from .viewer import serve
    except ImportError as exc:  # pragma: no cover - depends on the environment
        _die(
            f"the local viewer needs its extra dependencies ({exc}).\n"
            "  Install them with:  pip install 'postflight[viewer]'"
        )

    return serve(args.trace, host=args.host, port=args.port, open_browser=not args.no_browser)


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
        prog="postflight",
        description="Flight recorder for AI agents: replay, verify and diff agent runs.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"postflight {__version__} (trace schema v{SCHEMA_VERSION})",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_view = sub.add_parser("view", help="replay a run in the local viewer")
    p_view.add_argument("trace", help="path to a .jsonl trace file")
    p_view.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    p_view.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (localhost only)")
    p_view.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p_view.set_defaults(func=cmd_view)

    p_verify = sub.add_parser("verify", help="check a trace's hash chain")
    p_verify.add_argument("trace", nargs="+", help="one or more .jsonl trace files")
    p_verify.add_argument("--json", action="store_true", help="machine-readable output")
    p_verify.set_defaults(func=cmd_verify)

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
