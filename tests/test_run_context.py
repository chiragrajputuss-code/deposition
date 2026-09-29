"""Which run an event belongs to, across nesting, threads and asyncio.

Every bug these cover produced the same symptom: a trace that was missing events
and still sealed, still verified and still reported ``status: ok``. A recorder
whose gaps are invisible is worse than one that fails loudly, because the chain
then certifies a record that was never complete.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import deposition
from deposition.chain import verify_file
from deposition.schema import EventType


def events(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def labels(path):
    return [e["body"].get("label") for e in events(path) if e["type"] == EventType.DECISION.value]


def totals(path):
    return next(e["body"]["totals"] for e in events(path) if e["type"] == EventType.RUN_END.value)


def only_trace(directory):
    traces = list(directory.glob("run_*.jsonl"))
    assert len(traces) == 1, f"expected one trace, found {[t.name for t in traces]}"
    return traces[0]


# -- nesting ----------------------------------------------------------------


def test_a_run_keeps_recording_after_a_nested_run_finishes(trace_dir):
    deposition.init("nested", directory=trace_dir, instrument=False)

    @deposition.record(name="inner")
    def inner():
        deposition.log(EventType.DECISION, {"label": "inner step"})

    @deposition.record(name="outer")
    def outer():
        deposition.log(EventType.DECISION, {"label": "before"})
        inner()
        deposition.log(EventType.DECISION, {"label": "after"})

    outer()
    deposition.shutdown()

    traces = sorted(trace_dir.glob("run_*.jsonl"), key=lambda p: len(events(p)))
    assert labels(traces[0]) == ["inner step"]
    # "after" is the event the single-slot thread-local used to lose entirely.
    assert labels(traces[1]) == ["before", "after"]


def test_nested_runs_do_not_borrow_each_others_events(trace_dir):
    deposition.init("nested", directory=trace_dir, instrument=False)

    @deposition.record(name="inner")
    def inner():
        deposition.log(EventType.DECISION, {"label": "inner only"})

    @deposition.record(name="outer")
    def outer():
        inner()
        deposition.log(EventType.DECISION, {"label": "outer only"})

    outer()
    deposition.shutdown()

    found = {tuple(labels(t)) for t in trace_dir.glob("run_*.jsonl")}
    assert found == {("inner only",), ("outer only",)}


# -- threads and asyncio ----------------------------------------------------


def test_an_event_from_a_worker_thread_is_recorded(trace_dir):
    deposition.init("threads", directory=trace_dir, instrument=False)

    @deposition.record(name="agent")
    def run():
        deposition.log(EventType.DECISION, {"label": "main"})
        worker = threading.Thread(
            target=lambda: deposition.log(EventType.DECISION, {"label": "worker"})
        )
        worker.start()
        worker.join()

    run()
    deposition.shutdown()
    assert labels(only_trace(trace_dir)) == ["main", "worker"]


def test_events_from_a_thread_pool_are_recorded(trace_dir):
    deposition.init("pool", directory=trace_dir, instrument=False)

    @deposition.record(name="agent")
    def run():
        def job(i):
            deposition.log(EventType.DECISION, {"label": f"j{i}"})

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(job, range(3)))

    run()
    deposition.shutdown()
    assert sorted(labels(only_trace(trace_dir))) == ["j0", "j1", "j2"]


def test_an_asyncio_task_inherits_the_run_from_its_context(trace_dir):
    deposition.init("asyncio", directory=trace_dir, instrument=False)

    @deposition.record(name="agent")
    def run():
        async def work():
            await asyncio.gather(
                *(
                    asyncio.to_thread(deposition.log, EventType.DECISION, {"label": "t"})
                    for _ in range(2)
                )
            )
            deposition.log(EventType.DECISION, {"label": "loop"})

        asyncio.run(work())

    run()
    deposition.shutdown()
    assert sorted(labels(only_trace(trace_dir))) == ["loop", "t", "t"]


# -- the honesty of the fallback --------------------------------------------


def test_an_off_context_event_is_counted_as_adopted(trace_dir):
    # Attribution by elimination is a guess, and the trace has to admit it.
    deposition.init("adopted", directory=trace_dir, instrument=False)

    @deposition.record(name="agent")
    def run():
        deposition.log(EventType.DECISION, {"label": "in context"})
        worker = threading.Thread(
            target=lambda: deposition.log(EventType.DECISION, {"label": "off context"})
        )
        worker.start()
        worker.join()

    run()
    deposition.shutdown()
    assert totals(only_trace(trace_dir))["adopted"] == 1


def test_an_event_is_dropped_rather_than_guessed_when_two_runs_are_open(trace_dir):
    """With two runs in flight there is no honest answer, so nothing is recorded.

    A misattributed event in an evidentiary record is worse than a missing one:
    nothing downstream can tell that it was a guess.
    """
    deposition.init("ambiguous", directory=trace_dir, instrument=False)

    started = threading.Event()
    release = threading.Event()

    @deposition.record(name="first")
    def first():
        started.set()
        release.wait(5)

    holder = threading.Thread(target=first)
    holder.start()
    started.wait(5)

    @deposition.record(name="second")
    def second():
        # A second run is now open, so this off-context event cannot be placed.
        stray = threading.Thread(
            target=lambda: deposition.log(EventType.DECISION, {"label": "stray"})
        )
        stray.start()
        stray.join()
        deposition.log(EventType.DECISION, {"label": "in context"})

    second()
    release.set()
    holder.join(5)
    deposition.shutdown()

    recorded = [label for t in trace_dir.glob("run_*.jsonl") for label in labels(t)]
    assert "stray" not in recorded
    assert "in context" in recorded


# -- the record says what it lost -------------------------------------------


def test_run_end_reports_dropped_and_adopted_counts(trace_dir):
    deposition.init("counts", directory=trace_dir, instrument=False)

    @deposition.record(name="agent")
    def run():
        deposition.log(EventType.DECISION, {"label": "one"})

    run()
    deposition.shutdown()
    recorded = totals(only_trace(trace_dir))
    assert recorded["dropped"] == 0
    assert recorded["adopted"] == 0


def test_a_trace_with_adopted_events_still_verifies(trace_dir):
    deposition.init("verifies", directory=trace_dir, instrument=False)

    @deposition.record(name="agent")
    def run():
        worker = threading.Thread(target=lambda: deposition.log(EventType.DECISION, {"label": "w"}))
        worker.start()
        worker.join()

    run()
    deposition.shutdown()
    assert verify_file(only_trace(trace_dir)).ok
