"""Hash chain tests.

Postflight's claim is that a trace is evidence: if anyone edits, drops, inserts
or reorders an event, verification says so and names the first event that broke.
These tests are that claim, written down. A change to ``chain.py`` that is not
covered here has not been reviewed.
"""

from __future__ import annotations

import json

import pytest

from postflight.chain import (
    GENESIS_HASH,
    ChainBuilder,
    ChainResult,
    ChainVerificationError,
    event_hash,
    read_jsonl,
    seal,
    verify,
    verify_file,
)
from postflight.schema import Event, EventType, SchemaError, canonical_json

TS = "2026-09-27T08:14:03.412Z"


def build_trace(run_id: str = "run_9f3kQ2") -> list[dict]:
    """A small, ordinary, well-formed run."""
    builder = ChainBuilder(run_id)
    return [
        builder.append(EventType.RUN_START, {"agent": "triage-agent"}, ts=TS),
        builder.append(EventType.LLM_CALL, {"provider": "anthropic"}, ts=TS),
        builder.append(EventType.TOOL_CALL, {"tool": "search"}, caused_by=[1], ts=TS),
        builder.append(EventType.DECISION, {"label": "issue refund"}, caused_by=[1, 2], ts=TS),
        builder.append(EventType.RUN_END, {"status": "ok"}, ts=TS),
    ]


def write_trace(path, events) -> None:
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")


def codes(result: ChainResult) -> list[str]:
    return [e.code for e in result.errors]


# -- building ---------------------------------------------------------------


def test_a_freshly_built_trace_verifies():
    result = verify(build_trace())
    assert result.ok, result.summary()
    assert result.events_checked == 5
    assert result.run_id == "run_9f3kQ2"
    assert not result.errors


def test_run_start_is_seq_zero_anchored_to_the_genesis_hash():
    events = build_trace()
    assert events[0]["seq"] == 0
    assert events[0]["prev_hash"] == GENESIS_HASH


def test_event_one_carries_the_hash_of_the_run_start_header():
    # The spec's wording: "event 1's prev_hash is the hash of the run_start header".
    events = build_trace()
    assert events[1]["prev_hash"] == events[0]["hash"]


def test_each_event_links_to_its_predecessor():
    events = build_trace()
    # strict=False is the point: events[1:] is one shorter, which is what
    # pairs each event with its successor.
    for previous, current in zip(events, events[1:], strict=False):
        assert current["prev_hash"] == previous["hash"]
        assert current["seq"] == previous["seq"] + 1


def test_builder_tracks_the_head_hash_and_next_seq():
    builder = ChainBuilder("run_x")
    first = builder.append(EventType.RUN_START, {}, ts=TS)
    assert builder.head_hash == first["hash"]
    assert builder.next_seq == 1


def test_builder_can_resume_from_a_known_head():
    # Resuming is how a hosted-mode upload continues an already-started run.
    events = build_trace()
    resumed = ChainBuilder("run_9f3kQ2", prev_hash=events[-1]["hash"], next_seq=len(events))
    # A new event on the resumed builder keeps the chain intact up to run_end.
    extra = resumed.append(EventType.ANNOTATION, {"note": "reviewed"}, ts=TS)
    assert extra["prev_hash"] == events[-1]["hash"]
    assert extra["seq"] == 5


def test_hash_covers_the_event_but_not_the_hash_field_itself():
    event = build_trace()[2]
    assert event_hash(event) == event["hash"]
    assert event_hash({k: v for k, v in event.items() if k != "hash"}) == event["hash"]


def test_hash_is_deterministic_across_key_orderings():
    event = build_trace()[2]
    shuffled = dict(reversed(list(event.items())))
    assert event_hash(shuffled) == event["hash"]


def test_hashing_without_a_prev_hash_is_an_error():
    # An unlinked event has nothing to be evidence of.
    with pytest.raises(SchemaError, match="prev_hash"):
        event_hash({"v": "0.1", "run_id": "run_x", "seq": 0, "ts": TS, "type": "run_start"})


def test_seal_accepts_both_events_and_dicts():
    event = Event(run_id="run_x", seq=0, type=EventType.RUN_START, ts=TS)
    from_object = seal(event, GENESIS_HASH)
    from_dict = seal(event.to_dict(), GENESIS_HASH)
    assert from_object == from_dict


