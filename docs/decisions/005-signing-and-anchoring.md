# 005 — Signing and anchoring the chain

**Status:** accepted · layer 1 shipped 2026-09-29 · **Date:** 2026-09-27
**Raised by:** the prior-art review in [ADR 001](001-positioning.md)

## The problem

Schema v0's hash chain is **unsigned**. [ADR 002](002-schema-v0.md) states what it
proves — nothing was changed, removed from the middle, or reordered — but that
statement is only true against an attacker who *cannot rewrite the file*.

Anyone holding the trace can edit event 12, re-seal 12 through the end with the
same public algorithm, and produce a trace that `deposition verify` calls clean.
There is no secret involved, so there is nothing to forge.

That is fatal to the sentence "prove to someone else that this trace was not
edited", which is the sentence the whole product rests on. The prior-art review
found that essentially every comparable tool signs — Ed25519 is the norm, and
several add RFC 3161 timestamps or transparency-log inclusion proofs.

## What the chain honestly gives today

- **Tamper detection against third parties.** Anyone who did not hold the file
  cannot alter it undetectably — which covers corruption, transport, and storage.
- **A stable identity for a run.** The head hash names the trace's exact content
  in 32 bytes, which is what makes anchoring cheap later.
- **Precise localisation.** Verification names the first broken event, so a
  partially damaged trace is still partially usable.

It does **not** give: proof of authorship, proof of time, or protection against
the person holding the trace. Documentation must say this plainly. It already
does in ADR 002; the README and CLI wording were corrected to match.

## Decision

Three layers, cheapest and most valuable first. None of them changes the event
format, so schema v0 stays frozen — signatures and anchors are **sidecars**
keyed by the head hash.

### 1. Sign the head at run end (Phase 1/2, small)

`run_end` carries the head hash. The SDK signs it with an Ed25519 key and writes
`run_<id>.sig` beside the trace. `deposition verify` reports signature status
alongside chain status.

Key management is the hard part for a local-first tool: a key on the same disk as
the trace protects against very little. Ship it as opt-in
(`deposition.init(signing_key=...)`), be explicit that a local key only raises the
bar, and treat the hosted path below as the real answer.

### 2. Countersign at ingest (Phase 2, the real fix)

The hosted platform holds a key the user does not. On ingest it verifies the
chain, then countersigns the head hash and records the time it was received. That
is a statement no trace holder can forge: *this content existed, in this form, at
this time, and the server saw it.*

This is also the honest upgrade story for the open-source wedge: local traces are
tamper-*detecting*; uploaded traces are tamper-*proof to a third party*. That is
a real reason to have an account, which the free tier alone does not give us.

### 3. Anchor periodically (Phase 3+, only if users ask)

Publish a Merkle root of the day's head hashes to a transparency log, so the
platform's own countersignature does not have to be trusted either. Only worth
building when a real buyer asks who verifies the verifier — regulated users will,
eventually. RFC 6962 is the shape; do not invent one.

## Consequences

- Marketing copy does not use "prove" until layer 2 ships. "Tamper-evident" is
  accurate today; "tamper-proof" is not.
- The audit report export (Phase 3) must render signature and anchor status, not
  just chain status, or it recreates the same overclaim in PDF form.
- Signing is additive: an unsigned v0 trace stays verifiable forever, and a
  signature file is optional metadata. No schema bump.

## Implementation note — layer 1, 2026-09-29

Shipped as `src/deposition/signing.py` behind the `signing` extra
(`cryptography`), so ADR 003 still holds for everyone who does not ask for it.

- `deposition.init(signing_key=...)` takes a PEM path, PEM bytes or a 32-byte
  raw key. It **raises** if the key cannot be loaded. Recording is otherwise
  never allowed to raise, but silently recording unsigned traces for someone who
  asked for signatures is a worse failure than a loud one at startup.
- Each run writes `run_<id>.sig` beside its trace after the exporter closes, so
  a signature only ever exists for a trace that is completely on disk.
- The signature covers the whole sidecar — `run_id`, `head_hash`, `seq`,
  `events`, `public_key`, `signed_at` — not the head hash alone. Signing the
  head alone would let a valid sidecar be lifted onto another run's trace.
- `depo verify --pubkey <hex|file>` pins the expected key; `--require-signature`
  makes unsigned and unpinned traces exit non-zero. **Without a pinned key,
  verification reports `signed by an unverified key`, never `verified`** — the
  sidecar carries its own public key, so a rewriter can re-sign with a key of
  their own. This is the detail that decides whether layer 1 is real or
  decorative, and the CLI wording, the README and the exit codes all have to keep
  saying it.
- A present-and-failing signature exits 1 on its own, because that is positive
  evidence of tampering. An absent one does not, because most traces are
  unsigned and the chain still says something.

Layers 2 and 3 are unchanged and still pending.
