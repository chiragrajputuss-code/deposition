# 011 — What we claim, and what we never claim

**Status:** accepted · **Date:** 2026-10-02
**Supersedes:** the differentiation claims in [ADR 001](001-positioning.md)

## The correction

ADR 001 (reviewed 2026-09-27) said two things "appear nowhere in the field":
automatic full-fidelity capture, and observed causality. Five days later both
statements are false, and one of them may have been false when written.

- NovaFabric ships provider-neutral, tamper-evident, replayable run capsules
  with DSSE signatures and a Merkle log (arXiv 2609.12582).
- Argument-level provenance for agent tool calls — which this project built as
  `taint.py` and briefly believed it had derived fresh — is an active area:
  ARGUS (arXiv 2605.03378) traces each tool argument to its source span,
  Agent-Sentry (arXiv 2603.22868) builds per-argument provenance graphs, and
  `taintgate` on PyPI gates tool calls on instruction provenance. The
  underlying idea is taint analysis, which is 1970s compiler security.

The lesson is general, not specific: mechanisms converge. Everyone building in
this space reasons from the same literature toward the same constructions, so
"we invented X" is a claim with a short half-life and a long cost when it
expires. A search falsified ours in under a minute.

## The policy

Every public sentence about this project — README, launch posts, docstrings,
talks — follows three rules:

1. **Show, don't claim.** A demonstration (the forged trace passing integrity
   checks and failing the signature) or a measurement (CrewAI recording 0 of 2
   before the fix; 245 mutating commands in one coding session) in place of
   every adjective. Demonstrations and measurements cannot be found in a
   competitor's search results, because they happened here.
2. **Cite the neighbours.** Where a mechanism has prior art — and it almost
   always does — the docstring or ADR names it. In a project whose product is
   honesty about records, honesty about lineage is not optional decoration;
   it is the same property.
3. **Differentiate on posture, never on mechanism.** What is defensible is not
   any component but the stance: never in the host's hot path, never a
   probabilistic judgment sealed as fact, never one green light where four
   separate claims belong, and independent of every party it records. Posture
   survives convergence; mechanisms do not.

## What this is worth saying about taint.py specifically

The provenance gates in the literature *block* at runtime. This project's
version deliberately does not: it records the provenance fact into the sealed
trace and raises it only against a mandate the operator declared, because a
guardrail that acts is a different product with different failure modes, and
this one has promised never to be the reason an agent fell over. Same
mechanism as the field; opposite posture. That sentence is the honest form of
differentiation, and the template for every future one.

## Consequences

- ADR 001's positioning grid remains useful; its "appears nowhere" sentences
  are void and must not be quoted in any copy.
- `taint.py` cites its lineage in its docstring.
- Anyone writing launch or marketing text starts from the three rules above,
  and anything that reads like an invention claim is a review failure.
