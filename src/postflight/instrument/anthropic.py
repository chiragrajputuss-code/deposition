"""Anthropic Python SDK instrumentation.

Wraps ``messages.create`` (sync and async) into ``llm_call`` events. Same shape
as the OpenAI adapter - the schema is provider neutral, ``body.provider`` is what
differs.

``messages.stream()`` is **not** covered in v0: it returns a context manager that
does not route through ``create``, and wrapping it safely means owning the
caller's iteration. See ADR 004.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import _common
from ._common import jsonable

if TYPE_CHECKING:  # pragma: no cover
    from .. import Recorder

PROVIDER = "anthropic"

REQUEST_PARAMS = (
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "stop_sequences",
    "tool_choice",
    "thinking",
)


def describe_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"gen_ai.request.model": kwargs.get("model")}

    params = {k: jsonable(kwargs[k]) for k in REQUEST_PARAMS if kwargs.get(k) is not None}
    if params:
        body["request"] = params
    if kwargs.get("messages") is not None:
        body["messages"] = jsonable(kwargs["messages"])
    if kwargs.get("system") is not None:
        body["system"] = jsonable(kwargs["system"])
    if kwargs.get("tools"):
        body["tools"] = [_tool_name(t) for t in kwargs["tools"]]
    if kwargs.get("stream"):
        body["streamed"] = True
    return body


def _tool_name(tool: Any) -> Any:
    tool = jsonable(tool)
    if isinstance(tool, dict):
        return tool.get("name") or tool.get("type")
    return tool


def describe_response(response: Any) -> tuple[dict[str, Any], list[str]]:
    """Map a Message to body fields plus the tool names the model asked for."""
    payload = jsonable(response)
    body: dict[str, Any] = {"response": payload}
    tools: list[str] = []

    if not isinstance(payload, dict):
        return body, tools

    if payload.get("id"):
        body["response_id"] = payload["id"]
    if payload.get("model"):
        body["gen_ai.response.model"] = payload["model"]
    if payload.get("stop_reason"):
        body["stop_reason"] = payload["stop_reason"]

    usage = payload.get("usage") or {}
    if isinstance(usage, dict) and usage:
        body["usage"] = _normalise_usage(usage)

    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
            tools.append(block["name"])

    return body, tools


def _normalise_usage(usage: dict[str, Any]) -> dict[str, Any]:
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total = None
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        total = input_tokens + output_tokens
    normalised = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
    }
    # Cache tokens are billed differently and are worth keeping when present.
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if usage.get(key) is not None:
            normalised[key] = usage[key]
    return {k: v for k, v in normalised.items() if v is not None}


def _targets() -> list[Any]:
    try:
        from anthropic.resources.messages import AsyncMessages, Messages

        return [(Messages, False), (AsyncMessages, True)]
    except Exception:  # noqa: BLE001 - older or restructured SDK
        return []


def install(recorder: Recorder) -> bool:
    """Patch the Anthropic client. Returns False when it is not installed."""
    try:
        import anthropic  # noqa: F401
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
