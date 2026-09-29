"""Deposition - a verbatim, tamper-evident record of an AI agent run.

    import deposition

    deposition.init(project="support-triage")      # local mode

    @deposition.record(name="triage-agent")
    def run_agent(ticket): ...

    with deposition.step("decision", label="issue refund", caused_by=[13]): ...

Two promises hold this SDK together:

1. **Deposition failing must never fail your app.** Every public entry point is
   wrapped; the worst case is a missing trace, never a broken agent.
2. **A trace is tamper-evident.** Events are hash-chained as they are recorded,
   and ``deposition verify`` will say so if anything moved.
"""

from __future__ import annotations

import contextlib
import functools
import os
import platform
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from . import chain as _chain
from .chain import ChainBuilder, ChainResult, verify_file
from .exporters.jsonl import JsonlExporter
from .schema import SCHEMA_VERSION, Event, EventType, SchemaError

__version__ = "0.1.0.dev0"
__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "init",
    "record",
    "step",
    "log",
    "annotate",
    "current_run",
    "flush",
    "shutdown",
    "verify_file",
    "Recorder",
    "Run",
    "EventType",
    "Event",
    "SchemaError",
    "ChainResult",
]

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _new_run_id() -> str:
    return "run_" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def _env_fingerprint() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "sdk_version": __version__,
        "pid": os.getpid(),
    }


class Run:
    """One recorded run: a chain, an exporter and the events in between."""

    def __init__(self, recorder: Recorder, run_id: str, agent: str) -> None:
        self.recorder = recorder
        self.run_id = run_id
        self.agent = agent
        self.started = time.monotonic()
        self.chain = ChainBuilder(run_id)
        self.exporter = JsonlExporter(run_id, recorder.directory)
        self.totals = {"steps": 0, "tokens": 0, "cost_usd": 0.0}
        self.ended = False
        self._lock = threading.Lock()
        #: tool name -> seq of the llm_call whose response asked for it. Filled by
        #: auto-instrumentation and used to record observed causal edges.
        self.tool_requests: dict[str, int] = {}

    @property
    def path(self) -> str:
        return str(self.exporter.path)

    def emit(
        self,
        type: EventType | str,
        body: dict[str, Any] | None = None,
        *,
        caused_by: Iterable[int] | None = None,
    ) -> int | None:
        """Seal and export one event; returns its ``seq`` (useful for ``caused_by``)."""
        body = dict(body or {})
        if self.recorder.redact is not None:
            body = self.recorder.redact(body)
        body = self.exporter.externalize_body(body)
        caused_by = self._observed_cause(type, body, caused_by)
        with self._lock:
            sealed = self.chain.append(type, body, caused_by=caused_by)
            if sealed["type"] not in (EventType.RUN_START.value, EventType.RUN_END.value):
                self.totals["steps"] += 1
            usage = body.get("usage") or {}
            if isinstance(usage, dict):
                self.totals["tokens"] += int(usage.get("total_tokens") or 0)
            if isinstance(body.get("cost_usd"), (int, float)):
                self.totals["cost_usd"] += float(body["cost_usd"])
        self.exporter.export(sealed)
        return sealed["seq"]

    def note_tool_requests(self, seq: int, tools: Iterable[str]) -> None:
        """Remember which ``llm_call`` asked for which tools."""
        for name in tools:
            if isinstance(name, str):
                self.tool_requests[name] = seq

    def _observed_cause(
        self,
        type: EventType | str,
        body: dict[str, Any],
        caused_by: Iterable[int] | None,
    ) -> Iterable[int] | None:
        """Link a tool call to the model output that asked for it.

        Only an *observed* edge: the tool name has to appear in the response of an
        ``llm_call`` we already recorded. Nothing is inferred from adjacency - an
        unexplained tool call keeps no edge at all, because a causal graph nobody
        can trust is worse than a sparse one. An explicit ``caused_by`` always wins.
        """
        if caused_by is not None or str(type) != EventType.TOOL_CALL.value:
            return caused_by
        tool = body.get("tool")
        if not isinstance(tool, str):
            return None
        seq = self.tool_requests.get(tool)
        return [seq] if seq is not None else None

    def end(self, status: str = "ok", **extra: Any) -> None:
        if self.ended:
            return
        self.ended = True
        totals = dict(self.totals, duration_ms=round((time.monotonic() - self.started) * 1000, 3))
        self.emit(EventType.RUN_END, {"status": status, "totals": totals, **extra})
        self.exporter.close()
        self._sign()

    def _sign(self) -> None:
        """Write ``run_<id>.sig`` beside the trace, if a signing key was configured.

        Runs after the exporter is closed, so the signature only ever exists for
        a trace that is completely on disk. A failure here is reported and
        swallowed: the run is already over, and a missing sidecar is a far
        smaller harm than an agent that dies at teardown.
        """
        key = self.recorder.signing_key
        if key is None:
            return
        try:
            from . import signing

            sidecar = signing.sign_head(
                key,
                run_id=self.run_id,
                head_hash=self.chain.head_hash,
                seq=self.chain.next_seq - 1,
                events=self.chain.next_seq,
            )
            signing.write_sidecar(sidecar, self.exporter.path)
        except Exception as exc:  # noqa: BLE001 - never surface into the host agent
            print(f"deposition: could not sign {self.path}: {exc}", file=sys.stderr)


