"""Local replay viewer: a FastAPI app serving a pre-built single-page UI.

``deposition view run.jsonl`` must work offline with zero setup, so the UI is a
static bundle shipped inside the wheel - there is no build step at runtime and
no CDN request at page load.

FastAPI and uvicorn come from the ``viewer`` extra. The SDK itself never imports
this module, so the core stays dependency-free.
"""

from __future__ import annotations

import os
import threading
import webbrowser
from pathlib import Path
from typing import Any

from ..chain import read_jsonl, verify
from ..schema import is_blob_ref

__all__ = ["load_trace", "create_app", "serve"]

STATIC_DIR = Path(__file__).parent / "static"


def load_trace(
    path: str | os.PathLike[str], *, public_key: str | None = None
) -> dict[str, Any]:
    """Read a trace and establish what can be claimed about it, in one pass.

    Three separate questions, never collapsed into one badge:

    * **Integrity** - does the chain hold? Catches an edit, a deletion, a reorder.
    * **Authorship** - is it signed, and by the key the viewer expected? A chain
      alone can be edited and re-sealed by whoever holds the file; only a
      signature over a key they do not have catches that.
    * **Completeness** - did anything fail to reach the record? A chain proves
      nothing was removed after writing. It cannot prove anything was written,
      so the recorder's own counts of dropped and adopted events are the only
      evidence there is, and they belong on screen.

    The viewer always shows all three, including for a broken trace: a tampered
    run is exactly the run you most want to look at.
    """
    pairs = list(read_jsonl(path))
    events = [payload for _, payload in pairs]
    result = verify(events, lines=[lineno for lineno, _ in pairs])
    valid = [e for e in events if isinstance(e, dict)]

    run_start = next((e for e in valid if e.get("type") == "run_start"), None)
    run_end = next((e for e in valid if e.get("type") == "run_end"), None)

    signature = _signature_status(path, result, public_key)
    return {
        "trace": {
            "path": str(path),
            "name": Path(path).name,
        },
        "signature": signature,
        "completeness": _completeness(valid, run_end),
        "authority": _authority(valid),
        "digest": _digest(valid),
        "run": {
            "run_id": result.run_id,
            "agent": (run_start or {}).get("body", {}).get("agent"),
            "project": (run_start or {}).get("body", {}).get("project"),
            "started_at": (run_start or {}).get("ts"),
            "ended_at": (run_end or {}).get("ts"),
            "status": (run_end or {}).get("body", {}).get("status"),
            "totals": (run_end or {}).get("body", {}).get("totals") or {},
            "env": (run_start or {}).get("body", {}).get("env") or {},
        },
        "integrity": {
            "ok": result.ok,
            "events_checked": result.events_checked,
            "head_hash": result.head_hash,
            "errors": [
                {"code": e.code, "message": e.message, "seq": e.seq, "line": e.line}
                for e in result.errors
            ],
        },
        "events": valid,
    }


def _signature_status(
    path: str | os.PathLike[str], result: Any, public_key: str | None
) -> dict[str, Any]:
    """Whether a signature stands, in the terms the UI reports."""
    from .. import signing

    try:
        sidecar = signing.read_sidecar(path)
    except signing.SignatureError as exc:
        return {"status": signing.MALFORMED, "message": str(exc), "ok": False, "pinned": False}

    verdict = signing.verify_sidecar(
        sidecar,
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=public_key,
    )
    return {
        "status": verdict.status,
        "message": verdict.message,
        "ok": verdict.ok,
        "pinned": public_key is not None,
        "public_key": verdict.public_key,
        "signed_at": verdict.signed_at,
    }


def _completeness(events: list[dict[str, Any]], run_end: dict[str, Any] | None) -> dict[str, Any]:
    """What the recorder itself admits it lost or guessed.

    The chain cannot speak to this. If an event never reached the recorder there
    is no gap in the chain to find, so these counts - which the recorder writes
    into its own last event - are the only signal a reader has.
    """
    totals = (run_end or {}).get("body", {}).get("totals") or {}
    dropped = int(totals.get("dropped") or 0)
    adopted = int(totals.get("adopted") or 0)
    ended = run_end is not None

    if not ended:
        status, message = "truncated", (
            "no run_end - the run was interrupted or the recorder never finished writing"
        )
    elif dropped:
        status, message = "lossy", (
            f"{dropped} event(s) were dropped before reaching the trace"
        )
    elif adopted:
        status, message = "inferred", (
            f"{adopted} event(s) arrived with no run context and were attributed by "
            "elimination, not by observation"
        )
    else:
        status, message = "complete", "the recorder reports no dropped or inferred events"

    return {
        "status": status,
        "message": message,
        "ok": status == "complete",
        "dropped": dropped,
        "adopted": adopted,
        "events": len(events),
    }


