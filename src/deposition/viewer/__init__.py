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


def load_trace(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a trace and verify it in one pass.

    The viewer always shows the integrity verdict, including for a broken trace:
    a tampered run is exactly the run you most want to look at.
    """
    pairs = list(read_jsonl(path))
    events = [payload for _, payload in pairs]
    result = verify(events, lines=[lineno for lineno, _ in pairs])
    valid = [e for e in events if isinstance(e, dict)]

    run_start = next((e for e in valid if e.get("type") == "run_start"), None)
    run_end = next((e for e in valid if e.get("type") == "run_end"), None)

    return {
        "trace": {
            "path": str(path),
            "name": Path(path).name,
        },
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


def _blob_path(trace_path: str | os.PathLike[str], digest: str) -> Path:
    return Path(trace_path).parent / "blobs" / digest


def create_app(trace_path: str | os.PathLike[str]):
    """Build the FastAPI app for one trace file."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

    trace_path = Path(trace_path).resolve()
    app = FastAPI(title="Deposition viewer", docs_url=None, redoc_url=None)

    @app.get("/api/trace")
    def api_trace() -> JSONResponse:
        # Re-read on every request: a run still being written should be
        # refreshable without restarting the viewer.
        return JSONResponse(load_trace(trace_path))

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
) -> int:
    """Run the viewer until interrupted. Returns a process exit code."""
    import uvicorn

    app = create_app(trace_path)
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
