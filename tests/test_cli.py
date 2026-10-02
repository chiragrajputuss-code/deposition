"""CLI tests.

``deposition verify`` is the command someone runs to prove to a third party that
a trace was not edited, so its exit code is a contract: 0 verified, 1 broken,
2 could not run.
"""

from __future__ import annotations

import json

import pytest

from deposition.chain import ChainBuilder
from deposition.cli import EXIT_BROKEN, EXIT_OK, EXIT_USAGE, main
from deposition.schema import EventType, canonical_json

TS = "2026-09-27T08:14:03.412Z"


def write_run(path, *, run_id="run_9f3kQ2", tool="search", extra_step=False):
    builder = ChainBuilder(run_id)
    events = [
        builder.append(EventType.RUN_START, {"agent": "triage-agent"}, ts=TS),
        builder.append(EventType.LLM_CALL, {"gen_ai.request.model": "claude-sonnet-5"}, ts=TS),
        builder.append(
            EventType.TOOL_CALL, {"tool": tool, "latency_ms": 12.0}, caused_by=[1], ts=TS
        ),
    ]
    if extra_step:
        events.append(builder.append(EventType.DECISION, {"label": "escalate"}, ts=TS))
    events.append(builder.append(EventType.RUN_END, {"status": "ok"}, ts=TS))
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    return path


def tamper(path, seq, key, value):
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        event = json.loads(line)
        if event["seq"] == seq:
            event["body"][key] = value
            lines[index] = canonical_json(event)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# -- verify -----------------------------------------------------------------


def test_verify_returns_zero_and_says_ok(tmp_path, capsys):
    path = write_run(tmp_path / "run.jsonl")
    assert main(["verify", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "OK" in out and "run_9f3kQ2" in out


def test_verify_returns_one_on_a_tampered_trace(tmp_path, capsys):
    path = write_run(tmp_path / "run.jsonl")
    tamper(path, 2, "tool", "delete_everything")
    assert main(["verify", str(path)]) == EXIT_BROKEN
    out = capsys.readouterr().out
    assert "BROKEN" in out
    assert "hash_mismatch" in out
    # The user needs to know how much of the trace still stands.
    assert "Everything before the reported event is still intact" in out


def test_verify_checks_several_traces_and_fails_if_any_is_broken(tmp_path, capsys):
    good = write_run(tmp_path / "good.jsonl")
    bad = write_run(tmp_path / "bad.jsonl", run_id="run_other")
    tamper(bad, 1, "gen_ai.request.model", "gpt-4")
    assert main(["verify", str(good), str(bad)]) == EXIT_BROKEN
    out = capsys.readouterr().out
    assert "OK" in out and "BROKEN" in out


def test_verify_json_output_is_machine_readable(tmp_path, capsys):
    path = write_run(tmp_path / "run.jsonl")
    tamper(path, 2, "tool", "x")
    assert main(["verify", "--json", str(path)]) == EXIT_BROKEN
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["ok"] is False
    assert payload[0]["errors"][0]["code"] == "hash_mismatch"
    assert payload[0]["errors"][0]["seq"] == 2


def test_verify_json_reports_the_head_hash_of_a_good_trace(tmp_path, capsys):
    path = write_run(tmp_path / "run.jsonl")
    assert main(["verify", "--json", str(path)]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert len(payload[0]["head_hash"]) == 64


def test_a_missing_file_is_a_usage_error_not_a_crash(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["verify", str(tmp_path / "nope.jsonl")])
    assert excinfo.value.code == EXIT_USAGE
    assert "no such trace file" in capsys.readouterr().err


def test_a_directory_is_a_usage_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["verify", str(tmp_path)])
    assert excinfo.value.code == EXIT_USAGE
    assert "is a directory" in capsys.readouterr().err


# -- diff -------------------------------------------------------------------


def test_diff_of_identical_runs_reports_no_differences(tmp_path, capsys):
    left = write_run(tmp_path / "a.jsonl")
    right = write_run(tmp_path / "b.jsonl", run_id="run_other")
    assert main(["diff", str(left), str(right)]) == EXIT_OK
    assert "no differences in the recorded steps" in capsys.readouterr().out


def test_diff_reports_an_added_step_and_the_first_divergence(tmp_path, capsys):
    left = write_run(tmp_path / "a.jsonl")
    right = write_run(tmp_path / "b.jsonl", run_id="run_other", extra_step=True)
    assert main(["diff", str(left), str(right)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "+ seq 3  decision(escalate)" in out
    assert "first divergence at seq 3" in out


def test_diff_reports_a_changed_tool(tmp_path, capsys):
    left = write_run(tmp_path / "a.jsonl", tool="search")
    right = write_run(tmp_path / "b.jsonl", run_id="run_other", tool="refund")
    main(["diff", str(left), str(right)])
    out = capsys.readouterr().out
    assert "- seq 2  tool_call(search)" in out
    assert "+ seq 2  tool_call(refund)" in out


def test_diff_ignores_latency_and_other_per_run_noise(tmp_path, capsys):
    # Two runs of the same agent always differ in timing; a diff that shouts
    # about it is a diff nobody reads.
    left = write_run(tmp_path / "a.jsonl")
    right = write_run(tmp_path / "b.jsonl", run_id="run_other")
    tamper(right, 2, "latency_ms", 987.6)
    main(["diff", str(left), str(right)])
    assert "no differences in the recorded steps" in capsys.readouterr().out


def test_diff_reports_a_body_change_on_an_aligned_step(tmp_path, capsys):
    left = write_run(tmp_path / "a.jsonl")
    right = write_run(tmp_path / "b.jsonl", run_id="run_other")
    tamper(right, 2, "arguments", {"q": "different query"})
    main(["diff", str(left), str(right)])
    out = capsys.readouterr().out
    assert "body differs" in out
    assert "first divergence at seq 2" in out


# -- top level --------------------------------------------------------------


def test_no_command_prints_help_and_returns_two(capsys):
    assert main([]) == EXIT_USAGE
    out = capsys.readouterr().out
    # The program name comes from argv[0] so that `depo` and `deposition` each
    # describe themselves correctly; assert on the commands, not the prog name.
    assert "usage:" in out
    for command in ("view", "verify", "diff"):
        assert command in out


def test_version_reports_the_schema_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "trace schema v1.0" in capsys.readouterr().out
