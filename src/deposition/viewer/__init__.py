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
