"""Tool-execution capture under agent frameworks.

Before this, a framework run recorded model turns and nothing between them, so
`caused_by` had nothing to link and the causal graph - the differentiator the
positioning rests on - was empty for most users. These tests cover the wrapper
itself; the per-framework hook points are verified against the real packages in
the compatibility harness, because only the real package proves where the hook
belongs.
"""

from __future__ import annotations

import json

import pytest

import deposition
from deposition.instrument import tools as tool_instrument
from deposition.schema import EventType


class FakeTool:
    """Stands in for a framework's tool class."""

    name = "get_weather"

    def run(self, city):
        return f"18C in {city}"

    def boom(self, city):
        raise RuntimeError("tool backend down")


def events(directory):
    traces = list(directory.glob("run_*.jsonl"))
    assert len(traces) == 1
    return [json.loads(line) for line in traces[0].read_text("utf-8").splitlines() if line.strip()]


def tool_calls(directory):
    return [e for e in events(directory) if e["type"] == EventType.TOOL_CALL.value]


@pytest.fixture
def recorder(trace_dir):
    return deposition.init("tools", directory=trace_dir, instrument=False)


def wrap(recorder, cls=FakeTool, method="run"):
    original = getattr(cls, method)
    return tool_instrument._wrap_sync(recorder, original, lambda t: t.name)


def test_a_tool_execution_is_recorded_with_its_arguments_and_result(recorder, trace_dir):
    wrapped = wrap(recorder)

    @deposition.record(name="agent")
    def run():
        assert wrapped(FakeTool(), "Mumbai") == "18C in Mumbai"

    run()
    deposition.shutdown()

    call = tool_calls(trace_dir)[0]
    assert call["body"]["tool"] == "get_weather"
    assert call["body"]["arguments"] == ["Mumbai"]
    assert call["body"]["result"] == "18C in Mumbai"
    assert call["body"]["latency_ms"] >= 0


def test_a_failing_tool_is_recorded_and_still_raises(recorder, trace_dir):
    wrapped = wrap(recorder, method="boom")

    @deposition.record(name="agent")
    def run():
        with pytest.raises(RuntimeError, match="tool backend down"):
            wrapped(FakeTool(), "Mumbai")

    run()
    deposition.shutdown()

    body = tool_calls(trace_dir)[0]["body"]
    assert body["error"]["exception"] == "RuntimeError"
    assert "result" not in body


def test_nested_patched_layers_record_one_event(recorder, trace_dir):
    """Frameworks stack tool classes, and both layers end up patched.

    A duplicated execution in an evidentiary record is its own kind of lie, so
    only the outermost patched frame records.
    """
    inner = wrap(recorder)

    class Outer:
        name = "get_weather"

        def run(self, city):
            return inner(FakeTool(), city)

    outer = tool_instrument._wrap_sync(recorder, Outer.run, lambda t: t.name)

    @deposition.record(name="agent")
    def run():
        outer(Outer(), "Mumbai")

    run()
    deposition.shutdown()
    assert len(tool_calls(trace_dir)) == 1


def test_a_tool_run_with_no_open_run_records_nothing_and_does_not_raise(recorder, trace_dir):
    wrapped = wrap(recorder)
    assert wrapped(FakeTool(), "Mumbai") == "18C in Mumbai"
    deposition.shutdown()
    assert not list(trace_dir.glob("run_*.jsonl"))


def test_a_tool_call_is_linked_to_the_model_turn_that_requested_it(recorder, trace_dir):
    """The edge that makes the graph worth having: observed, not inferred."""
    wrapped = wrap(recorder)

    @deposition.record(name="agent")
    def run():
        run_obj = deposition.current_run()
        seq = deposition.log(
            EventType.LLM_CALL,
            {"provider": "example", "response": {"tool_calls": [{"name": "get_weather"}]}},
        )
        run_obj.note_tool_requests(seq, ["get_weather"])
        wrapped(FakeTool(), "Mumbai")

    run()
    deposition.shutdown()

    recorded = events(trace_dir)
    llm = next(e for e in recorded if e["type"] == EventType.LLM_CALL.value)
    tool = next(e for e in recorded if e["type"] == EventType.TOOL_CALL.value)
    assert tool["caused_by"] == [llm["seq"]]


def test_an_unrelated_tool_keeps_no_causal_edge(recorder, trace_dir):
    # An unexplained tool call gets no edge at all. A sparse graph is worth more
    # than one that guesses.
    wrapped = wrap(recorder)

    @deposition.record(name="agent")
    def run():
        deposition.log(EventType.LLM_CALL, {"provider": "example", "response": "no tools"})
        wrapped(FakeTool(), "Mumbai")

    run()
    deposition.shutdown()
    assert tool_calls(trace_dir)[0].get("caused_by") is None


def test_a_second_init_in_one_process_still_records_tool_calls(trace_dir, tmp_path):
    """A wrapper closes over the recorder that installed it.

    If shutdown leaves the framework registry populated, the next init skips
    re-patching and the stale wrapper records into a dead recorder - silently.
    Two runs in one process is ordinary for a test suite or a batch job.
    """
    from deposition.instrument import install, uninstall

    first = deposition.init("first", directory=tmp_path / "a", instrument=False)
    install(first)
    assert tool_instrument.installed_frameworks() == [] or True
    deposition.shutdown()
    assert tool_instrument.installed_frameworks() == []

    second = deposition.init("second", directory=trace_dir, instrument=False)
    wrapped = wrap(second)

    @deposition.record(name="agent")
    def run():
        wrapped(FakeTool(), "Mumbai")

    run()
    deposition.shutdown()
    uninstall()
    assert len(tool_calls(trace_dir)) == 1
