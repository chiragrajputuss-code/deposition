# 001 — Positioning: how Postflight differs

**Status:** accepted, prior-art review incomplete · **Date:** 2026-09-27

## The wedge

Postflight is **replay + tamper-evidence + root cause**, not monitoring.

LangSmith, Langfuse and AgentOps own the observability-metrics ground and are
better resourced than a solo developer with 8–10 hours a week. Competing on
dashboards, evals or prompt management is competing on their terms and losing.

The bet is that a different question is under-served. Not *how is my agent
performing?* but:

> *What exactly did this agent do, why did it do it, and can I prove to someone
> else that this record was not edited?*

That question comes from incident review, disputes and audits, not from daily
monitoring. It is the question a flight recorder answers.

## The three claims

1. **Replay.** A run is an append-only event log, so it can be walked step by
   step after the fact, offline, with no account.
2. **Tamper-evidence.** Every event is hash-chained. `postflight verify` names
   the first event where a trace stopped being trustworthy. See
   [002-schema-v0.md](002-schema-v0.md) for exactly what that proves and what it
   does not.
3. **Root cause.** `caused_by` edges make the trace explain itself: the viewer
   walks backwards from a failure to the model output that caused it.

Debugging tools stop at (1). (2) and (3) are the moat, and they compound: an
audit report is a chain verification plus a causal summary, which is a Phase 3
feature built from Phase 1 primitives.

## Known competition (as recorded Sep 2026)

- Two indie **"Retrace"** products — record / replay / fork.
- **AgentNotary** — an open-source governance CLI.

## Prior art review — TODO

The Week 1 checklist requires reading both Retrace products' docs and
AgentNotary's README before this ADR is final. **That has not been done**, and
this file deliberately does not characterise their features beyond the one-line
descriptions above rather than guessing at them.

When the review happens, this section should be replaced with, for each product:

- what it records and in what format
- whether the record is verifiable by a third party, or only replayable by its owner
- whether it explains causality or only lists steps
- where it stores data and what that implies for an audit
- the single sentence that says why someone would choose Postflight instead

If that last sentence cannot be written honestly for a product, that is a signal
about the wedge, not a writing problem — take it to the Phase 1 gate review.

## Response to the obvious threat

If LangSmith or Langfuse ship replay/fork, the answer does not change: tamper
evidence and exportable audit reports are the moat; metrics never were. Lean
harder into the forensic angle and publish a comparison page.
