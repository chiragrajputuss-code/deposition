"""Importing a Claude Code session transcript (`deposition import claude-code`).

Claude Code already writes down every model turn and every tool call, as plain
mutable JSON. These tests are the claim that `convert` rebuilds that into a
trace with the same guarantees as a live recording - a real hash chain, real
causal edges from tool call back to the model turn that asked for it, and the
same token-accounting discipline as the rest of this SDK - and that discovery
finds the right session without being told where to look, without ever
trusting an interface Claude Code has not promised to keep still.
"""

from __future__ import annotations

import json

import pytest

from deposition.chain import verify_file
from deposition.cli import EXIT_OK, EXIT_USAGE, main
from deposition.importers import claude_code
from deposition.importers.claude_code import (
    TranscriptError,
    convert,
    discover_latest_session,
    guess_project_dir,
    tail_once,
)

TS = "2026-10-16T09:00:00.000Z"


# -- building a realistic transcript, the way Claude Code actually writes one -


def assistant_turn(*, model="claude-opus-5", text="", tool_uses=(), usage=None, ts=TS):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for call_id, name, arguments in tool_uses:
        content.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": model,
            "content": content,
            "usage": usage or {"input_tokens": 100, "output_tokens": 20},
            "stop_reason": "tool_use" if tool_uses else "end_turn",
        },
    }


def tool_result(call_id, text, *, is_error=False, ts=TS):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": text,
                    "is_error": is_error,
                }
            ]
        },
    }


def user_prompt(text, *, ts=TS):
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


def header(**overrides):
    base = {
        "sessionId": "sess12345",
        "cwd": "/Users/dev/project",
        "entrypoint": "cli",
        "gitBranch": "main",
        "version": "2.1.0",
    }
    base.update(overrides)
    return base


