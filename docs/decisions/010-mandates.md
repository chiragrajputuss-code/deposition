# 010 — Mandates: record what the run was allowed to do

**Status:** accepted · **Date:** 2026-10-01

## The problem

A trace answers "what happened". The question an auditor, a compliance officer
or an incident review actually asks is "was it within authority?" — and today
that question is answered by a human reading the trace against a policy that
lives somewhere else, in someone's head or a wiki page that has since changed.

The record of what an agent did is only half of an audit. The other half is the
record of what it was permitted to do, captured *at the same moment*, inside
the same tamper-evident envelope — otherwise the policy can be quietly rewritten
after the fact just as easily as the log could before hashing.

## Decision

`deposition.init()` accepts a **mandate**: a plain dict describing the run's
authority, recorded verbatim in `run_start.body.mandate`. Because it lives in
the hashed body, it is covered by the chain and by any signature — the policy
and the conduct seal together.

v1 of the mandate vocabulary, deliberately small:

| key | meaning |
| --- | --- |
| `issuer` | who granted this authority (informational) |
| `allowed_tools` | if present, every `tool_call` must use one of these |
| `forbidden_tools` | tools that must never appear |
| `max_cost_usd` | cumulative `cost_usd` ceiling |
| `max_tokens` | cumulative token ceiling |
| `max_steps` | ceiling on recorded steps |
| `notes` | informational |

Unknown keys are **recorded but not enforced**, and the audit must say so (see
below). The vocabulary grows by ADR, not by convenience.

### The new verb: `deposition audit`

`audit` replays a trace against its own mandate and reports, with exit codes in
the house contract (0 compliant, 1 not attestable, 2 cannot run):

1. **Chain first.** A broken chain is not auditable — the verdict is about the
   record, and this record is not one.
2. **Each violation named by event**: the `seq`, what rule it broke, and its
   `caused_by` — so "the agent exceeded its authority" arrives with "and this is
   the model turn that led there".
3. **What was not checked.** A mandate key this version cannot enforce is
   listed as unchecked, never silently skipped. An audit that is quiet about
   its own blind spots is the overclaim this product exists to end.
4. **Completeness gates attestation.** If the recorder reported dropped events,
   the verdict is "compliant *as recorded*, but the record admits gaps" and the
   exit code is 1. You cannot attest compliance on a record that says it is
   incomplete.

### UI

The viewer's claim panel grows a fourth chip — **Authority** — beside
Not altered / Authorship / Completeness: *within mandate*, *N violations*, or
*none declared* (neutral: most runs will not carry a mandate, and that is not a
defect).

## What this is not

Not an enforcement system. The mandate does not stop the agent at runtime — it
makes the overrun provable afterwards. Runtime guardrails are a different
product with different failure modes; conflating them would put Deposition in
the hot path it has promised never to occupy ("recording must never break the
host agent").

## Consequences

- Additive only: a body field and a CLI command. No schema bump; traces without
  mandates behave exactly as before.
- Composes with ADR 009: a parent passing a sub-mandate to a child agent at the
  interlock point yields a delegation chain with authority recorded at every
  hop.
- The refund/injection examples should eventually carry mandates, so the
  flagship demo shows `audit` catching the exfiltration mail as a named
  violation rather than a narrated one.
