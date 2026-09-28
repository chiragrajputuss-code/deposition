# 006 — OpenTelemetry GenAI attribute mapping

**Status:** accepted · **Date:** 2026-09-27

Schema v0 claims to mirror the OTel GenAI semantic conventions "wherever they
overlap". This ADR records the actual mapping, checked against the conventions as
published in the `open-telemetry/semantic-conventions-genai` repository, so the
Phase 4 bridge is a lookup table rather than a research project.

## Where we already match

`gen_ai.request.model` is used verbatim in `llm_call` bodies, and
`gen_ai.response.model` was added by auto-instrumentation.

## Where we deliberately differ

The conventions are **span attributes** — a flat key space on a tree of
durations. A Deposition event is an ordered, hash-linked log entry with a nested
`body`. Flattening every attribute to its dotted OTel name would make bodies
unreadable and the chain no cheaper to verify, so the mapping stays a mapping.

| Deposition (`llm_call` body) | OTel attribute | Note |
| --- | --- | --- |
| `provider` | `gen_ai.provider.name` | Required in OTel |
| `gen_ai.request.model` | `gen_ai.request.model` | identical |
| `gen_ai.response.model` | `gen_ai.response.model` | identical |
| `response_id` | `gen_ai.response.id` | |
| `stop_reason` | `gen_ai.response.finish_reasons` | OTel takes an array |
| `messages` | `gen_ai.input.messages` | Opt-In in OTel; always recorded here |
| `response` | `gen_ai.output.messages` | Opt-In in OTel; always recorded here |
| `system` / `instructions` | `gen_ai.system_instructions` | Opt-In in OTel |
| `tools` | `gen_ai.tool.definitions` | we record names only, not schemas |
| `usage.input_tokens` | `gen_ai.usage.input_tokens` | |
| `usage.output_tokens` | `gen_ai.usage.output_tokens` | |
| `usage.total_tokens` | — | **no OTel equivalent**; we derive it |
| `usage.cache_read_input_tokens` | `gen_ai.usage.cache_read.input_tokens` | |
| `usage.cache_creation_input_tokens` | `gen_ai.usage.cache_write.input_tokens` | |
| `request.temperature` | `gen_ai.request.temperature` | |
| `request.top_p` / `top_k` | `gen_ai.request.top_p` / `top_k` | |
| `request.max_tokens` | `gen_ai.request.max_tokens` | |
| `request.seed` | `gen_ai.request.seed` | |
| `request.stop_sequences` | `gen_ai.request.stop_sequences` | |
| `streamed` | `gen_ai.request.stream` | |
| `latency_ms` | span duration | OTel has no attribute for it |
| `error.exception` | `error.type` | |
| `retrieval` body: `query` | `gen_ai.retrieval.query.text` | Opt-In |
| `retrieval` body: `doc_ids` | `gen_ai.retrieval.documents` | Opt-In |

## What OTel has no place for

`caused_by`, `prev_hash`, `hash` and `seq`. Causality and integrity are exactly
what Deposition adds, and there is nowhere in the conventions to put them — an
OTel export is therefore **lossy by definition**. The bridge exports what maps
and drops the rest; it is a convenience for people who already run a collector,
never the canonical record.

Note also that OTel marks messages, system instructions and tool definitions
**Opt-In**, because they are the expensive and sensitive parts. Deposition records
them by default — they are the whole point of a replay tool — which is why the
`redact` hook is documented as prominently as it is.

## Consequences

- `exporters/otel.py` implements this table and nothing cleverer.
- Adding a field to an `llm_call` body means adding a row here or writing down
  why there is no equivalent.
- If the conventions move to stable and rename something, that is a bridge
  change, never a schema change. Schema v0 does not follow OTel's version.
