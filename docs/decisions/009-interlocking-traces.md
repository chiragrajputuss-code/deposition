# 009 — Interlocking traces: receipts between agents

**Status:** proposed, awaiting decision · **Date:** 2026-10-01
**Raised by:** asking what the unit of dispute is once agents call agents

## The problem

Everything Deposition records today assumes the dispute is about one run: did
*my* agent do X. As agents start calling other agents — across teams, and then
across companies — the dispute moves to the interaction: your agent told mine
the shipment was confirmed, mine acted on it, someone ate the loss. Each side
holds its own log, each log says its own agent behaved, and neither log can say
anything about the other's.

Single-run tamper evidence is also the part of this product that is
commoditising (ADR 001's table, NovaFabric since). The space *between* agents is
empty, and it gets more valuable with every agent deployed.

## Decision (proposed)

When a recorded agent calls another recorded agent, the two chains commit to
each other at the moment of the call. Neither party can later rewrite its half
without the other's trace exposing it.

### The handshake

The caller attaches one header to the outbound request:

```
Deposition-Link: v=1; run=<run_id>; seq=<next_seq>; head=<head_hash>
```

`head` is the caller's chain head *at the moment of sending* — a 32-byte
commitment to everything the caller's run contains so far.

The receiver, if recording, writes that commitment into its own chain as an
event, and answers with the same header describing its own run. The caller
writes the response commitment into its chain.

After the exchange:

- B's chain contains A's `(run_id, seq, head)` — A cannot rewrite anything at or
  before that point without the hash B holds going stale.
- A's chain contains B's — symmetrically.

### Verification

`deposition verify --linked a.jsonl b.jsonl` finds the paired commitments and
checks each against the other chain's actual hashes at the named positions. The
verdict names each link as **held** (both commitments check out), **broken**
(a chain was rewritten after the exchange), or **one-sided** (only one party
recorded it — still evidence, held by whoever did).

### What it honestly proves, and does not

- It proves the interaction happened, when in each run it happened, and pins
  both chains' contents as of that moment. One honest party is enough to pin a
  dishonest one — that is the point.
- It does **not** prove the request or response *content* beyond what each side
  recorded, and two colluding parties can still fabricate a matching pair from
  scratch. Countersigning at ingest (ADR 005 layer 2) is what closes that, by
  adding a witness neither party controls.

### Fit with the schema

No schema change. The caller's commitment travels inside the `body` of the
events it already writes; the receiver records the inbound commitment as an
`annotation` with a structured body. If ADR 008's v1 bump is accepted, v1 may
add a first-class `link` event type; the wire format above does not depend on
it.

### Delegation is the same mechanism

A parent run spawning a sub-agent is a call like any other: the child's
`run_start` records the parent's `(run_id, seq, head)`, the parent records the
child's. Combined with a mandate (ADR 010) passed down at the same moment, the
result is the thing an audit of a multi-agent system actually needs: who
authorised whom to do what, and what each actually did, provable at every hop.

## Standards posture

Added 2026-10-01, while still proposed: protocols win by adoption politics,
which a solo developer with a day job cannot drive. So this spec's usefulness
must never depend on anyone else adopting it - two Deposition users verifying
each other is the product feature, and that works with zero outside adoption.
Where an emerging standard offers a seat (the IETF agent-audit-trail family,
A2A-style headers), ride it rather than compete with it; "Deposition-Link" is a
default wire name, not a flag to plant.

## Why spec-first, not code-first

This is a wire format other implementations would have to match — the same
class of decision ADR 008 exists to repair. A header format frozen casually is
frozen forever. The spec should sit public for comment (it is also the single
cheapest claim-staking artifact the launch can carry); the reference
implementation follows acceptance.

## Consequences

- One page of the docs becomes a protocol spec with the header grammar, the
  receiver's obligations, and the verification procedure, written so a non-
  Deposition implementation can interoperate.
- `verify --linked` becomes part of the CLI contract (exit 0 all links held,
  1 any broken, 2 cannot run).
- The honest-marketing rule continues: "pins both parties" only after
  `--linked` ships with tests; collusion resistance is never claimed without
  layer 2.