def _authority(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Was the run within the mandate it carries? Absence is neutral, not a
    defect - most runs will not carry one."""
    from ..mandate import audit

    result = audit(events)
    if result.mandate is None:
        return {"status": "none", "ok": None, "message": "no mandate declared for this run"}
    if result.violations:
        return {
            "status": "violations",
            "ok": False,
            "count": len(result.violations),
            "message": "; ".join(str(v) for v in result.violations[:3]),
            "seqs": [v.seq for v in result.violations],
            # Each violation with its own words. Two rules can fire on one event
            # - a forbidden tool that also received a content-derived value - and
            # the reader needs the specific reason, not the concatenation.
            "violations": [
                {"seq": v.seq, "code": v.code, "message": v.message} for v in result.violations
            ],
        }
    if not result.attestable:
        return {"status": "not_attestable", "ok": False, "message": result.summary()}
    checked = ", ".join(sorted(result.checked)) or "nothing enforceable"
    return {"status": "ok", "ok": True, "message": f"within mandate ({checked} checked)"}


def _digest(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The run compressed to what a reader opens it for.

    A flat list of events is legible at fifty and noise at a thousand, so the
    viewer leads with this and keeps the full list one click away. Episodes are
    cut at human prompts - the natural chapters of an interactive session - and
    moments are the events someone is actually looking for: things that changed
    the world outside, failures, authority violations, and the turns that blew
    up the context window. Everything here is a pointer into the full record,
    never a replacement for it.
    """
    episodes: list[dict[str, Any]] = []
    moments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def clip(text: Any, n: int = 90) -> str:
        text = str(text or "").strip().replace("\n", " ")
        return text[: n - 1] + "…" if len(text) > n else text

    deltas = sorted(
        (int(e["body"].get("context_delta") or 0), e["seq"])
        for e in events
        if e.get("type") == "llm_call" and e.get("body", {}).get("context_delta")
    )
    # "A spike" means relative to this run, not an absolute number: the five
    # largest jumps, provided they are big enough to be worth a glance at all.
    spikes = {seq for delta, seq in deltas[-5:] if delta >= 2000}

    for event in events:
        kind = event.get("type")
        body = event.get("body") or {}
        seq = event.get("seq")

        if kind == "annotation" and body.get("author") == "user":
            current = {
                "prompt": clip(body.get("note")),
                "start": seq,
                "end": seq,
                "turns": 0,
                "tools": 0,
                "mutations": 0,
                "errors": 0,
                "tokens": 0,
            }
            episodes.append(current)
            continue

        if current is not None:
            current["end"] = seq
            if kind == "llm_call":
                current["turns"] += 1
                current["tokens"] += int((body.get("usage") or {}).get("total_tokens") or 0)
            elif kind == "tool_call":
                current["tools"] += 1
                if body.get("mutates_environment"):
                    current["mutations"] += 1
                if body.get("error"):
                    current["errors"] += 1

        if kind == "tool_call":
            label = body.get("tool") or "tool"
            detail = body.get("arguments")
            if isinstance(detail, dict):
                detail = detail.get("command") or detail.get("file_path") or detail
            if body.get("error"):
                moments.append({"kind": "error", "seq": seq, "label": label,
                                "detail": clip(body["error"].get("message"))})
            elif body.get("mutates_environment"):
                moments.append({"kind": "mutation", "seq": seq, "label": label,
                                "detail": clip(detail)})
        elif kind == "error":
            moments.append({"kind": "error", "seq": seq,
                            "label": body.get("exception") or "error",
                            "detail": clip(body.get("message"))})
        elif kind == "llm_call" and seq in spikes:
            moments.append({"kind": "spike", "seq": seq, "label": "context jump",
                            "detail": f"+{int(body.get('context_delta') or 0):,} tokens "
                                      f"to {int(body.get('context_tokens') or 0):,}"})

    authority = _authority(events)
    # One event, one moment: the most specific rule wins when several fire.
    PRECEDENCE = {"tainted_external": 0, "forbidden_tool": 1, "tool_not_allowed": 2}
    best: dict[int, dict[str, Any]] = {}
    for violation in authority.get("violations") or []:
        rank = PRECEDENCE.get(violation["code"], 9)
        if violation["seq"] not in best or rank < best[violation["seq"]]["_rank"]:
            best[violation["seq"]] = dict(violation, _rank=rank)
    for seq, violation in sorted(best.items()):
        moments.append({
            "kind": "violation", "seq": seq,
            "label": violation["code"].replace("_", " "),
            "detail": violation["message"],
        })

    # Provenance, not intent: a value handed to a tool that came from content the
    # agent read rather than from the instruction. Ordinary retrieval produces
    # these too, so it is shown as a question, never as an accusation - it is
    # the pairing with a violation that makes one damning.
    from ..taint import tainted_arguments

    start = next((e for e in events if e.get("type") == "run_start"), None)
    declared_external = set(
        ((start or {}).get("body", {}).get("mandate") or {}).get("external_tools") or ()
    )
    violation_seqs = set(authority.get("seqs") or [])
    for finding in tainted_arguments(events):
        if finding.seq in violation_seqs:
            continue  # already reported as a violation; one event, one moment
        moments.append({
            "kind": "tainted", "seq": finding.seq,
            "label": f"{finding.tool} argument from content",
            "severity": "alert" if finding.tool in declared_external else "context",
            "detail": f"{finding.field}={clip(finding.value, 48)} first appeared in "
                      f"{finding.source_tool}'s result (seq {finding.source_seq}), "
                      f"not in the instruction",
        })

    moments.sort(key=lambda m: m["seq"])
    contexts = [int(e["body"].get("context_tokens") or 0) for e in events
                if e.get("type") == "llm_call" and e.get("body", {}).get("context_tokens")]
    return {
        "episodes": episodes,
        "moments": moments,
        "context": {"first": contexts[0], "last": contexts[-1], "peak": max(contexts)}
        if contexts else None,
    }


def _blob_path(trace_path: str | os.PathLike[str], digest: str) -> Path:
    return Path(trace_path).parent / "blobs" / digest


def create_app(trace_path: str | os.PathLike[str], *, public_key: str | None = None):
    """Build the FastAPI app for one trace file."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

    trace_path = Path(trace_path).resolve()
    app = FastAPI(title="Deposition viewer", docs_url=None, redoc_url=None)

    @app.get("/api/trace")
    def api_trace() -> JSONResponse:
        # Re-read on every request: a run still being written should be
        # refreshable without restarting the viewer.
        return JSONResponse(load_trace(trace_path, public_key=public_key))

    @app.get("/api/blob/{digest}")
    def api_blob(digest: str) -> PlainTextResponse:
        if not digest.isalnum() or len(digest) != 64:
            raise HTTPException(status_code=400, detail="not a blob digest")
        blob = _blob_path(trace_path, digest)
        if not blob.is_file():
            raise HTTPException(status_code=404, detail="blob not found next to this trace")
        return PlainTextResponse(blob.read_text(encoding="utf-8", errors="replace"))

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def serve(
    trace_path: str | os.PathLike[str],
    *,
    host: str = "127.0.0.1",
    port: int = 7878,
    open_browser: bool = True,
    public_key: str | None = None,
) -> int:
    """Run the viewer until interrupted. Returns a process exit code."""
    import uvicorn

    app = create_app(trace_path, public_key=public_key)
    url = f"http://{host}:{port}"

    print(f"deposition: serving replay viewer on {url}")
    print("deposition: press Ctrl+C to stop")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    return 0


def resolve_blob_refs(body: Any, trace_path: str | os.PathLike[str]) -> Any:
    """Inline blob contents into a body. Used by exports, not by the live UI."""
    if is_blob_ref(body):
        blob = _blob_path(trace_path, body["$blob"])
        if blob.is_file():
            return blob.read_text(encoding="utf-8", errors="replace")
        return body
    if isinstance(body, dict):
        return {k: resolve_blob_refs(v, trace_path) for k, v in body.items()}
    if isinstance(body, list):
        return [resolve_blob_refs(v, trace_path) for v in body]
    return body
