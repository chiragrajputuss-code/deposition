# 016 — Host-attested and corroborated: integrity is not verification

**Status:** accepted · **Date:** 2026-10-26
**Raised by:** public feedback, October 26
**Amends:** the status names in [ADR 014](014-rfc3161-time-anchoring.md)

## The problem

"Verified" was doing two jobs. `depo verify` said `OK  42 events verified`, and a
checked RFC 3161 timestamp was reported as `verified`. Both read as "these events
happened", which nothing Deposition records today can show: the process writing
the trace is the process running the tools. What `verify` actually checks is that
the record is unedited since it was written.

## Decision

Two labels, used the same way in the README section "What a Deposition trace
proves, and what it does not", `depo verify --help` and the standalone verifier's
header (a test pins the wording in all three):

- **Host-attested:** recorded only by the machine running the agent - in-process
  steps, local tools, model calls as the host saw them. It means what the host
  claims happened, unedited since it was written. **Everything Deposition records
  today is host-attested.**
- **Corroborated:** an external event - an API call, a payment, an email, an
  external write - confirmed by the counterparty that handled it, for example by a
  receipt signed with a key the agent host cannot use. **Not implemented yet.**

Rules that follow:

1. "Verified" is never used for host-attested events. The section opens with one
   sentence: verification checks the integrity of the record, not that the events
   in it happened. The chain verdict now reads `OK  42 events, record intact`.
2. "Verified" stays only where something cryptographic about the *record* is
   checked against a key the user pinned - "signature verified against <key>" -
   which is a statement about who signed the record, not about what it records.
3. A timestamp is described as "unedited since T", never as verification. ADR
   014's statuses are renamed: `verified` → **`anchored`** (the authority's
   signature on the token checked out against the user's `--tsa-ca`), `unverified`
   → **`unchecked`** (the token covers the head; its signature was not checked).
   Nothing about what is checked, or the exit codes, changes.
4. "What would raise the bar" is ordered by how much each step moves an event from
   host-attested toward corroborated: counterparty receipts ([ADR 009](009-interlocking-traces.md),
   proposed) first, then hosted countersigning (ADR 005 layer 2), then moving keys
   off the agent host.

## Consequences

- `chain.py`'s summary text changes (tested); exit codes, error codes and the JSON
  shape do not. The standalone verifier changes identically.
- The renamed timestamp statuses had not reached PyPI, so no released consumer
  depended on the old names.
- Corroboration is the direction that actually answers "did it happen?"; until a
  counterparty signs something, every sentence about a trace stays at
  host-attested.