def write_transcript(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def a_simple_session():
    """One prompt, one tool call, one answer - the shape most tests build on."""
    prompt = user_prompt("fix the failing test")
    prompt.update(header())
    plan = assistant_turn(
        text="",
        tool_uses=[("call_1", "Bash", {"command": "pytest -q"})],
        usage={"input_tokens": 200, "output_tokens": 15},
    )
    result = tool_result("call_1", "3 failed, 12 passed", is_error=False)
    answer = assistant_turn(text="Fixed it.", usage={"input_tokens": 260, "output_tokens": 8})
    return [prompt, plan, result, answer]


# -- convert: the shape of the trace -----------------------------------------


def test_converts_to_run_start_llm_call_tool_call_llm_call_run_end(tmp_path):
    transcript = write_transcript(tmp_path / "s.jsonl", a_simple_session())
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    assert [e["type"] for e in events] == [
        "run_start",
        "annotation",
        "llm_call",
        "tool_call",
        "llm_call",
        "run_end",
    ]


def test_the_converted_trace_verifies(tmp_path):
    transcript = write_transcript(tmp_path / "s.jsonl", a_simple_session())
    target = convert(transcript, tmp_path / "out")
    assert verify_file(target).ok


def test_run_start_carries_the_sessions_own_identity(tmp_path):
    transcript = write_transcript(tmp_path / "s.jsonl", a_simple_session())
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    start = events[0]
    assert start["body"]["agent"] == "claude-code"
    assert start["body"]["config"]["cwd"] == "/Users/dev/project"
    assert start["body"]["session_id"] == "sess12345"
    assert start["body"]["imported_from"] == str(transcript)


def test_a_human_prompt_becomes_an_annotation(tmp_path):
    transcript = write_transcript(tmp_path / "s.jsonl", a_simple_session())
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    note = next(e for e in events if e["type"] == "annotation")
    assert note["body"]["note"] == "fix the failing test"
    assert note["body"]["author"] == "user"


# -- causality ----------------------------------------------------------------


def test_a_tool_call_is_linked_to_the_model_turn_that_requested_it(tmp_path):
    transcript = write_transcript(tmp_path / "s.jsonl", a_simple_session())
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    plan = next(e for e in events if e["type"] == "llm_call" and e["seq"] == 2)
    tool = next(e for e in events if e["type"] == "tool_call")
    assert tool["caused_by"] == [plan["seq"]]


def test_two_tool_calls_in_one_turn_both_link_to_that_turn(tmp_path):
    turn = assistant_turn(
        tool_uses=[
            ("a", "Bash", {"command": "ls"}),
            ("b", "Bash", {"command": "pwd"}),
        ]
    )
    records = [
        user_prompt("do two things", ts=TS) | header(),
        turn,
        tool_result("a", "files..."),
        tool_result("b", "/repo"),
    ]
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    turn_seq = next(e for e in events if e["type"] == "llm_call")["seq"]
    tools = [e for e in events if e["type"] == "tool_call"]
    assert len(tools) == 2
    assert all(t["caused_by"] == [turn_seq] for t in tools)


# -- mutation flagging ---------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(claude_code.MUTATING))
def test_each_mutating_tool_is_flagged(tmp_path, tool):
    records = a_simple_session()
    records[1] = assistant_turn(tool_uses=[("call_1", tool, {})])
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["body"]["mutates_environment"] is True


def test_a_read_only_tool_is_not_flagged_as_mutating(tmp_path):
    records = a_simple_session()
    records[1] = assistant_turn(tool_uses=[("call_1", "Read", {"file_path": "x.py"})])
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    call = next(e for e in events if e["type"] == "tool_call")
    assert "mutates_environment" not in call["body"]


def test_run_end_counts_mutating_calls_separately_from_all_tool_calls(tmp_path):
    records = [
        user_prompt("go", ts=TS) | header(),
        assistant_turn(tool_uses=[("a", "Bash", {}), ("b", "Read", {})]),
        tool_result("a", "ok"),
        tool_result("b", "ok"),
    ]
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    totals = next(e for e in events if e["type"] == "run_end")["body"]["totals"]
    assert totals["tool_calls"] == 2
    assert totals["mutating_tool_calls"] == 1


# -- failure capture ------------------------------------------------------------


def test_a_failed_tool_result_is_recorded_as_an_error_not_a_result(tmp_path):
    records = a_simple_session()
    records[2] = tool_result("call_1", "command not found", is_error=True)
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["body"]["error"] == {"message": "command not found"}
    assert "result" not in call["body"]


# -- token accounting: the 40x regression, pinned ------------------------------


def test_cache_reads_are_excluded_from_the_token_total(tmp_path):
    """The bug that overstated a real session by ~40x: cache_read_input_tokens
    recurs on every turn, so summing it across a session counts the same
    context hundreds of times. total_tokens must never include it."""
    records = [
        user_prompt("go", ts=TS) | header(),
        assistant_turn(
            usage={
                "input_tokens": 50,
                "output_tokens": 10,
                "cache_read_input_tokens": 50_000,
                "cache_creation_input_tokens": 0,
            }
        ),
    ]
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    call = next(e for e in events if e["type"] == "llm_call")
    assert call["body"]["usage"]["total_tokens"] == 60  # 50 + 10, not +50,000


def test_context_tokens_includes_cache_reads_because_the_model_did_see_them(tmp_path):
    """Unlike the billed total, what the model *saw* this turn genuinely
    includes every re-read cached token - that is the whole point of showing
    context growth to a reader."""
    records = [
        user_prompt("go", ts=TS) | header(),
        assistant_turn(
            usage={
                "input_tokens": 50,
                "output_tokens": 10,
                "cache_read_input_tokens": 50_000,
                "cache_creation_input_tokens": 0,
            }
        ),
    ]
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    call = next(e for e in events if e["type"] == "llm_call")
    assert call["body"]["context_tokens"] == 50_050


def test_context_delta_is_growth_since_the_previous_turn(tmp_path):
    records = [
        user_prompt("go", ts=TS) | header(),
        assistant_turn(usage={"input_tokens": 100, "output_tokens": 10}),
        assistant_turn(usage={"input_tokens": 150, "output_tokens": 10}),
    ]
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    calls = [e for e in events if e["type"] == "llm_call"]
    assert calls[0]["body"]["context_delta"] == 0
    assert calls[1]["body"]["context_delta"] == 50


# -- one response, written as several records ------------------------------------


def split_response(response_id, blocks, *, usage, final_usage=None, ts=TS):
    """One API response the way Claude Code writes it: a record per content
    block, each repeating the response's usage. The last record may carry a
    larger output count than the earlier ones, written mid-stream."""
    records = []
    for i, block in enumerate(blocks):
        last = i == len(blocks) - 1
        records.append(
            {
                "type": "assistant",
                "timestamp": ts,
                "message": {
                    "id": response_id,
                    "model": "claude-opus-5",
                    "content": [block],
                    "usage": (final_usage or usage) if last else usage,
                    "stop_reason": "tool_use" if last else None,
                },
            }
        )
    return records


def split_session():
    usage = {"input_tokens": 3, "cache_creation_input_tokens": 1_000, "output_tokens": 10}
    final = dict(usage, output_tokens=150)
    return [
        user_prompt("go", ts=TS) | header(),
        *split_response(
            "msg_1",
            [
                {"type": "thinking", "thinking": "..."},
                {"type": "text", "text": "Running the tests."},
                {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}},
            ],
            usage=usage,
            final_usage=final,
        ),
        tool_result("a", "files..."),
    ]


