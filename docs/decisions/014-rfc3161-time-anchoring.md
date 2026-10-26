# 014 — Independent proof of time: RFC 3161 timestamps on the chain head

**Status:** accepted · **Date:** 2026-10-26
**Raised by:** public feedback - "the cheap fix short of an HSM is an RFC 3161
timestamping service on the chain head... it outsources the 'when did this exist'
question to someone who isn't you"
**Builds on:** [ADR 005](005-signing-and-anchoring.md), which this does not edit

## Why

ADR 005 deferred anchoring to layer 3, "only if users ask". A user asked. Today
nothing in a trace says when it existed except timestamps asserted by the machine
that wrote it - event `ts` and the signature's `signed_at` - which is exactly the
party a disputed record cannot be trusted on.

RFC 3161 is a cheaper, older and more standard step than the transparency log ADR
005 sketched for layer 3: send a hash to a timestamp authority (TSA), get back a
token the TSA signed, saying it saw that hash no later than time T.

## What it proves, and what it does not

- **Proves:** a third party saw this exact head hash no later than time T. That
  narrows the window for backdating or fabricating a trace after the fact: a
  rewritten trace has a different head, and the old token no longer matches it.
- **Does not prove** that the recorded events happened, or that they happened at
  T - only that the trace existed by then. The process writing the trace is still
  the process running the tools.
- **Does not replace** layer 2 countersigning, which also establishes that a
  server *received* the trace. A TSA sees one 32-byte hash and nothing else.

## Privacy

Only the 32-byte head hash leaves the machine. Never trace contents, never the
run id, never the file name.

## Zero dependencies

- The `TimeStampReq` is built in DER by hand with the standard library: version 1,
  a SHA-256 message imprint, a random nonce, `certReq` true. The imprint is the
  head hash itself - which is honest, since the head hash *is* the SHA-256 of the
  last event's canonical bytes.
- It is sent with `urllib` as `application/timestamp-query`.
- The raw `TimeStampResp` bytes are stored unchanged as `run_<id>.tsr`, so any
  RFC 3161 tool can read them.
- Verifying the token's signature needs CMS and X.509, which the standard library
  lacks. So this code parses only enough DER to check the token's message imprint
  against the head hash, and **the signature check is OpenSSL's**: `openssl ts
  -verify`, which `depo verify` prints, or runs itself with a CA file the user
  passes. An optional extra that verifies in-process is possible later, by a new
  ADR.

## Timing

- Phase 1 timestamps the head at `run_end` only, after the exporter closes and
  after signing - the same point ADR 005 signs at.
- The request runs on a background thread, never on the caller's: the run returns
  without waiting for the TSA. `deposition.shutdown()` waits for outstanding
  requests within its usual timeout; a process that exits without calling it may
  lose the timestamp, and the status file then says the request never completed.
- Periodic anchoring during long runs (suggested in the same feedback: every few
  minutes) is a recorded follow-up, not built.

## Failure

The TSA being slow, down or wrong must never block or fail the user's app.

- A short timeout (5 seconds), no retries.
- `run_<id>.timestamp.json` is written as `pending` before the request and
  replaced with `granted` or `unavailable` (with the reason) after it. A TSA error
  is never an exception in the host.
- A token whose imprint or nonce does not match the request is not stored; the
  status records why.

## Which authority

The user configures the URL: `deposition.init(timestamp_url=...)`. There is **no
default authority**. Choosing one is a trust decision this project should not make
silently on anyone's behalf; a default would need the maintainer's explicit
approval and its own note here.

## Verification statuses

`depo verify` reports a third line beside chain and signature:

| status | meaning | exit |
| --- | --- | --- |
| `none` | no timestamp beside this trace | 0 |
| `unavailable` | requested, but the TSA did not answer or refused | 0 |
| `unverified` | imprint matches the head; the TSA's signature has not been checked | 0 |
| `verified` | imprint matches, and `openssl ts -verify` passed against `--tsa-ca` | 0 |
| `imprint_mismatch` | the token covers a different head - rewritten after timestamping | 1 |
| `bad_timestamp` | `openssl ts -verify` rejected the token against `--tsa-ca` | 1 |
| `malformed` | the `.tsr` cannot be parsed, or its nonce does not match the request | 1 |

**Never `verified` without a CA file the user chose**, the same principle as the
pinned signing key: a token carries the TSA's certificate, and anyone can stand up
a TSA.

## Consequences

- New stdlib-only module `deposition/timestamp.py`; no new dependency (ADR 003).
- No schema bump: like signatures, timestamps are sidecars keyed by the head hash.
- The standalone verifier checks the imprint and prints the `openssl ts -verify`
  command.
- The README's "proves / does not prove" section gains its time sentence, and its
  "what would raise the bar" table marks independent timestamps opt-in and shipped.
