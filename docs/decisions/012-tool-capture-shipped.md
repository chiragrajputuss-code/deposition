# 012 — Tool capture shipped; ADR 004's Phase 4 deferral is void

**Status:** accepted · **Date:** 2026-10-16
**Supersedes:** the deferral in [ADR 004](004-auto-instrumentation.md)

## The correction

ADR 004 (2026-09-27) said: *"Only these two [OpenAI, Anthropic]. LangGraph and
CrewAI adapters are Phase 4 - adapters chase frameworks, frameworks change
faster than any small team can follow, and a broken adapter costs more trust
than a missing one."*

That reasoning was sound and the conclusion was wrong within two weeks. Without
tool-capture adapters, a framework run recorded `llm_call -> llm_call` with
nothing between them, and `caused_by` had nothing to link - the causal graph
this project rests on was empty for exactly the users most likely to try it.
`src/deposition/instrument/tools.py` (`e34350e`, `1d52d77`) now patches the one
method each framework funnels tool execution through: LangChain/LangGraph,
CrewAI, the OpenAI Agents SDK, Pydantic AI, and AutoGen.

ADR 004's warning about adapters chasing frameworks was not wrong, either - it
was a prediction, and it came true on schedule. Re-verifying against current
releases (2026-10-16) found two hook points had already moved: Pydantic AI's
`execute_tool_call` was never at risk, but LangGraph's own agent factory
(`langgraph.prebuilt.create_react_agent`, used only in this project's test
harness, not in the shipped adapter) is deprecated in 1.x and gone in 2.0. The
adapters themselves needed no change; the harness that exercises them did.

## Decision

ADR 004's envelope-patching section stands unchanged - OpenAI and Anthropic are
still patched at the resource-class level, and that remains how any framework's
*model calls* get captured for free. Its Phase 4 deferral of *tool-call*
capture is void. The standing commitment going forward:

- Tool-call hook points are re-verified against the current release of each
  supported framework on a cadence, not assumed stable, using the independent
  ground-truth harness in `deposition-compat` (stub model + a spy on the real
  tool function, counting what happened outside Deposition entirely).
- A hook point that moves is a harness fix, not a surprise, because the
  hook was never assumed permanent - see the per-framework notes in
  [`instrument/tools.py`](../../src/deposition/instrument/tools.py).
- README claims a framework is supported only as of the date it was last
  verified this way, stated plainly next to the claim.

## Consequences

- ADR 004 keeps its history as written; readers hit the Phase 4 sentence, then
  this ADR, in the order the project actually unfolded.
- The README's framework-support table links here, not to ADR 004 alone.
- Re-verification is cheap (roughly an hour across five frameworks) and should
  happen before any claim of support is repeated in a launch post or a reply to
  an outside question - a stale compatibility claim is worse than none.