def test_a_response_split_across_records_becomes_one_model_turn(tmp_path):
    transcript = write_transcript(tmp_path / "s.jsonl", split_session())
    events = [
        json.loads(line)
        for line in convert(transcript, tmp_path / "out").read_text("utf-8").splitlines()
    ]
    calls = [e for e in events if e["type"] == "llm_call"]
    assert len(calls) == 1
    assert calls[0]["body"]["response"]["text"] == "Running the tests."
    assert calls[0]["body"]["response"]["tool_calls"] == [{"name": "Bash", "id": "a"}]
    assert calls[0]["body"]["stop_reason"] == "tool_use"


def test_a_split_responses_tokens_are_counted_once_using_the_final_usage(tmp_path):
    """Every record repeats the same usage, so summing per record counted one
    response's tokens two or three times on real sessions."""
    transcript = write_transcript(tmp_path / "s.jsonl", split_session())
    events = [
        json.loads(line)
        for line in convert(transcript, tmp_path / "out").read_text("utf-8").splitlines()
    ]
    call = next(e for e in events if e["type"] == "llm_call")
    assert call["body"]["usage"]["total_tokens"] == 3 + 1_000 + 150
    assert events[-1]["body"]["totals"]["tokens"] == 3 + 1_000 + 150


def test_tool_calls_from_a_split_response_link_to_its_one_turn_even_when_interleaved(tmp_path):
    usage = {"input_tokens": 3, "output_tokens": 10}
    first, second = split_response(
        "msg_1",
        [
            {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {}},
        ],
        usage=usage,
    )
    records = [
        user_prompt("go", ts=TS) | header(),
        first,
        tool_result("a", "contents"),
        second,
        tool_result("b", "ok"),
    ]
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    target = convert(transcript, tmp_path / "out")
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    turn = next(e for e in events if e["type"] == "llm_call")
    tools = [e for e in events if e["type"] == "tool_call"]
    assert [t["body"]["tool"] for t in tools] == ["Read", "Bash"]
    assert all(t["caused_by"] == [turn["seq"]] for t in tools)
    assert verify_file(target).ok