class Recorder:
    """Process-wide recording configuration, created by :func:`init`."""

    def __init__(
        self,
        project: str,
        *,
        directory: str | os.PathLike[str] = "./deposition",
        token: str | None = None,
        redact: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        instrument: bool = True,
        config: dict[str, Any] | None = None,
        signing_key: Any | None = None,
    ) -> None:
        self.project = project
        self.directory = directory
        self.token = token
        self.redact = redact
        # Loaded here, not at first use: a key that cannot be loaded is a
        # configuration error, and the only honest place to raise is before any
        # run starts. Downgrading to unsigned traces silently would leave the
        # user believing in signatures they do not have.
        self.signing_key = None
        if signing_key is not None:
            from .signing import load_key

            self.signing_key = load_key(signing_key)
        self.config = config or {}
        self.instrument = instrument
        self._local = threading.local()
        self._runs: list[Run] = []

        if token:
            # Hosted mode is Phase 2. Say so rather than silently recording locally
            # under a name that implies upload.
            print(
                "deposition: hosted mode is not available yet (Phase 2); "
                "recording locally instead.",
                file=sys.stderr,
            )

    # -- run lifecycle ------------------------------------------------------

    @property
    def run(self) -> Run | None:
        return getattr(self._local, "run", None)

    def start_run(self, agent: str) -> Run:
        run = Run(self, _new_run_id(), agent)
        self._local.run = run
        self._runs.append(run)
        run.emit(
            EventType.RUN_START,
            {
                "agent": agent,
                "project": self.project,
                "sdk_version": __version__,
                "env": _env_fingerprint(),
                "config": self.config,
            },
        )
        return run

    def finish_run(self, run: Run, status: str = "ok") -> None:
        run.end(status)
        if getattr(self._local, "run", None) is run:
            self._local.run = None

    def flush(self, timeout: float = 5.0) -> None:
        for run in self._runs:
            run.exporter.flush(timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        for run in self._runs:
            if not run.ended:
                run.end("interrupted")
            run.exporter.close(timeout)
        self._runs.clear()


_recorder: Recorder | None = None


def init(
    project: str,
    *,
    directory: str | os.PathLike[str] = "./deposition",
    token: str | None = None,
    redact: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    instrument: bool = True,
    config: dict[str, Any] | None = None,
    signing_key: str | os.PathLike[str] | bytes | None = None,
) -> Recorder:
    """Configure recording for this process.

    ``redact`` is called on every event body before anything is hashed or
    written - the only place to strip PII out of prompts. Use it.

    ``signing_key`` is an Ed25519 private key - a path to a PEM file, the PEM
    bytes, or a 32-byte raw key - and needs the ``signing`` extra. Each run then
    writes ``run_<id>.sig`` beside its trace. Read ``docs/decisions/005`` before
    relying on it: a key stored beside the traces it signs raises the bar, it
    does not settle the question.
    """
    global _recorder
    _recorder = Recorder(
        project,
        directory=directory,
        token=token,
        redact=redact,
        instrument=instrument,
        config=config,
        signing_key=signing_key,
    )
    if instrument:
        from .instrument import install

        install(_recorder)
    return _recorder


def current_run() -> Run | None:
    """The run being recorded on this thread, if any."""
    return _recorder.run if _recorder else None


def record(name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: record one run per call of the wrapped function."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        agent = name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _recorder is None:
                return fn(*args, **kwargs)
            try:
                run = _recorder.start_run(agent)
            except Exception:  # noqa: BLE001 - recording must never break the agent
                return fn(*args, **kwargs)
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                with contextlib.suppress(Exception):
                    run.emit(
                        EventType.ERROR,
                        {
                            "exception": type(exc).__name__,
                            "message": str(exc),
                            "stack": "".join(
                                traceback.format_exception_only(type(exc), exc)
                            ).strip(),
                        },
                    )
                    _recorder.finish_run(run, "error")
                raise
            with contextlib.suppress(Exception):
                _recorder.finish_run(run, "ok")
            return result

        return wrapper

    return decorator


@contextlib.contextmanager
def step(
    type: EventType | str = EventType.DECISION,
    *,
    caused_by: Iterable[int] | None = None,
    **body: Any,
) -> Iterator[dict[str, Any]]:
    """Record one step, timing it and capturing any exception it raises.

    The yielded dict is the event body; mutate it inside the block to attach a
    result. The event is written on exit, so its ``ts`` marks completion.
    """
    payload: dict[str, Any] = dict(body)
    started = time.monotonic()
    run = current_run()
    try:
        yield payload
    except BaseException as exc:
        if run is not None:
            with contextlib.suppress(Exception):
                # exc.__class__, not type(exc): the ``type`` parameter above
                # shadows the builtin inside this function.
                payload["error"] = {"exception": exc.__class__.__name__, "message": str(exc)}
                payload["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
                run.emit(type, payload, caused_by=caused_by)
        raise
    if run is not None:
        with contextlib.suppress(Exception):
            payload.setdefault("latency_ms", round((time.monotonic() - started) * 1000, 3))
            run.emit(type, payload, caused_by=caused_by)


def log(
    type: EventType | str,
    body: dict[str, Any] | None = None,
    *,
    caused_by: Iterable[int] | None = None,
) -> int | None:
    """Record one event directly. Returns its ``seq``, or ``None`` if not recording."""
    run = current_run()
    if run is None:
        return None
    try:
        return run.emit(type, body, caused_by=caused_by)
    except Exception:  # noqa: BLE001
        return None


def annotate(
    note: str, *, author: str = "user", caused_by: Iterable[int] | None = None
) -> int | None:
    """Attach a free-form note to the trace."""
    return log(EventType.ANNOTATION, {"note": note, "author": author}, caused_by=caused_by)


def flush(timeout: float = 5.0) -> None:
    if _recorder is not None:
        with contextlib.suppress(Exception):
            _recorder.flush(timeout)


def shutdown(timeout: float = 5.0) -> None:
    """Close every open run. Safe to call more than once."""
    global _recorder
    with contextlib.suppress(Exception):
        from .instrument import uninstall

        uninstall()
    if _recorder is not None:
        with contextlib.suppress(Exception):
            _recorder.shutdown(timeout)
        _recorder = None


# Keep a reference so ``deposition.chain`` resolves after ``import deposition``.
chain = _chain
