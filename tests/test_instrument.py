"""Auto-instrumentation tests.

These run against the real OpenAI and Anthropic response models - the mapping is
only worth anything if it holds for the objects those SDKs actually return. The
network call itself is replaced: the resource method is swapped for one that
returns a constructed response, and instrumentation then wraps *that*, which is
exactly the position it occupies in a real process.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import deposition
from deposition import instrument
from deposition.chain import verify_file
from deposition.instrument import _common

anthropic_sdk = pytest.importorskip("anthropic")
openai_sdk = pytest.importorskip("openai")

from anthropic.types import Message, TextBlock, ToolUseBlock, Usage  # noqa: E402
from openai.types.chat import ChatCompletion, ChatCompletionMessage  # noqa: E402
from openai.types.chat.chat_completion import Choice  # noqa: E402
from openai.types.chat.chat_completion_message_tool_call import (  # noqa: E402
    ChatCompletionMessageToolCall,
    Function,
)
from openai.types.completion_usage import CompletionUsage  # noqa: E402

WEATHER_TOOL_OPENAI = {
    "type": "function",
    "function": {"name": "get_weather", "parameters": {"type": "object"}},
}
WEATHER_TOOL_ANTHROPIC = {"name": "get_weather", "input_schema": {"type": "object"}}


def openai_response(*, tool: str | None = "get_weather", text: str | None = None):
    calls = (
        [
            ChatCompletionMessageToolCall(
                id="call_1",
                type="function",
                function=Function(name=tool, arguments='{"city":"Mumbai"}'),
            )
        ]
        if tool
        else None
    )
    return ChatCompletion(
        id="chatcmpl-1",
        model="gpt-4o-mini-2024-07-18",
        object="chat.completion",
        created=0,
        choices=[
            Choice(
                index=0,
                finish_reason="tool_calls" if tool else "stop",
                message=ChatCompletionMessage(role="assistant", content=text, tool_calls=calls),
            )
        ],
        usage=CompletionUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def anthropic_response(*, tool: str | None = "get_weather", text: str | None = None):
    content = []
    if text:
        content.append(TextBlock(type="text", text=text))
    if tool:
        content.append(
            ToolUseBlock(id="toolu_1", name=tool, input={"city": "Mumbai"}, type="tool_use")
        )
    return Message(
        id="msg_1",
        model="claude-sonnet-5",
        role="assistant",
        type="message",
        stop_reason="tool_use" if tool else "end_turn",
        content=content,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class _Provider:
    """Swaps a resource method for a stub, then lets Deposition patch on top."""

    def __init__(self, owner, name, stub):
        self.owner, self.name, self.stub = owner, name, stub
        self.original = getattr(owner, name)
        self.calls: list[dict] = []

    def __enter__(self):
        def recorded(_self, **kwargs):
            self.calls.append(kwargs)
            return self.stub(**kwargs)

        setattr(self.owner, self.name, recorded)
        return self

    def __exit__(self, *exc):
        # Deposition's patch must come off before the stub does, or reverting it
        # would reinstate the stub as if it were the SDK's own method.
        instrument.uninstall()
        setattr(self.owner, self.name, self.original)
        return False


@pytest.fixture
def openai_calls():
    from openai.resources.chat.completions import Completions

    with _Provider(Completions, "create", lambda **kw: openai_response()) as provider:
        yield provider


@pytest.fixture
def anthropic_calls():
    from anthropic.resources.messages import Messages

    with _Provider(Messages, "create", lambda **kw: anthropic_response()) as provider:
        yield provider


def events(path):
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def run_and_read(trace_dir, fn):
    deposition.init("test-project", directory=trace_dir)

    @deposition.record(name="instrumented-agent")
    def agent():
        return fn()

    agent()
    path = deposition._recorder._runs[-1].path
    deposition.shutdown()
    return events(path)


# -- installation -----------------------------------------------------------


def test_init_patches_both_providers(trace_dir, openai_calls, anthropic_calls):
    deposition.init("test-project", directory=trace_dir)
    assert set(instrument.installed()) == {"openai", "anthropic"}


def test_instrument_false_leaves_the_clients_alone(trace_dir, openai_calls):
    deposition.init("test-project", directory=trace_dir, instrument=False)
    assert instrument.installed() == []


def test_shutdown_restores_the_original_methods(trace_dir, openai_calls):
    from openai.resources.chat.completions import Completions

    deposition.init("test-project", directory=trace_dir)
    assert _common.instrumented(Completions.create)
    deposition.shutdown()
    assert not _common.instrumented(Completions.create)
    assert instrument.installed() == []


def test_patching_twice_does_not_double_wrap(trace_dir, openai_calls):
    deposition.init("test-project", directory=trace_dir)
    assert instrument.install(deposition._recorder) == []


def test_install_is_a_no_op_when_a_provider_is_missing(trace_dir, monkeypatch):
    # A user with only one SDK installed must not get an error about the other.
    monkeypatch.setitem(__import__("sys").modules, "anthropic", None)
    deposition.init("test-project", directory=trace_dir)
    assert "anthropic" not in instrument.installed()


# -- OpenAI -----------------------------------------------------------------


def test_an_openai_call_is_recorded(trace_dir, openai_calls):
    client = openai_sdk.OpenAI(api_key="test")
    recorded = run_and_read(
        trace_dir,
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "weather in Mumbai?"}],
            temperature=0.2,
            tools=[WEATHER_TOOL_OPENAI],
        ),
    )
    call = recorded[1]
    assert call["type"] == "llm_call"
    assert call["body"]["provider"] == "openai"
    assert call["body"]["gen_ai.request.model"] == "gpt-4o-mini"
    assert call["body"]["gen_ai.response.model"] == "gpt-4o-mini-2024-07-18"
    assert call["body"]["messages"] == [{"role": "user", "content": "weather in Mumbai?"}]
    assert call["body"]["request"]["temperature"] == 0.2
    assert call["body"]["tools"] == ["get_weather"]
    assert call["body"]["response_id"] == "chatcmpl-1"
    assert call["body"]["latency_ms"] >= 0


def test_openai_usage_is_normalised_to_the_schema_names(trace_dir, openai_calls):
    client = openai_sdk.OpenAI(api_key="test")
    recorded = run_and_read(
        trace_dir,
        lambda: client.chat.completions.create(model="gpt-4o-mini", messages=[]),
    )
    # prompt_tokens/completion_tokens are OpenAI's names; the schema indexes on
    # the OTel-aligned ones.
    assert recorded[1]["body"]["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }


def test_the_provider_object_is_returned_unchanged(trace_dir, openai_calls):
    client = openai_sdk.OpenAI(api_key="test")
    deposition.init("test-project", directory=trace_dir)

    @deposition.record(name="agent")
    def agent():
        return client.chat.completions.create(model="gpt-4o-mini", messages=[])

    result = agent()
    deposition.shutdown()
    assert isinstance(result, ChatCompletion)
    assert result.choices[0].message.tool_calls[0].function.name == "get_weather"


def test_the_caller_still_sees_its_own_arguments(trace_dir, openai_calls):
    client = openai_sdk.OpenAI(api_key="test")
    run_and_read(
        trace_dir,
        lambda: client.chat.completions.create(model="gpt-4o-mini", messages=[], top_p=0.9),
    )
    assert openai_calls.calls[0]["top_p"] == 0.9


# -- Anthropic --------------------------------------------------------------


def test_an_anthropic_call_is_recorded(trace_dir, anthropic_calls):
    client = anthropic_sdk.Anthropic(api_key="test")
    recorded = run_and_read(
        trace_dir,
        lambda: client.messages.create(
            model="claude-sonnet-5",
            max_tokens=512,
            system="You are terse.",
            messages=[{"role": "user", "content": "weather in Mumbai?"}],
            tools=[WEATHER_TOOL_ANTHROPIC],
        ),
    )
    call = recorded[1]
    assert call["body"]["provider"] == "anthropic"
    assert call["body"]["gen_ai.request.model"] == "claude-sonnet-5"
    assert call["body"]["system"] == "You are terse."
    assert call["body"]["request"]["max_tokens"] == 512
    assert call["body"]["tools"] == ["get_weather"]
    assert call["body"]["stop_reason"] == "tool_use"
    assert call["body"]["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_the_anthropic_response_content_is_recorded_in_full(trace_dir, anthropic_calls):
    client = anthropic_sdk.Anthropic(api_key="test")
    recorded = run_and_read(
        trace_dir, lambda: client.messages.create(model="claude-sonnet-5", messages=[])
    )
    block = recorded[1]["body"]["response"]["content"][0]
    assert block["type"] == "tool_use"
    assert block["input"] == {"city": "Mumbai"}


# -- observed causality -----------------------------------------------------


def test_a_tool_call_is_linked_to_the_model_output_that_asked_for_it(trace_dir, anthropic_calls):
    client = anthropic_sdk.Anthropic(api_key="test")

    def work():
        client.messages.create(
            model="claude-sonnet-5", messages=[], tools=[WEATHER_TOOL_ANTHROPIC]
        )
        with deposition.step("tool_call", tool="get_weather") as body:
            body["result"] = "18C"

    recorded = run_and_read(trace_dir, work)
    llm_call, tool_call = recorded[1], recorded[2]
    assert tool_call["type"] == "tool_call"
    assert tool_call["caused_by"] == [llm_call["seq"]]


def test_the_same_link_is_made_for_openai_tool_calls(trace_dir, openai_calls):
    client = openai_sdk.OpenAI(api_key="test")

    def work():
        client.chat.completions.create(model="gpt-4o-mini", messages=[])
        with deposition.step("tool_call", tool="get_weather"):
            pass

    recorded = run_and_read(trace_dir, work)
    assert recorded[2]["caused_by"] == [recorded[1]["seq"]]


def test_a_tool_the_model_never_asked_for_gets_no_causal_edge(trace_dir, anthropic_calls):
    # Nothing is inferred from adjacency: a causal graph nobody can trust is
    # worse than a sparse one.
    client = anthropic_sdk.Anthropic(api_key="test")

    def work():
        client.messages.create(model="claude-sonnet-5", messages=[])
        with deposition.step("tool_call", tool="send_email"):
            pass

    recorded = run_and_read(trace_dir, work)
    assert "caused_by" not in recorded[2]


def test_an_explicit_caused_by_always_wins(trace_dir, anthropic_calls):
    client = anthropic_sdk.Anthropic(api_key="test")

    def work():
        client.messages.create(model="claude-sonnet-5", messages=[])
        with deposition.step("tool_call", tool="get_weather", caused_by=[0]):
            pass

    recorded = run_and_read(trace_dir, work)
    assert recorded[2]["caused_by"] == [0]


def test_the_link_points_at_the_most_recent_request_for_that_tool(trace_dir, anthropic_calls):
    client = anthropic_sdk.Anthropic(api_key="test")

    def work():
        client.messages.create(model="claude-sonnet-5", messages=[])
        client.messages.create(model="claude-sonnet-5", messages=[])
        with deposition.step("tool_call", tool="get_weather"):
            pass

    recorded = run_and_read(trace_dir, work)
    assert recorded[3]["caused_by"] == [recorded[2]["seq"]]


# -- failure modes ----------------------------------------------------------


def test_a_provider_error_is_recorded_and_still_raised(trace_dir):
    from anthropic.resources.messages import Messages

    def explode(**kwargs):
        raise RuntimeError("rate limited")

    with _Provider(Messages, "create", explode):
        client = anthropic_sdk.Anthropic(api_key="test")
        deposition.init("test-project", directory=trace_dir)

        @deposition.record(name="agent")
        def agent():
            client.messages.create(model="claude-sonnet-5", messages=[])

        with pytest.raises(RuntimeError, match="rate limited"):
            agent()
        path = deposition._recorder._runs[-1].path
        deposition.shutdown()

    recorded = events(path)
    call = recorded[1]
    assert call["type"] == "llm_call"
    assert call["body"]["error"]["exception"] == "RuntimeError"
    assert call["body"]["gen_ai.request.model"] == "claude-sonnet-5"


def test_a_streamed_call_says_the_response_was_not_captured(trace_dir, anthropic_calls):
    # Better an honest gap than a silently empty response field.
    client = anthropic_sdk.Anthropic(api_key="test")
    recorded = run_and_read(
        trace_dir,
        lambda: client.messages.create(model="claude-sonnet-5", messages=[], stream=True),
    )
    assert recorded[1]["body"]["response"] == {"streamed": True, "captured": False}


def test_a_call_outside_a_run_is_not_recorded_and_does_not_fail(trace_dir, anthropic_calls):
    client = anthropic_sdk.Anthropic(api_key="test")
    deposition.init("test-project", directory=trace_dir)
    assert client.messages.create(model="claude-sonnet-5", messages=[]).id == "msg_1"


def test_an_unserialisable_argument_does_not_lose_the_event(trace_dir, anthropic_calls):
    class Opaque:
        def __repr__(self):
            return "<opaque>"

    client = anthropic_sdk.Anthropic(api_key="test")
    recorded = run_and_read(
        trace_dir,
        lambda: client.messages.create(
            model="claude-sonnet-5", messages=[{"role": "user", "content": Opaque()}]
        ),
    )
    assert recorded[1]["body"]["messages"] == [{"role": "user", "content": "<opaque>"}]


def test_an_instrumented_run_still_verifies(trace_dir, anthropic_calls):
    client = anthropic_sdk.Anthropic(api_key="test")
    deposition.init("test-project", directory=trace_dir)

    @deposition.record(name="agent")
    def agent():
        client.messages.create(model="claude-sonnet-5", messages=[], tools=[WEATHER_TOOL_ANTHROPIC])
        with deposition.step("tool_call", tool="get_weather"):
            pass

    agent()
    path = deposition._recorder._runs[-1].path
    deposition.shutdown()
    assert verify_file(path).ok


# -- async ------------------------------------------------------------------


def test_an_async_call_is_recorded(trace_dir):
    from anthropic.resources.messages import AsyncMessages

    async def stub(**kwargs):
        return anthropic_response()

    original = AsyncMessages.create

    async def recorded_create(_self, **kwargs):
        return await stub(**kwargs)

    AsyncMessages.create = recorded_create
    try:
        client = anthropic_sdk.AsyncAnthropic(api_key="test")
        deposition.init("test-project", directory=trace_dir)

        @deposition.record(name="async-agent")
        def agent():
            return asyncio.run(client.messages.create(model="claude-sonnet-5", messages=[]))

        result = agent()
        path = deposition._recorder._runs[-1].path
        deposition.shutdown()
    finally:
        instrument.uninstall()
        AsyncMessages.create = original

    assert isinstance(result, Message)
    assert events(path)[1]["body"]["provider"] == "anthropic"


# -- jsonable ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("x", "x"),
        (7, 7),
        (True, True),
        ({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}),
        ((1, 2), [1, 2]),
        ({1: "a"}, {"1": "a"}),
    ],
)
def test_jsonable_passes_json_types_through(value, expected):
    assert _common.jsonable(value) == expected


def test_jsonable_stringifies_non_finite_floats():
    # canonical JSON forbids NaN/Infinity, and one must not be able to silently
    # drop a whole event.
    assert _common.jsonable(float("nan")) == "nan"
    assert _common.jsonable(float("inf")) == "inf"
    assert _common.jsonable(1.5) == 1.5


def test_jsonable_unwraps_pydantic_models():
    assert _common.jsonable(anthropic_response())["id"] == "msg_1"


def test_jsonable_gives_up_gracefully_on_depth():
    deep = current = {}
    for _ in range(40):
        current["next"] = {}
        current = current["next"]
    assert isinstance(_common.jsonable(deep), dict)  # did not raise


def test_jsonable_survives_a_model_dump_that_raises():
    class Hostile:
        def model_dump(self):
            raise RuntimeError("no")

        def __repr__(self):
            return "<hostile>"

    assert _common.jsonable(Hostile()) == "<hostile>"
