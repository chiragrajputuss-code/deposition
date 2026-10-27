"""SDK recording tests.

Two promises are under test here:

1. A recorded run produces a trace that ``deposition verify`` accepts.
2. Deposition failing never fails the host agent - the worst case is a missing
   trace, never a broken application.
"""

from __future__ import annotations

import json
import threading

import pytest

import deposition
from deposition.chain import verify_file
from deposition.exporters.jsonl import JsonlExporter
from deposition.schema import EventType, canonical_bytes, is_blob_ref


def read_events(path) -> list[dict]:
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def record_run(trace_dir, fn=None, **init_kwargs):
    """Run ``fn`` under a recorded run and return its trace path."""
    deposition.init("test-project", directory=trace_dir, instrument=False, **init_kwargs)

    @deposition.record(name="test-agent")
    def wrapped():
        return fn() if fn else None

    wrapped()
    run = deposition._recorder._runs[-1]
    deposition.flush()
    return run.path


# -- a recorded run is a valid trace ----------------------------------------


def test_a_recorded_run_produces_a_verifiable_trace(trace_dir):
    def work():
        deposition.log(EventType.LLM_CALL, {"provider": "anthropic", "usage": {"total_tokens": 10}})
        with deposition.step("tool_call", tool="search"):
            pass

    path = record_run(trace_dir, work)
    deposition.shutdown()
    result = verify_file(path)
    assert result.ok, result.summary()


def test_the_trace_opens_with_run_start_and_closes_with_run_end(trace_dir):
    path = record_run(trace_dir, lambda: deposition.annotate("hello"))
    deposition.shutdown()
    events = read_events(path)
    assert events[0]["type"] == "run_start"
    assert events[-1]["type"] == "run_end"
    assert events[-1]["body"]["status"] == "ok"


def test_run_start_records_the_agent_project_and_environment(trace_dir):
    path = record_run(trace_dir, None)
    deposition.shutdown()
    body = read_events(path)[0]["body"]
    assert body["agent"] == "test-agent"
    assert body["project"] == "test-project"
    assert body["sdk_version"] == deposition.__version__
    assert body["env"]["python"]


def test_run_end_totals_count_steps_tokens_and_cost(trace_dir):
    def work():
        deposition.log(EventType.LLM_CALL, {"usage": {"total_tokens": 120}, "cost_usd": 0.004})
        deposition.log(EventType.LLM_CALL, {"usage": {"total_tokens": 80}, "cost_usd": 0.002})
        deposition.log(EventType.TOOL_CALL, {"tool": "search"})

    path = record_run(trace_dir, work)
    deposition.shutdown()
    totals = read_events(path)[-1]["body"]["totals"]
    assert totals["steps"] == 3  # run_start and run_end are not steps
    assert totals["tokens"] == 200
    assert totals["cost_usd"] == pytest.approx(0.006)
    assert totals["duration_ms"] >= 0


def test_the_trace_file_is_named_after_the_run(trace_dir):
    path = record_run(trace_dir, None)
    deposition.shutdown()
    run_id = read_events(path)[0]["run_id"]
    assert path.endswith(f"{run_id}.jsonl")


# -- steps and causality ----------------------------------------------------


def test_log_returns_the_seq_so_it_can_be_cited_as_a_cause(trace_dir):
    seqs = []

    def work():
        first = deposition.log(EventType.LLM_CALL, {"provider": "anthropic"})
        second = deposition.log(EventType.TOOL_CALL, {"tool": "search"}, caused_by=[first])
        seqs.extend([first, second])

    path = record_run(trace_dir, work)
    deposition.shutdown()
    assert seqs == [1, 2]
    assert read_events(path)[2]["caused_by"] == [1]


def test_step_records_on_exit_with_a_latency(trace_dir):
    def work():
        with deposition.step("decision", label="issue refund", caused_by=[0]) as body:
            body["detail"] = "customer is within the window"

    path = record_run(trace_dir, work)
    deposition.shutdown()
    decision = read_events(path)[1]
    assert decision["type"] == "decision"
    assert decision["body"]["label"] == "issue refund"
    assert decision["body"]["detail"] == "customer is within the window"
    assert decision["body"]["latency_ms"] >= 0
    assert decision["caused_by"] == [0]


