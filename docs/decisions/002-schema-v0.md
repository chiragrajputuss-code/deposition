# 002 — Trace schema v0

**Status:** frozen · **Date:** 2026-09-27 · **Supersedes:** nothing

Schema v0 is the product's foundation: the SDK writes it, the viewer renders it,
the hosted platform indexes it, and the hash chain guarantees it. It is frozen
here so that every later component can be built against a fixed target.

> **Changing anything on this page is a schema version bump**, not a bug fix.
> Bump `SCHEMA_VERSION` in `src/postflight/schema.py` and add a migration note as
> a new ADR in this directory.

## The envelope

One run is one append-only event log. Each line of a `.jsonl` trace is one event:

```json
{
  "v": "0.1",
  "run_id": "run_9f3kQ2",
  "seq": 14,
  "ts": "2026-09-27T08:14:03.412Z",
  "type": "tool_call",
  "body": { },
  "caused_by": [11],
  "prev_hash": "9c41…",
  "hash": "e7f2…"
}
```

| Field | Rule |
| --- | --- |
| `v` | schema version; always present, always first for human readability |
| `run_id` | non-empty string, identical for every event in the run |
| `seq` | starts at 0 on `run_start`, increases by exactly 1, no gaps |
| `ts` | RFC 3339 / ISO 8601 with a zone, millisecond precision |
| `type` | one of the eight types below |
| `body` | type-specific object; unknown keys allowed |
| `caused_by` | optional list of strictly earlier `seq` values |
| `prev_hash` | SHA-256 of the previous event, 64 lowercase hex |
| `hash` | SHA-256 of this event including `prev_hash` |

Unknown **envelope** fields are rejected — the envelope is the hashed surface, so
a stray field there is a schema break. Unknown **body** fields are allowed, so
instrumentation can move faster than the schema.

Optional fields that are unset are **omitted**, never written as `null`. A field
that is present changes the hash, so "absent" and "null" must not be confusable.

## Event types

| Type | Body carries |
| --- | --- |
| `run_start` | agent name, SDK version, env fingerprint, config snapshot |
| `llm_call` | provider, model, request params, messages in, response out, token usage, latency, cost |
| `tool_call` | tool name, arguments, result or error, latency |
| `decision` | label + the LLM output span it derives from (`caused_by`) |
| `retrieval` | query, source, doc ids returned |
| `error` | exception class, message, stack summary, `caused_by` |
| `annotation` | free-form note (user- or SDK-added) |
| `run_end` | status, totals (steps, tokens, cost, duration) |

## Hash chain rules

1. **Canonical JSON** is the hash input: `json.dumps(sort_keys=True,
   separators=(",", ":"), ensure_ascii=False, allow_nan=False)`, encoded UTF-8.
   Two events that are equal must serialise to identical bytes regardless of how
   they were built. This byte-level contract *is* the guarantee.
2. An event's `hash` is `SHA-256(canonical_json(event without "hash"))`.
   `prev_hash` is inside that input — that is what links the chain.
3. `run_start` is `seq` 0 and its `prev_hash` is the **genesis hash**, 64 zeros.
   Event `seq` 1 therefore carries the hash of the `run_start` header.
4. Verification replays the chain from the genesis hash and fails loudly on any
   gap, edit or reorder, naming the **first** event where the trace stops being
   trustworthy. One clear verdict beats a wall of downstream noise.
5. `postflight verify run.jsonl` is a first-class command with a contract exit
   code: `0` verified, `1` broken, `2` could not run.

### What the chain does and does not prove

It proves **nothing was changed, removed from the middle, or reordered**.

It does **not** prove nothing was removed from the end — truncating a trace
leaves a shorter, still-valid chain. `run_end` is what closes that gap: a trace
without one is visibly unfinished. Signing the head hash externally would close
it fully; that is a later decision, not a v0 one. Say this plainly in the docs
rather than overselling the guarantee.

## Causality

Any event may carry `caused_by: [seq, …]` pointing at strictly earlier events.
The SDK fills it in where it can — a tool call caused by the LLM message that
requested it — and the viewer walks it backwards to answer "why did this
happen?".

This field is the differentiator, so it is **required design, not
nice-to-have**. A forward reference is rejected at validation: causality only
runs backwards, and a forward-pointing edge is either a bug or a forgery.

## Payload handling

Bodies over **64 KB** are stored as content-addressed blobs beside the log at
`blobs/<sha256>` and referenced as `{"$blob": "<sha256>", "size": n,
"encoding": "utf-8"}`. This keeps logs scannable and de-duplicates the repeated
context that dominates agent runs.

Externalisation happens **before** the event is sealed: the hash covers the
reference that is actually stored, not the payload it stands in for.

A `redact` hook runs on every body before anything is hashed or written. Users
will send PII in prompts whether we like it or not; the tool and the warning both
belong in the product.

## OpenTelemetry alignment

Field names mirror the OTel GenAI semantic conventions wherever they overlap
(`gen_ai.request.model`, token-usage attributes) so an OTel bridge stays cheap.
Postflight does not adopt the OTel data model itself: spans are a tree of
durations, and a trace here is an ordered, hash-linked log. The two are not the
same shape, and pretending otherwise would cost the chain.
