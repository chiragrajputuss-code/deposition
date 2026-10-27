# 017 — `verify` tells a finished run from an intact prefix

**Status:** accepted · **Date:** 2026-10-27
**Raised by:** public feedback - "one hole I'd explicitly test is suffix truncation"
**Amends:** the exit-code meaning in [ADR 002](002-schema-v0.md) rule 5, which this does not edit

## The problem

Cutting events off the end of a trace leaves a shorter chain that verifies on its
own: every event that is left is untouched. ADR 002 said so ("it does not prove
nothing was removed from the end") and named `run_end` as what closes the gap -
but only the viewer looked for it. Tested on 2026-10-27: the demo trace with its
last three events removed passed `depo verify` with `OK` and exit 0. A signed trace
cut the same way did fail, but the message said it had been "rewritten", which
was wrong.

## Decision

`depo verify` and the standalone verifier report **completeness** beside chain,
signature and timestamp, whenever the chain holds:

| status | meaning | exit |
| --- | --- | --- |
| `complete` | the trace ends with `run_end`, and the step count it declares (when it declares one) matches the events present | 0 |
| `incomplete` | no `run_end`: the run was cut off or never finished; every event present is intact, but this is not the whole run | 1 |
| `count_mismatch` | `run_end` declares N steps but the trace holds M: it was rebuilt after the run ended | 1 |

- A trace that cannot show it is the whole run does not pass, even when every
  retained event is intact. The headline says `INCOMPLETE`, never `OK`.
- `count_mismatch` can only appear on a chain that verifies, which means someone
  re-sealed it - so it is a failure, not a gap. The count is `run_end`'s
  `totals.steps`, which the recorder, the Claude Code importer and the examples
  all write as "events between `run_start` and `run_end`".
- A signed trace cut short now says *"signature covers 17 events but this trace
  has 14 - its end is missing"*: the signature sidecar already carries run id,
  event count and final head, which is the independent terminal record a verifier
  needs.
- A crashed run - no `run_end` because the process died - is reported incomplete
  too. That is correct: the record cannot attest the rest of the run.

## What it does not change

Completeness is about the record, not about whether the recorded actions
happened (ADR 016). An unsigned trace can still be truncated and re-sealed with a
forged `run_end` and a matching count by whoever holds the file; only the
signature or a timestamp catches that. Dropped events already break the chain
(their sequence numbers are missing), so they never reach this check.
