"""Tool-execution capture for agent frameworks.

The LLM adapters record what the model *asked for*. Under a framework that was
all anyone got: the framework runs the tool itself, so a trace read
``llm_call -> llm_call`` with no record that anything happened in between, and
``caused_by`` had nothing to link. The causal graph - the thing the product rests
on - was empty for exactly the users most likely to try it.

Every framework funnels tool execution through one method, so one patch per
framework covers every tool the user wrote, with no change to their code:

* LangChain / LangGraph - ``BaseTool.run`` and ``BaseTool.arun``
* CrewAI - ``BaseTool.run``
* OpenAI Agents SDK - the callable a ``FunctionTool`` holds

No ``caused_by`` is passed. The recorder links the tool call to the model turn
that named this tool, so the edge in the trace is one it observed rather than
one inferred from adjacency.
"""

from __future__ import annotations

import contextvars
import time
from typing import TYPE_CHECKING, Any

from . import _common
from ._common import jsonable

if TYPE_CHECKING:  # pragma: no cover
    from .. import Recorder

__all__ = ["install", "installed_frameworks"]

_frameworks: list[str] = []

#: Depth of patched tool-execution frames on this context.
#:
#: Frameworks layer their own tool classes - CrewAI calls ``Tool.run``, which a
#: future version could route through ``BaseTool.run``, and both are patched.
#: Recording at every layer would write the same execution two or three times,
#: and a duplicated event in an evidentiary record is its own kind of lie. Only
#: the outermost frame records.
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("deposition_tool_depth", default=0)


def installed_frameworks() -> list[str]:
    return list(_frameworks)


def _emit(
    recorder: Recorder,
    *,
    tool: str,
    arguments: Any,
    started: float,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    """Record one tool execution, or nothing if no run is open."""
    run = recorder.run
    if run is None:
        return
    body: dict[str, Any] = {
        "tool": tool,
        "arguments": jsonable(arguments),
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
    }
    if error is not None:
        body["error"] = {"exception": type(error).__name__, "message": str(error)}
    else:
        body["result"] = jsonable(result)
    run.emit("tool_call", body)


def _wrap_sync(recorder: Recorder, original: Any, name_of: Any) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _depth.get():
            return original(self, *args, **kwargs)
        token = _depth.set(1)
        started = time.monotonic()
        try:
            result = original(self, *args, **kwargs)
        except BaseException as exc:
            _depth.reset(token)
            _common._safely(
                _emit,
                recorder,
                tool=name_of(self),
                arguments=args or kwargs,
                started=started,
                error=exc,
            )
            raise
        _depth.reset(token)
        _common._safely(
            _emit,
            recorder,
            tool=name_of(self),
            arguments=args or kwargs,
            started=started,
            result=result,
        )
        return result

    return wrapper


def _wrap_async(recorder: Recorder, original: Any, name_of: Any) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _depth.get():
            return await original(self, *args, **kwargs)
        token = _depth.set(1)
        started = time.monotonic()
        try:
            result = await original(self, *args, **kwargs)
        except BaseException as exc:
            _depth.reset(token)
            _common._safely(
                _emit,
                recorder,
                tool=name_of(self),
                arguments=args or kwargs,
                started=started,
                error=exc,
            )
            raise
        _depth.reset(token)
        _common._safely(
            _emit,
            recorder,
            tool=name_of(self),
            arguments=args or kwargs,
            started=started,
            result=result,
        )
        return result

    return wrapper


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or type(tool).__name__)


# -- per framework -----------------------------------------------------------


def _install_langchain(recorder: Recorder) -> bool:
    try:
        from langchain_core.tools.base import BaseTool
    except ImportError:
        return False
    patched = _common.patch(BaseTool, "run", lambda o: _wrap_sync(recorder, o, _tool_name))
    patched |= _common.patch(BaseTool, "arun", lambda o: _wrap_async(recorder, o, _tool_name))
    return patched


def _install_crewai(recorder: Recorder) -> bool:
    """CrewAI converts every tool into a ``CrewStructuredTool`` and calls that.

    Patching ``BaseTool.run`` - the method the user's own tool class exposes -
    catches nothing, because the agent never calls it. ``invoke`` on the
    converted tool is the real execution point.
    """
    patched = False
    try:
        from crewai.tools.structured_tool import CrewStructuredTool

        patched |= _common.patch(
            CrewStructuredTool, "invoke", lambda o: _wrap_sync(recorder, o, _tool_name)
        )
        patched |= _common.patch(
            CrewStructuredTool, "ainvoke", lambda o: _wrap_async(recorder, o, _tool_name)
        )
    except ImportError:
        pass

    try:
        from crewai.tools.base_tool import BaseTool, Tool

        # `Tool` is what the @tool decorator produces and it defines its own
        # `run`, shadowing BaseTool's - patching only the base class catches
        # nothing at all. Both are patched; the depth guard keeps it to one event.
        patched |= _common.patch(Tool, "run", lambda o: _wrap_sync(recorder, o, _tool_name))
        patched |= _common.patch(BaseTool, "run", lambda o: _wrap_sync(recorder, o, _tool_name))
    except ImportError:
        pass
    return patched


def _install_agents_sdk(recorder: Recorder) -> bool:
    """The Agents SDK keeps its callable in a dataclass field, not on the class.

    So the patch goes on construction: every FunctionTool built after ``init()``
    gets its ``on_invoke_tool`` wrapped. Tools are built at import or decoration
    time, which is why this has to survive being applied to a dataclass.
    """
    try:
        from agents.tool import FunctionTool
    except ImportError:
        return False

    def factory(original: Any) -> Any:
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            original(self, *args, **kwargs)
            invoke = getattr(self, "on_invoke_tool", None)
            if invoke is None or _common.instrumented(invoke):
                return None

            async def wrapped(ctx: Any, input_json: Any) -> Any:
                started = time.monotonic()
                try:
                    result = await invoke(ctx, input_json)
                except BaseException as exc:
                    _common._safely(
                        _emit,
                        recorder,
                        tool=_tool_name(self),
                        arguments=input_json,
                        started=started,
                        error=exc,
                    )
                    raise
                _common._safely(
                    _emit,
                    recorder,
                    tool=_tool_name(self),
                    arguments=input_json,
                    started=started,
                    result=result,
                )
                return result

            wrapped.__deposition_wrapped__ = True
            object.__setattr__(self, "on_invoke_tool", wrapped)
            return None

        return wrapper

    return _common.patch(FunctionTool, "__init__", factory)


_ADAPTERS = (
    ("langchain", _install_langchain),
    ("crewai", _install_crewai),
    ("openai-agents", _install_agents_sdk),
)


def install(recorder: Recorder) -> list[str]:
    """Patch whichever frameworks are importable. Returns what was patched."""
    patched: list[str] = []
    for name, adapter in _ADAPTERS:
        if name in _frameworks:
            continue
        try:
            if adapter(recorder):
                _frameworks.append(name)
                patched.append(name)
        except Exception:  # noqa: BLE001 - instrumentation is never worth a crash
            continue
    return patched
