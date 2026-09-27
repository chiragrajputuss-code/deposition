"""SDK recording tests.

Two promises are under test here:

1. A recorded run produces a trace that ``postflight verify`` accepts.
2. Postflight failing never fails the host agent - the worst case is a missing
   trace, never a broken application.
"""

from __future__ import annotations

import json
import threading

import pytest

import postflight
from postflight.chain import verify_file
from postflight.exporters.jsonl import JsonlExporter
from postflight.schema import EventType, canonical_bytes, is_blob_ref


def read_events(path) -> list[dict]:
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def record_run(trace_dir, fn=None, **init_kwargs):
    """Run ``fn`` under a recorded run and return its trace path."""
    postflight.init("test-project", directory=trace_dir, instrument=False, **init_kwargs)

    @postflight.record(name="test-agent")
    def wrapped():
        return fn() if fn else None

    wrapped()
    run = postflight._recorder._runs[-1]
    postflight.flush()
    return run.path


# -- a recorded run is a valid trace ----------------------------------------


def test_a_recorded_run_produces_a_verifiable_trace(trace_dir):
    def work():
        postflight.log(EventType.LLM_CALL, {"provider": "anthropic", "usage": {"total_tokens": 10}})
        with postflight.step("tool_call", tool="search"):
            pass

    path = record_run(trace_dir, work)
    postflight.shutdown()
    result = verify_file(path)
    assert result.ok, result.summary()


def test_the_trace_opens_with_run_start_and_closes_with_run_end(trace_dir):
    path = record_run(trace_dir, lambda: postflight.annotate("hello"))
    postflight.shutdown()
    events = read_events(path)
    assert events[0]["type"] == "run_start"
    assert events[-1]["type"] == "run_end"
    assert events[-1]["body"]["status"] == "ok"


def test_run_start_records_the_agent_project_and_environment(trace_dir):
    path = record_run(trace_dir, None)
    postflight.shutdown()
    body = read_events(path)[0]["body"]
    assert body["agent"] == "test-agent"
    assert body["project"] == "test-project"
    assert body["sdk_version"] == postflight.__version__
    assert body["env"]["python"]


def test_run_end_totals_count_steps_tokens_and_cost(trace_dir):
    def work():
        postflight.log(EventType.LLM_CALL, {"usage": {"total_tokens": 120}, "cost_usd": 0.004})
        postflight.log(EventType.LLM_CALL, {"usage": {"total_tokens": 80}, "cost_usd": 0.002})
        postflight.log(EventType.TOOL_CALL, {"tool": "search"})

    path = record_run(trace_dir, work)
    postflight.shutdown()
    totals = read_events(path)[-1]["body"]["totals"]
    assert totals["steps"] == 3  # run_start and run_end are not steps
    assert totals["tokens"] == 200
    assert totals["cost_usd"] == pytest.approx(0.006)
    assert totals["duration_ms"] >= 0


def test_the_trace_file_is_named_after_the_run(trace_dir):
    path = record_run(trace_dir, None)
    postflight.shutdown()
    run_id = read_events(path)[0]["run_id"]
    assert path.endswith(f"{run_id}.jsonl")


# -- steps and causality ----------------------------------------------------


def test_log_returns_the_seq_so_it_can_be_cited_as_a_cause(trace_dir):
    seqs = []

    def work():
        first = postflight.log(EventType.LLM_CALL, {"provider": "anthropic"})
        second = postflight.log(EventType.TOOL_CALL, {"tool": "search"}, caused_by=[first])
        seqs.extend([first, second])

    path = record_run(trace_dir, work)
    postflight.shutdown()
    assert seqs == [1, 2]
    assert read_events(path)[2]["caused_by"] == [1]


def test_step_records_on_exit_with_a_latency(trace_dir):
    def work():
        with postflight.step("decision", label="issue refund", caused_by=[0]) as body:
            body["detail"] = "customer is within the window"

    path = record_run(trace_dir, work)
    postflight.shutdown()
    decision = read_events(path)[1]
    assert decision["type"] == "decision"
    assert decision["body"]["label"] == "issue refund"
    assert decision["body"]["detail"] == "customer is within the window"
    assert decision["body"]["latency_ms"] >= 0
    assert decision["caused_by"] == [0]