def test_a_step_that_raises_is_recorded_and_the_exception_still_propagates(trace_dir):
    def work():
        with pytest.raises(ValueError, match="refund declined"):
            with deposition.step("tool_call", tool="refund"):
                raise ValueError("refund declined")

    path = record_run(trace_dir, work)
    deposition.shutdown()
    event = read_events(path)[1]
    assert event["body"]["error"] == {"exception": "ValueError", "message": "refund declined"}


def test_an_agent_that_raises_is_recorded_as_a_failed_run(trace_dir):
    deposition.init("test-project", directory=trace_dir, instrument=False)

    @deposition.record(name="doomed-agent")
    def doomed():
        raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        doomed()

    path = deposition._recorder._runs[-1].path
    deposition.shutdown()

    events = read_events(path)
    assert events[-2]["type"] == "error"
    assert events[-2]["body"]["exception"] == "RuntimeError"
    assert events[-1]["body"]["status"] == "error"
    assert verify_file(path).ok


# -- observed causality for hand-logged model turns (ADR 004) ----------------

RESPONSE_SHAPES = {
    "hand_logged": {"tool_calls": [{"name": "send_email"}]},
    "openai_chat": {
        "choices": [{"message": {"tool_calls": [{"function": {"name": "send_email"}}]}}]
    },
    "openai_tool_calls": {"tool_calls": [{"type": "function", "function": {"name": "send_email"}}]},
    "anthropic": {"content": [{"type": "tool_use", "name": "send_email", "input": {}}]},
    "openai_responses": {"output": [{"type": "function_call", "name": "send_email"}]},
}


def log_turn_then_tool(response, *, tool="send_email", caused_by=None):
    def work():
        turn = deposition.log("llm_call", {"response": response})
        with deposition.step("tool_call", tool=tool, caused_by=caused_by):
            pass
        return turn

    return work


@pytest.mark.parametrize("shape", sorted(RESPONSE_SHAPES))
def test_a_hand_logged_model_turn_that_names_a_tool_links_the_tool_call_that_follows(
    trace_dir, shape
):
    events = read_events(record_run(trace_dir, log_turn_then_tool(RESPONSE_SHAPES[shape])))
    turn = next(e for e in events if e["type"] == "llm_call")
    tool = next(e for e in events if e["type"] == "tool_call")
    assert tool["caused_by"] == [turn["seq"]]


def test_a_tool_the_hand_logged_turn_never_named_gets_no_edge(trace_dir):
    events = read_events(
        record_run(trace_dir, log_turn_then_tool(RESPONSE_SHAPES["anthropic"], tool="delete"))
    )
    tool = next(e for e in events if e["type"] == "tool_call")
    assert "caused_by" not in tool


def test_an_explicit_caused_by_still_wins_over_the_observed_edge(trace_dir):
    events = read_events(
        record_run(trace_dir, log_turn_then_tool(RESPONSE_SHAPES["anthropic"], caused_by=[0]))
    )
    tool = next(e for e in events if e["type"] == "tool_call")
    assert tool["caused_by"] == [0]


@pytest.mark.parametrize(
    "response",
    [
        "a plain string",
        {"tool_calls": "not a list"},
        {"content": [None, 3, {"type": "tool_use", "name": 7}]},
        {"choices": ["garbage"]},
    ],
)
def test_an_unreadable_response_names_no_tools_and_still_records(trace_dir, response):
    events = read_events(record_run(trace_dir, log_turn_then_tool(response)))
    assert [e["type"] for e in events].count("llm_call") == 1
    assert "caused_by" not in next(e for e in events if e["type"] == "tool_call")
    assert verify_file(record_run(trace_dir)).ok


