"""Mandate audits (ADR 010).

A trace records what happened; the mandate inside it records what was allowed.
These tests are the claim that `audit` judges the first against the second,
names every overrun by event, admits what it cannot check, and refuses to
attest compliance on a record that says it is incomplete.
"""

from __future__ import annotations

import json

import pytest

import deposition
from deposition.chain import ChainBuilder
from deposition.cli import EXIT_BROKEN, EXIT_OK, EXIT_USAGE, main
from deposition.mandate import audit
from deposition.schema import EventType, canonical_json

TS = "2026-10-01T09:00:00.000Z"

MANDATE = {
    "issuer": "ops@example.com",
    "allowed_tools": ["refund_policy", "issue_refund"],
    "max_cost_usd": 0.05,
    "notes": "support refunds only",
}


def build(mandate=MANDATE, tools=("refund_policy", "issue_refund"), costs=(0.01,), totals=None):
    builder = ChainBuilder("run_aud1t0")
    header = {"agent": "refund-agent"}
    if mandate is not None:
        header["mandate"] = mandate
    events = [builder.append(EventType.RUN_START, header, ts=TS)]
    llm = builder.append(
        EventType.LLM_CALL,
        {"provider": "example", "cost_usd": costs[0], "usage": {"total_tokens": 100}},
        ts=TS,
    )
    events.append(llm)
    for tool in tools:
        events.append(
            builder.append(EventType.TOOL_CALL, {"tool": tool}, caused_by=[llm["seq"]], ts=TS)
        )
    for extra in costs[1:]:
        events.append(builder.append(EventType.LLM_CALL, {"cost_usd": extra}, ts=TS))
    events.append(
        builder.append(
            EventType.RUN_END,
            {"status": "ok", "totals": dict({"dropped": 0, "adopted": 0}, **(totals or {}))},
            ts=TS,
        )
    )
    return events


def write(path, events):
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    return path


def test_a_compliant_run_is_attestable_and_says_what_was_checked():
    result = audit(build())
    assert result.attestable
    assert sorted(result.checked) == ["allowed_tools", "max_cost_usd"]
    assert not result.violations


def test_a_tool_outside_allowed_tools_is_named_by_event_and_cause():
    result = audit(build(tools=("refund_policy", "send_email")))
    assert not result.attestable
    violation = result.violations[0]
    assert violation.code == "tool_not_allowed"
    assert "send_email" in violation.message
    assert violation.caused_by == (1,)  # the model turn that led there


def test_a_forbidden_tool_is_a_violation_even_when_also_listed_as_allowed():
    mandate = dict(MANDATE, forbidden_tools=["issue_refund"])
    result = audit(build(mandate=mandate))
    assert [v.code for v in result.violations] == ["forbidden_tool"]


def test_a_cost_ceiling_is_flagged_once_at_the_event_that_crosses_it():
    result = audit(build(costs=(0.03, 0.01, 0.04, 0.02)))
    overruns = [v for v in result.violations if v.code == "max_cost_usd"]
    assert len(overruns) == 1
    assert "0.05" in overruns[0].message


def test_an_unknown_mandate_key_is_reported_as_unchecked_never_skipped():
    mandate = dict(MANDATE, data_boundaries=["no PII leaves the EU"])
    result = audit(build(mandate=mandate))
    assert result.unchecked == ["data_boundaries"]
    assert result.attestable  # unknown keys do not fail the audit; they are disclosed


def test_dropped_events_block_attestation_even_with_no_violation():
    result = audit(build(totals={"dropped": 2}))
    assert not result.violations
    assert not result.attestable
    assert "admits gaps" in result.summary()


def test_a_trace_without_a_mandate_is_not_auditable():
    result = audit(build(mandate=None))
    assert result.mandate is None
    assert not result.attestable


def test_init_records_the_mandate_inside_run_start(trace_dir):
    deposition.init("refunds", directory=trace_dir, instrument=False, mandate=MANDATE)

    @deposition.record(name="agent")
    def run():
        pass

    run()
    deposition.shutdown()
    trace = next(trace_dir.glob("run_*.jsonl"))
    events = [json.loads(line) for line in trace.read_text("utf-8").splitlines()]
    assert events[0]["body"]["mandate"] == MANDATE


# -- CLI ----------------------------------------------------------------------


def test_audit_exits_zero_for_a_compliant_trace(tmp_path, capsys):
    path = write(tmp_path / "run.jsonl", build())
    assert main(["audit", str(path)]) == EXIT_OK
    assert "WITHIN MANDATE" in capsys.readouterr().out


def test_audit_exits_one_and_names_the_violation(tmp_path, capsys):
    path = write(tmp_path / "run.jsonl", build(tools=("send_email",)))
    assert main(["audit", str(path)]) == EXIT_BROKEN
    out = capsys.readouterr().out
    assert "send_email" in out and "caused_by 1" in out


def test_audit_discloses_what_it_could_not_check(tmp_path, capsys):
    mandate = dict(MANDATE, data_boundaries=["eu-only"])
    path = write(tmp_path / "run.jsonl", build(mandate=mandate))
    main(["audit", str(path)])
    assert "NOT checkable" in capsys.readouterr().out


def test_audit_without_a_mandate_is_a_usage_outcome_not_a_pass(tmp_path, capsys):
    path = write(tmp_path / "run.jsonl", build(mandate=None))
    assert main(["audit", str(path)]) == EXIT_USAGE
    assert "NO MANDATE" in capsys.readouterr().out


def test_audit_refuses_a_broken_chain(tmp_path, capsys):
    events = build()
    events[2]["body"]["tool"] = "edited_after_the_fact"  # break the hash
    path = write(tmp_path / "run.jsonl", events)
    assert main(["audit", str(path)]) == EXIT_BROKEN
    assert "Cannot audit" in capsys.readouterr().out


# -- viewer -------------------------------------------------------------------


def test_the_viewer_reports_authority_alongside_the_other_claims(tmp_path):
    pytest.importorskip("fastapi", reason="viewer extra not installed")
    from deposition.viewer import load_trace

    good = write(tmp_path / "good.jsonl", build())
    assert load_trace(good)["authority"]["status"] == "ok"

    bad = write(tmp_path / "bad.jsonl", build(tools=("send_email",)))
    claims = load_trace(bad)["authority"]
    assert claims["status"] == "violations"
    assert claims["count"] == 1

    plain = write(tmp_path / "plain.jsonl", build(mandate=None))
    assert load_trace(plain)["authority"]["status"] == "none"