def test_a_step_that_raises_is_recorded_and_the_exception_still_propagates(trace_dir):
    def work():
        with pytest.raises(ValueError, match="refund declined"):
            with postflight.step("tool_call", tool="refund"):
                raise ValueError("refund declined")

    path = record_run(trace_dir, work)
    postflight.shutdown()
    event = read_events(path)[1]
    assert event["body"]["error"] == {"exception": "ValueError", "message": "refund declined"}


def test_an_agent_that_raises_is_recorded_as_a_failed_run(trace_dir):
    postflight.init("test-project", directory=trace_dir, instrument=False)

    @postflight.record(name="doomed-agent")
    def doomed():
        raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        doomed()

    path = postflight._recorder._runs[-1].path
    postflight.shutdown()

    events = read_events(path)
    assert events[-2]["type"] == "error"
    assert events[-2]["body"]["exception"] == "RuntimeError"
    assert events[-1]["body"]["status"] == "error"
    assert verify_file(path).ok


# -- recording must never break the host agent ------------------------------


def test_the_decorated_function_runs_when_postflight_was_never_initialised():
    assert postflight.current_run() is None

    @postflight.record()
    def work():
        return "done"

    assert work() == "done"


def test_log_outside_a_run_is_a_no_op():
    postflight.init("test-project", instrument=False)
    assert postflight.log(EventType.ANNOTATION, {"note": "nobody is listening"}) is None


def test_a_redact_hook_that_explodes_does_not_break_the_agent(trace_dir):
    def explode(body):
        raise RuntimeError("bad redactor")

    postflight.init("test-project", directory=trace_dir, instrument=False, redact=explode)

    @postflight.record(name="resilient-agent")
    def work():
        postflight.log(EventType.TOOL_CALL, {"tool": "search"})
        return "done"

    assert work() == "done"


def test_shutdown_is_idempotent(trace_dir):
    postflight.init("test-project", directory=trace_dir, instrument=False)
    postflight.shutdown()
    postflight.shutdown()
    postflight.flush()


def test_an_unfinished_run_is_closed_as_interrupted_on_shutdown(trace_dir):
    postflight.init("test-project", directory=trace_dir, instrument=False)
    run = postflight._recorder.start_run("abandoned-agent")
    postflight.shutdown()
    assert read_events(run.path)[-1]["body"]["status"] == "interrupted"


def test_hosted_mode_says_it_is_not_available_yet(trace_dir, capsys):
    # Recording locally under a token that implies upload would be a lie.
    postflight.init("test-project", directory=trace_dir, token="pf_live_x", instrument=False)
    assert "hosted mode is not available yet" in capsys.readouterr().err


# -- redaction --------------------------------------------------------------


def test_redaction_happens_before_anything_is_written(trace_dir):
    def strip_pii(body):
        return {k: ("[redacted]" if k == "email" else v) for k, v in body.items()}

    path = record_run(
        trace_dir,
        lambda: postflight.log(EventType.TOOL_CALL, {"tool": "lookup", "email": "a@b.com"}),
        redact=strip_pii,
    )
    postflight.shutdown()
    raw = open(path, encoding="utf-8").read()
    assert "a@b.com" not in raw
    assert read_events(path)[1]["body"]["email"] == "[redacted]"


# -- concurrency ------------------------------------------------------------


def test_runs_on_different_threads_are_recorded_separately(trace_dir):
    postflight.init("test-project", directory=trace_dir, instrument=False)

    @postflight.record(name="threaded-agent")
    def work():
        postflight.log(EventType.TOOL_CALL, {"tool": "search"})

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    paths = [run.path for run in postflight._recorder._runs]
    postflight.shutdown()
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
        trace_dir, lambda: postflight.log(EventType.LLM_CALL, {"prompt": "q" * 70_000})
    )
    postflight.shutdown()
    assert verify_file(path).ok
    assert is_blob_ref(read_events(path)[1]["body"]["prompt"])


def test_the_trace_is_written_as_canonical_json_one_event_per_line(trace_dir):
    path = record_run(trace_dir, lambda: postflight.annotate("hello"))
    postflight.shutdown()
    for line in open(path, encoding="utf-8"):
        assert line.endswith("\n")
        assert ", " not in line.split('"note"')[0]  # no pretty-printing separators