def test_seal_replaces_any_hash_already_present():
    # Otherwise a forged hash could survive re-sealing.
    event = dict(build_trace()[1], hash="f" * 64)
    resealed = seal(event, event["prev_hash"])
    assert resealed["hash"] != "f" * 64
    assert resealed["hash"] == build_trace()[1]["hash"]


def test_seal_rejects_an_invalid_event():
    with pytest.raises(SchemaError):
        seal({"v": "0.1", "run_id": "run_x", "seq": 0, "ts": TS, "type": "nope"}, GENESIS_HASH)


def test_two_runs_with_identical_content_produce_different_chains():
    # run_id is inside the hash input, so traces cannot be swapped between runs.
    assert build_trace("run_a")[0]["hash"] != build_trace("run_b")[0]["hash"]


# -- tamper detection -------------------------------------------------------


def test_editing_an_event_body_is_detected():
    events = build_trace()
    events[2]["body"]["tool"] = "delete_everything"
    result = verify(events)
    assert not result.ok
    assert codes(result) == ["hash_mismatch"]
    assert result.errors[0].seq == 2
    assert "edited after it was written" in result.errors[0].message


def test_editing_a_timestamp_is_detected():
    events = build_trace()
    events[3]["ts"] = "2026-09-27T09:00:00.000Z"
    assert codes(verify(events)) == ["hash_mismatch"]


def test_editing_causality_is_detected():
    # Rewriting *why* something happened is exactly the forgery this guards.
    events = build_trace()
    events[3]["caused_by"] = [1]
    assert codes(verify(events)) == ["hash_mismatch"]


def test_deleting_an_event_is_detected():
    events = build_trace()
    del events[2]
    result = verify(events)
    assert not result.ok
    assert codes(result) == ["seq_break"]
    assert "expected seq 2, found 3" in result.errors[0].message


def test_reordering_two_events_is_detected():
    events = build_trace()
    events[1], events[2] = events[2], events[1]
    assert codes(verify(events)) == ["seq_break"]


def test_duplicating_an_event_is_detected():
    events = build_trace()
    events.insert(2, events[1])
    assert codes(verify(events)) == ["seq_break"]


def test_inserting_a_forged_event_is_detected():
    # An attacker who re-seals their insert still cannot match the next event's
    # prev_hash without re-sealing the entire remainder of the chain.
    events = build_trace()
    forged = seal(
        Event(run_id="run_9f3kQ2", seq=3, type=EventType.TOOL_CALL, body={"tool": "refund"}, ts=TS),
        events[2]["hash"],
    )
    events.insert(3, forged)
    for index, event in enumerate(events[4:], start=4):
        event["seq"] = index
    result = verify(events)
    assert not result.ok
    assert "prev_hash_mismatch" in codes(result)


def test_truncating_the_tail_is_not_detectable_without_run_end():
    # Honest limitation: a hash chain proves nothing was changed, not that
    # nothing was removed from the end. run_end is what closes that gap, so a
    # truncated trace verifies but is visibly unfinished.
    events = build_trace()[:3]
    result = verify(events)
    assert result.ok
    assert events[-1]["type"] != EventType.RUN_END.value


def test_events_after_run_end_are_rejected():
    builder = ChainBuilder("run_x")
    events = [
        builder.append(EventType.RUN_START, {}, ts=TS),
        builder.append(EventType.RUN_END, {"status": "ok"}, ts=TS),
        builder.append(EventType.ANNOTATION, {"note": "sneaked in"}, ts=TS),
    ]
    assert codes(verify(events)) == ["events_after_run_end"]


# -- structural failures ----------------------------------------------------


def test_a_trace_not_starting_with_run_start_is_rejected():
    builder = ChainBuilder("run_x")
    events = [builder.append(EventType.TOOL_CALL, {"tool": "search"}, ts=TS)]
    result = verify(events)
    assert not result.ok
    assert "bad_header" in codes(result)


