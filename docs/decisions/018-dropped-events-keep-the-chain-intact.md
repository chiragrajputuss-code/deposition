# 018 — A dropped event is counted, never a gap in the chain

**Status:** accepted · **Date:** 2026-10-27
**Corrects:** one sentence in [ADR 017](017-verify-reports-completeness.md)

## The problem

ADR 010 designed for dropped events: the recorder counts them in `run_end`, the
chain stays intact, and `audit` refuses to attest a record that admits gaps. The
viewer has a `lossy` status for exactly that case. None of it could happen.

`Run.emit` sealed an event into the chain *before* handing it to the exporter. When
the exporter's queue was full it dropped the already-sealed event, so its seq was
missing from the file. Tested on 2026-10-27: one dropped event made the trace
`BROKEN  seq_break` - indistinguishable from tampering, with everything after the
gap unverifiable. ADR 017 then repeated the consequence as if it were design
("dropped events already break the chain").

A second defect sat beside it: the event was queued *after* the lock that assigned
its seq was released, so two threads recording into one run could write their
events out of seq order.

## Decision

- `Exporter.export` returns whether it accepted the event.
- `Run.emit` seals and queues under one lock. If the exporter drops the event, the
  chain is rolled back to before it: the dropped event never takes a seq, the file
  has no gap, and `run_end.totals.dropped` counts it. Steps, tokens and cost count
  only events that are in the record, so ADR 017's step-count check holds on a
  lossy trace.
- `emit` - and so `deposition.log` - returns `None` for a dropped event, because
  its seq will be reused by the next event and must not be cited as a cause.

## What still leaves a gap

An event the writer thread fails to write *after* accepting it (a disk error, say)
cannot be rolled back: later events are already chained to it. That trace is
reported broken, which is honest - the file is not the record that was sealed.

## Consequences

- A trace with dropped events verifies, `depo verify` prints the count, the viewer
  shows `lossy`, and `depo audit` reports *not attestable*, as ADR 010 intended.
- Queue order now matches seq order across threads by construction.