def test_tool_names_survive_a_response_large_enough_to_become_a_blob(trace_dir):
    response = {"text": "x" * 70_000, "tool_calls": [{"name": "send_email"}]}
    events = read_events(record_run(trace_dir, log_turn_then_tool(response)))
    turn = next(e for e in events if e["type"] == "llm_call")
    tool = next(e for e in events if e["type"] == "tool_call")
    assert tool["caused_by"] == [turn["seq"]]


# -- dropped events: counted, never a gap in the chain (ADR 010) -------------


def drop_notes_marked(marker):
    """Make the exporter behave as it does on a full queue, for marked events."""
    run = deposition.current_run()
    original = run.exporter.export

    def full_queue(event):
        if event["body"].get("note") == marker:
            run.exporter.dropped += 1
            return False
        return original(event)

    run.exporter.export = full_queue


def test_a_dropped_event_leaves_the_chain_intact_and_is_counted(trace_dir):
    def work():
        drop_notes_marked("lost")
        deposition.annotate("kept")
        deposition.annotate("lost")
        deposition.annotate("kept too")

    path = record_run(trace_dir, work)
    deposition.shutdown()
    assert verify_file(path).ok
    events = read_events(path)
    assert [e["body"].get("note") for e in events if e["type"] == "annotation"] == [
        "kept",
        "kept too",
    ]
    totals = events[-1]["body"]["totals"]
    assert totals["dropped"] == 1
    assert totals["steps"] == 2  # only what is in the record, so completeness holds


def test_log_returns_none_for_a_dropped_event_so_no_cause_cites_a_reused_seq(trace_dir):
    seen = {}

    def work():
        drop_notes_marked("lost")
        seen["lost"] = deposition.log("annotation", {"note": "lost"})
        seen["next"] = deposition.log("annotation", {"note": "next"})

    record_run(trace_dir, work)
    assert seen["lost"] is None
    assert seen["next"] == 1


def test_verify_counts_a_dropped_event_and_audit_refuses_to_attest(trace_dir, capsys):
    from deposition.cli import main

    def work():
        drop_notes_marked("lost")
        deposition.annotate("lost")

    path = record_run(trace_dir, work, mandate={"max_steps": 100})
    deposition.shutdown()
    assert main(["verify", str(path)]) == 0
    out = capsys.readouterr().out
    assert "1 dropped event(s) - the record admits gaps" in out
    assert "OK completeness" in out
    assert main(["audit", str(path)]) == 1
    assert "NOT ATTESTABLE" in capsys.readouterr().out


