# 004 — Auto-instrumentation scope and causality

**Status:** accepted · **Date:** 2026-09-27

## What is patched

`deposition.init()` patches the **resource classes**, not client instances:

| Provider | Patched |
| --- | --- |
| OpenAI | `chat.completions.create`, `responses.create` (sync + async) |
| Anthropic | `messages.create` (sync + async) |

Patching the class covers clients created *before* `init()`, which is what people
actually write — the client is usually a module-level global. `shutdown()` puts
every original method back.

Only these two. LangGraph and CrewAI adapters are Phase 4: adapters chase
frameworks, frameworks change faster than any small team can follow, and a
broken adapter costs more trust than a missing one.

## Causality is observed, never inferred

The tempting heuristic is to link every `tool_call` to whichever `llm_call` came
before it. **We do not do that.**

Instead, when a recorded response contains tool requests (`tool_use` blocks for
Anthropic, `tool_calls` for OpenAI), the requested tool *names* are remembered
against that event's `seq`. A later `tool_call` event with a matching `tool` name
and no explicit `caused_by` gets the edge. Anything unmatched gets **no edge at
all**.

The reason is the product, not the code. Deposition's claim is that a trace is
evidence. An audit report cannot present "the model asked for this tool by name"
and "a tool ran shortly afterwards" as the same kind of fact. A sparse causal
graph that is true beats a complete one that is guessed.

An explicit `caused_by` always wins over the automatic edge.

## Deliberate gaps in v0

**Streaming responses are not captured.** A streamed call records its request and
`response: {"streamed": true, "captured": false}`. Capturing the content would
mean consuming or proxying the caller's stream object, and the top promise —
never break the host agent — outranks completeness. `anthropic.messages.stream()`
is not patched at all: it does not route through `create`.

**Cost is not computed.** The body has a `cost_usd` field and
auto-instrumentation leaves it empty. Pricing tables go stale silently, and a
confidently wrong number in an audit report is worse than a blank. Token counts
are recorded; cost belongs where prices can be kept current — the platform, or
the user's own `redact`-style hook.

**Positional arguments are not captured.** Both SDKs' `create` methods are
keyword-only, so this costs nothing today. It would break silently if that
changed, which is a reason to keep the adapters tested against the real response
models.

## Failure behaviour

- Provider not installed, or its internals moved → instrumentation stays off.
  No warning, no error. `install()` returns what it actually patched.
- Anything raised while building the event → swallowed; the call proceeds.
- The provider's exception → recorded on the `llm_call` event, then re-raised
  unchanged.
- The provider's return value → handed back untouched.

Responses are pydantic models, so everything goes through `jsonable()` before it
reaches the chain: unknown objects become their `str()`, and non-finite floats
become strings, because canonical JSON forbids `NaN` and one bad float must not
be able to drop a whole event.