def test_a_tool_use_repeated_within_one_response_is_recorded_once(tmp_path):
    block = {"type": "tool_use", "id": "a", "name": "Bash", "input": {}}
    records = [
        user_prompt("go", ts=TS) | header(),
        *split_response("msg_1", [block, block], usage={"input_tokens": 3, "output_tokens": 1}),
        tool_result("a", "ok"),
    ]
    transcript = write_transcript(tmp_path / "s.jsonl", records)
    events = [
        json.loads(line)
        for line in convert(transcript, tmp_path / "out").read_text("utf-8").splitlines()
    ]
    assert len([e for e in events if e["type"] == "tool_call"]) == 1


# -- malformed input ------------------------------------------------------------


def test_a_transcript_with_no_model_turns_raises_transcript_error(tmp_path):
    transcript = write_transcript(tmp_path / "empty.jsonl", [])
    with pytest.raises(TranscriptError):
        convert(transcript, tmp_path / "out")


def test_a_malformed_line_is_skipped_rather_than_crashing_the_import(tmp_path):
    path = tmp_path / "s.jsonl"
    lines = [json.dumps(r) for r in a_simple_session()]
    lines.insert(2, "{not valid json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target = convert(path, tmp_path / "out")
    assert verify_file(target).ok


# -- discovery: finding a session without being told where it is ---------------


def test_guess_project_dir_matches_claude_codes_own_slugification(tmp_path):
    # Confirmed against a real session directory containing a path with both
    # an underscore and a space: "/Users/x/Downloads/FM_18-dec 2" becomes
    # "-Users-x-Downloads-FM-18-dec-2" - every non-alphanumeric run to "-".
    guessed = guess_project_dir("/Users/x/Downloads/FM_18-dec 2", root=tmp_path)
    assert guessed == tmp_path / "-Users-x-Downloads-FM-18-dec-2"


def test_discover_latest_session_finds_the_newest_file_by_mtime(tmp_path):
    root = tmp_path / "claude-projects"
    project = root / guess_project_dir("/work/repo", root=root).name
    project.mkdir(parents=True)
    import time as _t

    write_transcript(project / "older.jsonl", a_simple_session())
    _t.sleep(0.01)
    newer = write_transcript(project / "newer.jsonl", a_simple_session())

    found = discover_latest_session(cwd="/work/repo", root=root)
    assert found == newer


def test_discover_latest_session_returns_none_for_an_empty_or_missing_directory(tmp_path):
    assert discover_latest_session(cwd="/nowhere/at/all", root=tmp_path) is None


def test_an_explicit_project_dir_is_trusted_without_checking_cwd(tmp_path):
    project = tmp_path / "wherever"
    project.mkdir()
    session = write_transcript(project / "s.jsonl", a_simple_session())
    assert discover_latest_session(project_dir=project) == session


def test_discovery_falls_back_to_matching_the_cwd_field_when_the_guessed_dir_is_missing(tmp_path):
    """The guessed slug directory does not exist (renamed, or the algorithm
    changed underneath us), but some directory's own session still states the
    right cwd - that is ground truth Claude Code wrote about itself, and
    discovery must still find it."""
    root = tmp_path / "claude-projects"
    oddly_named = root / "not-the-slug-you-would-guess"
    oddly_named.mkdir(parents=True)
    session = write_transcript(
        oddly_named / "s.jsonl",
        [user_prompt("go", ts=TS) | header(cwd="/work/myrepo")],
    )
    found = discover_latest_session(cwd="/work/myrepo", root=root)
    assert found == session


def test_discovery_does_not_match_a_directory_for_a_different_cwd(tmp_path):
    root = tmp_path / "claude-projects"
    other = root / "something-else"
    other.mkdir(parents=True)
    write_transcript(other / "s.jsonl", [user_prompt("go", ts=TS) | header(cwd="/work/other")])
    assert discover_latest_session(cwd="/work/myrepo", root=root) is None


# -- tail_once: following a session as it grows ---------------------------------


def test_tail_once_converts_on_the_first_call(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    write_transcript(project / "s.jsonl", a_simple_session())
    state: dict = {}
    target = tail_once(tmp_path / "out", project_dir=project, state=state)
    assert target is not None
    assert verify_file(target).ok


def test_tail_once_does_nothing_when_the_transcript_has_not_changed(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    write_transcript(project / "s.jsonl", a_simple_session())
    state: dict = {}
    first = tail_once(tmp_path / "out", project_dir=project, state=state)
    second = tail_once(tmp_path / "out", project_dir=project, state=state)
    assert first is not None
    assert second is None


def test_tail_once_reconverts_when_the_transcript_grows(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    transcript = write_transcript(project / "s.jsonl", a_simple_session())
    state: dict = {}
    tail_once(tmp_path / "out", project_dir=project, state=state)

    grown = a_simple_session() + [
        assistant_turn(text="one more thing", usage={"input_tokens": 10, "output_tokens": 5})
    ]
    write_transcript(transcript, grown)
    target = tail_once(tmp_path / "out", project_dir=project, state=state)
    assert target is not None
    events = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    assert sum(1 for e in events if e["type"] == "llm_call") == 3


def test_tail_once_returns_none_when_no_session_exists(tmp_path):
    state: dict = {}
    assert tail_once(tmp_path / "out", project_dir=tmp_path / "empty", state=state) is None


def test_tail_once_writes_a_stable_current_pointer(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    write_transcript(project / "s.jsonl", a_simple_session())
    out = tmp_path / "out"
    state: dict = {}
    target = tail_once(out, project_dir=project, state=state)
    pointer = out / claude_code.CURRENT_POINTER
    assert pointer.exists()
    assert pointer.read_text("utf-8") == target.read_text("utf-8")


def test_two_tail_loops_do_not_share_state(tmp_path):
    """state is the caller's dict, not module-level - two independent tail
    loops in one process (two tests; a script and a viewer) must not step on
    each other's notion of what has already been converted."""
    project = tmp_path / "proj"
    project.mkdir()
    write_transcript(project / "s.jsonl", a_simple_session())
    state_a: dict = {}
    state_b: dict = {}
    tail_once(tmp_path / "out_a", project_dir=project, state=state_a)
    second = tail_once(tmp_path / "out_b", project_dir=project, state=state_b)
    assert second is not None  # state_b has never seen this transcript


# -- CLI --------------------------------------------------------------------


def test_cli_import_claude_code_writes_a_trace_and_prints_where(tmp_path, capsys):
    transcript = write_transcript(tmp_path / "s.jsonl", a_simple_session())
    out = tmp_path / "out"
    assert main(["import", "claude-code", str(transcript), "--out", str(out)]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "imported" in printed
    assert list(out.glob("run_*.jsonl"))


def test_cli_import_claude_code_with_a_missing_transcript_exits_usage(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["import", "claude-code", str(tmp_path / "nope.jsonl")])
    assert exc.value.code == EXIT_USAGE


def test_cli_import_claude_code_with_nothing_discoverable_exits_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "DEFAULT_CLAUDE_CODE_ROOT", tmp_path / "empty")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["import", "claude-code"])
    assert exc.value.code == EXIT_USAGE


def test_cli_import_claude_code_sign_produces_a_verifiable_signature(tmp_path, capsys):
    pytest.importorskip("cryptography", reason="signing extra not installed")
    from deposition.signing import generate_key

    transcript = write_transcript(tmp_path / "s.jsonl", a_simple_session())
    key = generate_key()
    key_path = key.write(tmp_path / "k.pem")
    out = tmp_path / "out"
    assert (
        main(
            [
                "import",
                "claude-code",
                str(transcript),
                "--out",
                str(out),
                "--sign",
                str(key_path),
            ]
        )
        == EXIT_OK
    )
    assert "signed with" in capsys.readouterr().out

    target = next(out.glob("run_*.jsonl"))
    result = verify_file(target)
    from deposition.signing import read_sidecar, verify_sidecar

    signature = verify_sidecar(
        read_sidecar(target),
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=key.public_hex,
    )
    assert signature.ok, signature.message