def test_events_from_many_threads_in_one_run_are_written_in_seq_order(trace_dir):
    def work():
        def burst():
            for i in range(200):
                deposition.annotate(f"event {i}")

        threads = [threading.Thread(target=burst) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    path = record_run(trace_dir, work)
    deposition.shutdown()
    result = verify_file(path)
    assert result.ok, result.summary()


# -- recording must never break the host agent ------------------------------


def test_the_decorated_function_runs_when_deposition_was_never_initialised():
    assert deposition.current_run() is None

    @deposition.record()
    def work():
        return "done"

    assert work() == "done"


def test_log_outside_a_run_is_a_no_op():
    deposition.init("test-project", instrument=False)
    assert deposition.log(EventType.ANNOTATION, {"note": "nobody is listening"}) is None


def test_a_redact_hook_that_explodes_does_not_break_the_agent(trace_dir):
    def explode(body):
        raise RuntimeError("bad redactor")

    deposition.init("test-project", directory=trace_dir, instrument=False, redact=explode)

    @deposition.record(name="resilient-agent")
    def work():
        deposition.log(EventType.TOOL_CALL, {"tool": "search"})
        return "done"

    assert work() == "done"


def test_shutdown_is_idempotent(trace_dir):
    deposition.init("test-project", directory=trace_dir, instrument=False)
    deposition.shutdown()
    deposition.shutdown()
    deposition.flush()


def test_an_unfinished_run_is_closed_as_interrupted_on_shutdown(trace_dir):
    deposition.init("test-project", directory=trace_dir, instrument=False)
    run = deposition._recorder.start_run("abandoned-agent")
    deposition.shutdown()
    assert read_events(run.path)[-1]["body"]["status"] == "interrupted"


def test_hosted_mode_says_it_is_not_available_yet(trace_dir, capsys):
    # Recording locally under a token that implies upload would be a lie.
    deposition.init("test-project", directory=trace_dir, token="pf_live_x", instrument=False)
    assert "hosted mode is not available yet" in capsys.readouterr().err


# -- redaction --------------------------------------------------------------


def test_redaction_happens_before_anything_is_written(trace_dir):
    def strip_pii(body):
        return {k: ("[redacted]" if k == "email" else v) for k, v in body.items()}

    path = record_run(
        trace_dir,
        lambda: deposition.log(EventType.TOOL_CALL, {"tool": "lookup", "email": "a@b.com"}),
        redact=strip_pii,
    )
    deposition.shutdown()
    raw = open(path, encoding="utf-8").read()
    assert "a@b.com" not in raw
    assert read_events(path)[1]["body"]["email"] == "[redacted]"


# -- concurrency ------------------------------------------------------------


def test_runs_on_different_threads_are_recorded_separately(trace_dir):
    deposition.init("test-project", directory=trace_dir, instrument=False)

    @deposition.record(name="threaded-agent")
    def work():
        deposition.log(EventType.TOOL_CALL, {"tool": "search"})

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    paths = [run.path for run in deposition._recorder._runs]
    deposition.shutdown()
    assert len({*paths}) == 4
    for path in paths:
        assert verify_file(path).ok


# -- the local exporter -----------------------------------------------------


def test_large_strings_become_blob_references(tmp_path):
    exporter = JsonlExporter("run_blob", tmp_path)
    try:
        body = exporter.externalize_body({"prompt": "x" * 70_000, "tool": "search"})
        assert is_blob_ref(body["prompt"])
        assert body["tool"] == "search"
        blob = tmp_path / "blobs" / body["prompt"]["$blob"]
        assert blob.read_bytes() == b"x" * 70_000
        assert body["prompt"]["size"] == 70_000
    finally:
        exporter.close()


def test_identical_payloads_are_stored_once(tmp_path):
    exporter = JsonlExporter("run_blob", tmp_path)
    try:
        first = exporter.externalize_body({"prompt": "y" * 70_000})
        second = exporter.externalize_body({"prompt": "y" * 70_000})
        assert first["prompt"]["$blob"] == second["prompt"]["$blob"]
        assert len(list((tmp_path / "blobs").iterdir())) == 1
    finally:
        exporter.close()


def test_a_body_that_is_only_large_in_aggregate_is_externalised(tmp_path):
    exporter = JsonlExporter("run_blob", tmp_path)
    try:
        messages = [{"content": "z" * 1000} for _ in range(100)]
        body = exporter.externalize_body({"messages": messages})
        assert len(canonical_bytes(body)) <= exporter.blob_threshold
    finally:
        exporter.close()


def test_small_bodies_are_left_alone(tmp_path):
    exporter = JsonlExporter("run_small", tmp_path)
    try:
        body = {"tool": "search", "arguments": {"q": "refund policy"}}
        assert exporter.externalize_body(body) == body
        assert not (tmp_path / "blobs").exists()
    finally:
        exporter.close()


def test_blob_references_survive_the_hash_chain(trace_dir):
    # The hash must cover the reference that is actually stored on disk.
    path = record_run(
        trace_dir, lambda: deposition.log(EventType.LLM_CALL, {"prompt": "q" * 70_000})
    )
    deposition.shutdown()
    assert verify_file(path).ok
    assert is_blob_ref(read_events(path)[1]["body"]["prompt"])


def test_the_trace_is_written_as_canonical_json_one_event_per_line(trace_dir):
    path = record_run(trace_dir, lambda: deposition.annotate("hello"))
    deposition.shutdown()
    for line in open(path, encoding="utf-8"):
        assert line.endswith("\n")
        assert ", " not in line.split('"note"')[0]  # no pretty-printing separators
