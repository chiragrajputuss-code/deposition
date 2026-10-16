# 013 — `messages.stream()` is patched; content is still not captured

**Status:** accepted · **Date:** 2026-10-16
**Amends:** one sentence in [ADR 004](004-auto-instrumentation.md)

## The correction

ADR 004 said: *"`anthropic.messages.stream()` is not patched at all: it does
not route through `create`."* That was true and is no longer: the entry point
is now patched (`instrument/_common.py`'s `wrap_stream_entrypoint`,
`instrument/anthropic.py`'s `install()`). Found while writing the "Current
limitations" section this ADR exists to make honest - a limitation someone is
about to read as current should not be stale by the time they read it.

Everything else in ADR 004's "Deliberate gaps" section stands: capturing a
streamed response's *content* still means consuming or proxying the caller's
own object, and the rule that recording must never break the host agent still
outranks completeness. That part of the gap is unchanged and is not what this
ADR is about.

## What changed

`.stream()` returns a context manager synchronously - no network I/O happens at
the point of the call, only when the caller enters it. That makes the call site
itself safe to record without any of the risk the content-capture decision is
about: the wrapper calls `describe_request`, emits one `llm_call` with the same
honest marker `.create(stream=True)` already used
(`{"streamed": true, "captured": false}`), and returns the exact object the SDK
built - never entered, never iterated, never wrapped in a proxy.

Before this, a call through `.stream()` left **no event at all** - not an
incomplete one, an absent one. That is a worse failure than the one ADR 004
accepted: an incomplete-but-marked event is the gap this project's whole
design admits to; a missing event is the kind of silent hole the chain cannot
even gesture at, because there is nothing there to be incomplete.

## Consequences

- ADR 004's own text is left as written, per house rule; this entry is the
  correction, read after it.
- The gap this closes is "no record of the call," not "no content from the
  call" - the second one is unchanged and is where `.stream()` and
  `.create(stream=True)` now behave identically.
- `completeness.status` gains a `"streamed"` value and the viewer's digest
  gains an `"uncaptured"` moment kind, so a run containing either kind of
  streamed call says so where a reader using the default view will see it,
  not only inside a body field nobody opens.
