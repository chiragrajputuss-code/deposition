"""OpenAI Python SDK instrumentation.

Wraps ``chat.completions.create`` and ``responses.create`` (sync and async) into
``llm_call`` events. Field names follow the OpenTelemetry GenAI conventions, so
``gen_ai.request.model`` is the model, not ``model``.

Patching happens on the resource *class*, so a client built before
``deposition.init()`` is instrumented too - which is what people actually write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import _common
from ._common import jsonable

if TYPE_CHECKING:  # pragma: no cover
    from .. import Recorder

PROVIDER = "openai"

#: Request parameters worth recording. Everything else is either noise or a
#: client-side concern (timeouts, retries, headers).
REQUEST_PARAMS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "n",
    "stop",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "tool_choice",
    "reasoning_effort",
)


def describe_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"gen_ai.request.model": kwargs.get("model")}

    params = {k: jsonable(kwargs[k]) for k in REQUEST_PARAMS if kwargs.get(k) is not None}
    if params:
        body["request"] = params
    if kwargs.get("messages") is not None:
        body["messages"] = jsonable(kwargs["messages"])
    if kwargs.get("input") is not None:  # the Responses API's name for messages
        body["messages"] = jsonable(kwargs["input"])
    if kwargs.get("instructions") is not None:
        body["instructions"] = jsonable(kwargs["instructions"])
    if kwargs.get("tools"):
        body["tools"] = [_tool_name(t) for t in kwargs["tools"]]
    if kwargs.get("stream"):
        body["streamed"] = True
    return body


def _tool_name(tool: Any) -> Any:
    """The name of an available tool. Schemas are the caller's code, not the run."""
    tool = jsonable(tool)
    if isinstance(tool, dict):
        return tool.get("name") or (tool.get("function") or {}).get("name") or tool.get("type")
    return tool


def describe_response(response: Any) -> tuple[dict[str, Any], list[str]]:
    """Map a response to body fields plus the tool names the model asked for.

    The tool names are what let a later ``tool_call`` event record an *observed*
    causal edge rather than an inferred one - see ADR 004.
    """
    payload = jsonable(response)
    body: dict[str, Any] = {"response": payload}
    tools: list[str] = []

    if not isinstance(payload, dict):
        return body, tools

    if payload.get("id"):
        body["response_id"] = payload["id"]
    if payload.get("model"):
        body["gen_ai.response.model"] = payload["model"]

    usage = payload.get("usage") or {}
    if isinstance(usage, dict) and usage:
        # Normalised to the names the schema and the platform index on, keeping
        # the provider's own numbers under "response".
        body["usage"] = _normalise_usage(usage)

    for choice in payload.get("choices") or []:
        message = (choice or {}).get("message") or {}
        for call in message.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name") or call.get("name")
            if name:
                tools.append(name)
    for item in payload.get("output") or []:  # Responses API
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name"):
            tools.append(item["name"])

    return body, tools


def _normalise_usage(usage: dict[str, Any]) -> dict[str, Any]:
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    total = usage.get("total_tokens")
    if total is None and isinstance(input_tokens, int) and isinstance(output_tokens, int):
        total = input_tokens + output_tokens
    return {
        k: v
        for k, v in {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total,
        }.items()
        if v is not None
    }


def _targets() -> list[Any]:
    """The resource classes to patch, skipping any this SDK version lacks."""
    found = []
    try:
        from openai.resources.chat.completions import AsyncCompletions, Completions

        found.append((Completions, False))
        found.append((AsyncCompletions, True))
    except Exception:  # noqa: BLE001 - older or restructured SDK
        pass
    try:
        from openai.resources.responses import AsyncResponses, Responses

        found.append((Responses, False))
        found.append((AsyncResponses, True))
    except Exception:  # noqa: BLE001 - Responses API not in this SDK version
        pass
    return found


def install(recorder: Recorder) -> bool:
    """Patch the OpenAI client. Returns False when it is not installed."""
    try:
        import openai  # noqa: F401
    except ImportError:
        return False

    patched = False
    for owner, is_async in _targets():
        wrapper = _common.wrap_async if is_async else _common.wrap
        patched |= _common.patch(
            owner,
            "create",
            lambda original, _w=wrapper: _w(
                recorder,
                original,
                provider=PROVIDER,
                describe_request=describe_request,
                describe_response=describe_response,
            ),
        )
    return patched