def test_events_from_another_run_are_rejected():
    events = build_trace("run_a")
    other = build_trace("run_b")
    events[2] = other[2]
    result = verify(events)
    assert not result.ok
    assert "run_id_mismatch" in codes(result)


def test_a_wrong_genesis_hash_is_rejected():
    forged = seal(Event(run_id="run_x", seq=0, type=EventType.RUN_START, ts=TS), "1" * 64)
    assert codes(verify([forged])) == ["prev_hash_mismatch"]


def test_an_invalid_event_is_reported_not_raised():
    events = build_trace()
    events[2]["type"] = "teleport"
    result = verify(events)
    assert not result.ok
    assert codes(result) == ["invalid_event"]


def test_an_empty_trace_is_not_ok():
    result = verify([])
    assert not result.ok
    assert codes(result) == ["empty_trace"]


def test_verification_never_raises_on_garbage():
    for garbage in ([None], ["nonsense"], [{"v": "0.1"}], [42]):
        assert not verify(garbage).ok


def test_verify_reports_the_first_break_and_stops():
    # One clear "the trace stops being trustworthy here" beats a wall of noise.
    events = build_trace()
    events[1]["body"]["provider"] = "openai"
    events[3]["body"]["label"] = "deny refund"
    result = verify(events)
    assert len(result.errors) == 1
    assert result.errors[0].seq == 1


# -- results ----------------------------------------------------------------


def test_head_hash_is_the_last_verified_event():
    events = build_trace()
    assert verify(events).head_hash == events[-1]["hash"]


def test_raise_if_broken_is_silent_on_a_good_trace():
    verify(build_trace()).raise_if_broken()


def test_raise_if_broken_raises_and_carries_the_result():
    events = build_trace()
    events[1]["body"] = {}
    result = verify(events)
    with pytest.raises(ChainVerificationError) as excinfo:
        result.raise_if_broken()
    assert excinfo.value.result is result
    assert "BROKEN" in str(excinfo.value)


def test_summaries_read_like_a_verdict():
    assert verify(build_trace()).summary().startswith("OK  5 events verified for run_9f3kQ2")
    broken = build_trace()
    broken[1]["ts"] = TS.replace("08", "09")
    assert verify(broken).summary().startswith("BROKEN")


def test_error_string_names_the_line_and_seq():
    events = build_trace()
    events[2]["body"]["tool"] = "x"
    error = verify(events).errors[0]
    assert str(error) == f"line 3 (seq 2): hash_mismatch - {error.message}"


# -- files ------------------------------------------------------------------


def test_verify_file_accepts_a_written_trace(tmp_path):
    path = tmp_path / "run.jsonl"
    write_trace(path, build_trace())
    result = verify_file(path)
    assert result.ok, result.summary()
    assert result.events_checked == 5


def test_verify_file_detects_an_edited_line(tmp_path):
    path = tmp_path / "run.jsonl"
    events = build_trace()
    write_trace(path, events)
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[2])
    tampered["body"]["tool"] = "delete_everything"
    lines[2] = canonical_json(tampered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_file(path)
    assert not result.ok
    assert result.errors[0].code == "hash_mismatch"
    assert result.errors[0].line == 3  # 1-indexed, so it points at the real line


def test_blank_lines_do_not_shift_the_reported_line_numbers(tmp_path):
    path = tmp_path / "run.jsonl"
    events = build_trace()
    events[3]["body"]["label"] = "tampered"
    body = "".join(canonical_json(e) + "\n" for e in events)
    path.write_text("\n" + body, encoding="utf-8")  # leading blank line
    assert verify_file(path).errors[0].line == 5


def test_verify_file_reports_a_corrupt_line_rather_than_crashing(tmp_path):
    path = tmp_path / "run.jsonl"
    write_trace(path, build_trace())
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    result = verify_file(path)
    assert not result.ok
    assert codes(result) == ["unparseable"]


def test_verify_file_on_an_empty_file(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text("", encoding="utf-8")
    assert codes(verify_file(path)) == ["empty_trace"]


def test_read_jsonl_skips_blank_lines_and_keeps_line_numbers(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text('{"a":1}\n\n{"b":2}\n', encoding="utf-8")
    assert list(read_jsonl(path)) == [(1, {"a": 1}), (3, {"b": 2})]
