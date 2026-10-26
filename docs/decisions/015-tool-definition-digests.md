# 015 — Tool definitions are sealed as digests

**Status:** accepted · **Date:** 2026-10-26
**Raised by:** public feedback - "a way to prove the tool definitions didn't change
between runs... that's the kind of thing a security team will ask about first"
**Adds a row to:** the mapping in [ADR 006](006-otel-attribute-mapping.md)

## The problem

`llm_call.body.tools` recorded only tool **names**. The definitions the model was
actually shown - name, description, input schema - were in `kwargs["tools"]` at
call time and were thrown away. So two runs could look identical in a trace while
the model had been told something different about what `refund_customer` does, and
a definition swapped mid-run left no mark at all.

## Decision

Both provider adapters record, beside the existing `tools` list of names:

```json
"tool_digests": {"refund_customer": "<sha256 hex>", "lookup_order": "<sha256 hex>"}
```

Each digest is SHA-256 over the RFC 8785 canonical JSON
([ADR 008](008-canonical-json.md)) of the definition **exactly as sent to the
provider**, so it reproduces in any language.

- **Digests, not full text.** A digest detects any change, keeps traces small, and
  avoids copying long schemas into every `llm_call`. Recording full definitions
  can be added later as an option; nothing here prevents it.
- **No schema bump.** ADR 002 allows unknown body keys ("so instrumentation can
  move faster than the schema"), and `tool_digests` is one.
- **Never breaks the host.** A definition that cannot be hashed reproducibly -
  serialisation fails, or the only serialisation embeds a memory address that
  changes every run - records `"<unhashable>"` for that tool and the rest are
  still hashed.
- `depo diff` names tools added, removed, or whose digest changed, and no longer
  reports those as an opaque "body differs".
- `depo audit` prints a `!!` line when one tool name carries two digests within a
  run. Informational only: it is not a mandate violation, because the mandate
  vocabulary (ADR 010) says nothing about definitions yet.

## OTel mapping (the row ADR 006 asks for)

| Deposition (`llm_call` body) | OTel attribute | Note |
| --- | --- | --- |
| `tool_digests` | - | **no OTel equivalent**; `gen_ai.tool.definitions` carries full definitions (Opt-In), not digests |

## What it does not cover

Definitions that never pass through an instrumented provider SDK - a framework
with its own model client, or raw HTTP - are not sealed. Model calls there are
not captured either, so the gap is the same one the README's tool-capture section
already states.

## Proposed, not built: pinning approved definitions

A mandate key such as `allowed_tool_digests: {"refund_customer": "<sha256>"}`
would let `audit` report a run whose model was shown any other definition as a
violation. The mandate vocabulary grows only by ADR (ADR 010), so this is recorded
here as a proposal and waits for someone to ask for it.
