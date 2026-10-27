"""`depo verify` tells a finished run from an intact prefix of one (ADR 017).

Cutting events off the end of a trace leaves a shorter chain that verifies on
its own - every event that is left is untouched. So the chain alone cannot say
whether the run finished. ``run_end`` can: it is sealed last and declares how
many steps the run recorded. Raised publicly as "suffix truncation".
"""

from __future__ import annotations

import json

import deposition
from deposition.chain import ChainBuilder, seal
from deposition.cli import main
from deposition.schema import canonical_json
from deposition.signing import generate_key, sign_head, write_sidecar

TS = "2026-10-27T09:00:00.000Z"


def build(*, steps=3, totals=True):
    b = ChainBuilder("run_cut0001")
    end = {"status": "ok"} | ({"totals": {"steps": steps}} if totals else {})
    return [
        b.append("run_start", {"agent": "a"}, ts=TS),
        b.append("llm_call", {"provider": "test"}, ts=TS),
        b.append("tool_call", {"tool": "send_email"}, caused_by=[1], ts=TS),
        b.append("decision", {"label": "done"}, ts=TS),
        b.append("run_end", end, ts=TS),
    ]


def write(path, events):
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    return path


def verify_json(path, capsys, *args):
    code = main(["verify", str(path), "--json", *map(str, args)])
    return code, json.loads(capsys.readouterr().out)[0]


def test_a_finished_run_is_reported_complete(tmp_path, capsys):
    code, result = verify_json(write(tmp_path / "run.jsonl", build()), capsys)
    assert result["completeness"]["status"] == "complete"
    assert code == 0


def test_an_intact_prefix_without_run_end_is_incomplete_and_fails(tmp_path, capsys):
    path = write(tmp_path / "run.jsonl", build()[:-2])
    code, result = verify_json(path, capsys)
    assert result["chain_ok"] is True  # every event left is untouched
    assert result["completeness"]["status"] == "incomplete"
    assert result["ok"] is False
    assert code == 1


def test_an_incomplete_trace_never_headlines_ok(tmp_path, capsys):
    path = write(tmp_path / "run.jsonl", build()[:-1])
    main(["verify", str(path)])
    out = capsys.readouterr().out
    assert out.startswith("INCOMPLETE  4 events intact for run_cut0001, but no run_end")
    assert "!! completeness: no run_end" in out


def test_a_trace_rebuilt_with_fewer_steps_than_run_end_declares_fails(tmp_path, capsys):
    """Drop a middle event and re-seal: the chain holds, the declared count does not."""
    events = build()
    kept = [events[0], events[1], events[3], events[4]]
    rebuilt, prev = [], "0" * 64
    for seq, event in enumerate(kept):
        body = {k: v for k, v in event.items() if k not in ("hash", "caused_by")}
        rebuilt.append(seal(dict(body, seq=seq), prev))
        prev = rebuilt[-1]["hash"]
    code, result = verify_json(write(tmp_path / "run.jsonl", rebuilt), capsys)
    assert result["chain_ok"] is True
    assert result["completeness"]["status"] == "count_mismatch"
    assert "declares 3 steps but the trace holds 2" in result["completeness"]["message"]
    assert code == 1


def test_a_run_end_without_a_step_count_is_still_complete(tmp_path, capsys):
    code, result = verify_json(write(tmp_path / "run.jsonl", build(totals=False)), capsys)
    assert result["completeness"]["status"] == "complete"
    assert code == 0


def test_a_broken_chain_is_not_also_judged_for_completeness(tmp_path, capsys):
    events = build()
    events[2] = dict(events[2], body={"tool": "edited"})
    _, result = verify_json(write(tmp_path / "run.jsonl", events), capsys)
    assert result["completeness"]["status"] == "unknown"


def test_a_signed_trace_cut_short_says_its_end_is_missing_not_that_it_was_rewritten(
    tmp_path, capsys
):
    key = generate_key()
    events = build()
    path = write(tmp_path / "run.jsonl", events)
    write_sidecar(
        sign_head(key, run_id="run_cut0001", head_hash=events[-1]["hash"], seq=4, events=5), path
    )
    write(path, events[:-2])
    code = main(["verify", str(path), "--pubkey", key.public_hex])
    out = capsys.readouterr().out
    assert "signature covers 5 events but this trace has 3 - its end is missing" in out
    assert "rebuilt after signing" not in out
    assert code == 1


def test_a_run_recorded_by_the_sdk_declares_exactly_the_steps_it_holds(trace_dir, capsys):
    deposition.init("p", directory=trace_dir, instrument=False)

    @deposition.record(name="agent")
    def work():
        deposition.annotate("one")
        with deposition.step("tool_call", tool="search") as body:
            body["result"] = "ok"

    work()
    path = deposition._recorder._runs[-1].path
    deposition.shutdown()
    code, result = verify_json(path, capsys)
    assert result["completeness"]["status"] == "complete"
    assert code == 0
