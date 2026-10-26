"""`depo diff` and `depo audit` on tool definitions, sealed as digests.

A security reviewer's first question about an agent is whether the tools the
model was shown changed - between two runs, or under it mid-run. The adapters
seal each definition as a digest (tests/test_instrument.py); these tests are
the claim that diff and audit then say so by name, instead of burying it in a
generic "body differs".
"""

from __future__ import annotations

from deposition.chain import ChainBuilder
from deposition.cli import main
from deposition.instrument._common import tool_digests
from deposition.schema import canonical_json

TS = "2026-10-26T09:00:00.000Z"

REFUND = {"name": "refund_customer", "description": "refund up to $50", "input_schema": {}}
LOOKUP = {"name": "lookup_order", "description": "read an order", "input_schema": {}}


def name_of(tool):
    return tool["name"]


def llm_call_body(*tools):
    return {
        "gen_ai.request.model": "claude-sonnet-5",
        "tools": [t["name"] for t in tools],
        "tool_digests": tool_digests(tools, name_of),
    }


def write_run(path, *turns, mandate=None):
    b = ChainBuilder("run_tools1")
    start = {"agent": "support"} | ({"mandate": mandate} if mandate else {})
    events = [b.append("run_start", start, ts=TS)]
    events += [b.append("llm_call", llm_call_body(*tools), ts=TS) for tools in turns]
    events.append(b.append("run_end", {"status": "ok"}, ts=TS))
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    return path


def diff(tmp_path, capsys, left, right):
    a = write_run(tmp_path / "a.jsonl", left)
    b = write_run(tmp_path / "b.jsonl", right)
    assert main(["diff", str(a), str(b)]) == 0
    return capsys.readouterr().out


def test_changing_one_tools_description_shows_up_in_diff(tmp_path, capsys):
    reworded = {**REFUND, "description": "refund any amount"}
    out = diff(tmp_path, capsys, [REFUND, LOOKUP], [reworded, LOOKUP])
    old = tool_digests([REFUND], name_of)["refund_customer"]
    new = tool_digests([reworded], name_of)["refund_customer"]
    assert f"tool definition changed: refund_customer ({old[:6]}... -> {new[:6]}...)" in out
    assert "lookup_order" not in out


def test_a_definition_change_is_named_not_reported_as_a_generic_body_difference(tmp_path, capsys):
    out = diff(tmp_path, capsys, [REFUND], [{**REFUND, "description": "refund any amount"}])
    assert "body differs" not in out


def test_adding_a_tool_shows_up_in_diff(tmp_path, capsys):
    out = diff(tmp_path, capsys, [LOOKUP], [LOOKUP, REFUND])
    assert "+ tool added: refund_customer" in out


def test_removing_a_tool_shows_up_in_diff(tmp_path, capsys):
    out = diff(tmp_path, capsys, [LOOKUP, REFUND], [LOOKUP])
    assert "- tool removed: refund_customer" in out


def test_identical_definitions_produce_no_tool_change_in_diff(tmp_path, capsys):
    out = diff(tmp_path, capsys, [REFUND, LOOKUP], [dict(REFUND), dict(LOOKUP)])
    assert "no differences in the recorded steps" in out


def test_a_definition_changing_mid_run_shows_up_in_audit(tmp_path, capsys):
    reworded = {**REFUND, "description": "refund any amount"}
    path = write_run(
        tmp_path / "run.jsonl",
        [REFUND],
        [reworded],
        mandate={"allowed_tools": ["refund_customer"]},
    )
    code = main(["audit", str(path)])
    out = capsys.readouterr().out
    old = tool_digests([REFUND], name_of)["refund_customer"]
    new = tool_digests([reworded], name_of)["refund_customer"]
    assert (
        f"!! tool definition changed mid-run: refund_customer "
        f"({old[:6]}... at seq 1 -> {new[:6]}... at seq 2)"
    ) in out
    # informational, not a mandate violation: the verdict is unchanged
    assert "WITHIN MANDATE" in out
    assert code == 0


def test_an_unchanged_definition_is_not_flagged_by_audit(tmp_path, capsys):
    path = write_run(tmp_path / "run.jsonl", [REFUND], [REFUND], mandate={"max_steps": 10})
    main(["audit", str(path)])
    assert "tool definition changed" not in capsys.readouterr().out
