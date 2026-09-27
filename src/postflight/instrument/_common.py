"""Shared machinery for auto-instrumentation.

Three rules hold everywhere in this package:

1. **Patching is best-effort.** If a client is not installed, or its internals
   moved between versions, instrumentation stays off. A missing trace is a
   disappointment; a crashed agent is a bug report.
2. **Recording never changes what the caller sees.** The wrapper returns the
   provider's own object, untouched, and re-raises the provider's own exception.
3. **Everything that ends up in a body must survive canonical JSON.** SDK
   responses are pydantic models, not dicts, so they go through
   :func:`jsonable` before they reach the chain.
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..schema import EventType

if TYPE_CHECKING:  # pragma: no cover
    from .. import Recorder

__all__ = ["jsonable", "patch", "revert_all", "instrumented", "record_llm_call"]

#: How deep to walk a response before giving up and stringifying. Guards against
#: cyclic or pathologically nested provider objects.
MAX_DEPTH = 12


def jsonable(value: Any, _depth: int = 0) -> Any:
    """Coerce a provider object into something ``canonical_json`` can serialise.

    Unknown objects become their ``str()`` rather than raising: a slightly lossy
    record of an LLM call beats no record of it.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # canonical JSON forbids NaN/Infinity, and a provider returning one must
        # not be able to silently drop the whole event.
        return value if math.isfinite(value) else repr(value)
    if _depth >= MAX_DEPTH:
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, _depth + 1) for v in value]

    for method in ("model_dump", "dict", "to_dict"):
        dump = getattr(value, method, None)
        if callable(dump):
            try:
                return jsonable(dump(), _depth + 1)
            except Exception:  # noqa: BLE001 - fall through to the next strategy
                continue
    return str(value)


# -- patch registry ---------------------------------------------------------


@dataclass(frozen=True)
class _Patch:
    owner: Any
    name: str
    original: Callable[..., Any]


_patches: list[_Patch] = []


def instrumented(fn: Any) -> bool:
    """Has this callable already been wrapped by Postflight?"""
    return getattr(fn, "__postflight_wrapped__", False)


def patch(
    owner: Any, name: str, factory: Callable[[Callable[..., Any]], Callable[..., Any]]
) -> bool:
    """Replace ``owner.name`` with ``factory(original)``. Returns whether it happened.

    Patching the class rather than an instance means clients created *before*
    ``postflight.init()`` are instrumented too - which is what people actually
    write, since the client is usually a module-level global.
    """
    original = getattr(owner, name, None)
    if original is None or not callable(original) or instrumented(original):
        return False

    wrapper = factory(original)
    wrapper.__postflight_wrapped__ = True
    setattr(owner, name, wrapper)
    _patches.append(_Patch(owner, name, original))
    return True


def revert_all() -> None:
    """Undo every patch, most recent first. Used by ``uninstall()`` and by tests."""
    while _patches:
        entry = _patches.pop()
        try:
            setattr(entry.owner, entry.name, entry.original)
        except Exception:  # noqa: BLE001 - a failed revert must not cascade
            continue


# -- recording --------------------------------------------------------------


def record_llm_call(
    recorder: Recorder,
    *,
    provider: str,
    request: dict[str, Any],
    started: float,
    response: dict[str, Any] | None = None,
    requested_tools: list[str] | None = None,
    error: BaseException | None = None,
) -> int | None:
    """Emit one ``llm_call`` event. Returns its ``seq``, or ``None`` if not recording."""
    run = recorder.run
    if run is None:
        # Instrumentation records into the active run. A call made outside
        # @postflight.record has no run to belong to.
        return None

    body: dict[str, Any] = {"provider": provider}
    body.update(request)
    body["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    if response:
        body.update(response)
    if error is not None:
        body["error"] = {"exception": error.__class__.__name__, "message": str(error)}

    seq = run.emit(EventType.LLM_CALL, body)
    if seq is not None and requested_tools:
        run.note_tool_requests(seq, requested_tools)
    return seq


def wrap(
    recorder: Recorder,
    original: Callable[..., Any],
    *,
    provider: str,
    describe_request: Callable[[dict[str, Any]], dict[str, Any]],
    describe_response: Callable[[Any], tuple[dict[str, Any], list[str]]],
) -> Callable[..., Any]:
    """Wrap a synchronous provider call."""

    @functools.wraps(original)
    def wrapper(_self: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            request = describe_request(kwargs)
        except Exception:  # noqa: BLE001
            request = {}
        try:
            result = original(_self, *args, **kwargs)
        except BaseException as exc:
            _safely(record_llm_call, recorder, provider=provider, request=request,
                    started=started, error=exc)
            raise
        _safely(_record_success, recorder, provider, request, started, result, describe_response)
        return result

    return wrapper


def wrap_async(
    recorder: Recorder,
    original: Callable[..., Any],
    *,
    provider: str,
    describe_request: Callable[[dict[str, Any]], dict[str, Any]],
    describe_response: Callable[[Any], tuple[dict[str, Any], list[str]]],
) -> Callable[..., Any]:
    """Wrap an asynchronous provider call."""

    @functools.wraps(original)
    async def wrapper(_self: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            request = describe_request(kwargs)
        except Exception:  # noqa: BLE001
            request = {}
        try:
            result = await original(_self, *args, **kwargs)
        except BaseException as exc:
            _safely(record_llm_call, recorder, provider=provider, request=request,
                    started=started, error=exc)
            raise
        _safely(_record_success, recorder, provider, request, started, result, describe_response)
        return result

    return wrapper


def _record_success(
    recorder: Recorder,
    provider: str,
    request: dict[str, Any],
    started: float,
    result: Any,
    describe_response: Callable[[Any], tuple[dict[str, Any], list[str]]],
) -> None:
    if request.get("streamed"):
        # Consuming the stream to capture it would change the caller's object and
        # risk breaking their agent. v0 records that a stream happened and says
        # plainly that the response was not captured - see ADR 004.
        response, tools = {"response": {"streamed": True, "captured": False}}, []
    else:
        response, tools = describe_response(result)
    record_llm_call(
        recorder,
        provider=provider,
        request=request,
        started=started,
        response=response,
        requested_tools=tools,
    )


def _safely(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run a recording step, swallowing anything it raises."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - recording is never worth the host's crash
        pass
