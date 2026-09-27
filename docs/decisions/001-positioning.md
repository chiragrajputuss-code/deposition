# 001 — Positioning: how Postflight differs

**Status:** accepted · **Date:** 2026-09-27 · **Prior-art review:** completed 2026-09-27

## The wedge

Postflight is **replay + tamper-evidence + root cause**, not monitoring.

LangSmith, Langfuse and AgentOps own the observability-metrics ground and are
better resourced than a solo developer with 8–10 hours a week. Competing on
dashboards, evals or prompt management is competing on their terms and losing.

The bet is that a different question is under-served. Not *how is my agent
performing?* but:

> *What exactly did this agent do, why did it do it, and can I show someone else
> a record of it that has not been quietly rewritten?*

## Prior art, as reviewed

### The two "Retrace" products are not the same category

**Retrace (retraceai.tech)** — the real overlap. An execution replay engine for
AI agents: records every model call, tool invocation, retrieval and error as a
span with input, output, cost and timing; lets you fork a run at any span and
re-run live from there; gives first-divergence diffs with improved / regressed /
unchanged verdicts. Python and TypeScript SDKs, hosted, closed source, five paid
tiers from free (1,000 traces) through $29 / $99 / $399 to $2,000 enterprise.

It is ahead of Postflight on replay: forking is a harder feature than viewing,
and a TypeScript SDK is a year-one non-goal here. It makes **no integrity claim
at all** — nothing about hash chains, signatures or third-party verification.

**Retrace (retracesoftware.com)** — not a competitor. A deterministic
record-replay debugger for Python generally, aimed at CI and production crashes:
records at the Python/outside-world call boundary with <0.1% overhead, replays in
VS Code over the Debug Adapter Protocol, steps backwards from a crash, and traces
any heap value through its mutation history. Open source. Different buyer,
different problem. The name collision is the only overlap, and it is a reason to
**settle the working name early**.

### AgentNotary is one of many, and the category is crowded

AgentNotary is a Python CLI that notarises, governs and audits agents with
cryptographic seals, runtime guards, EU AI Act documentation and adversarial
fuzzing — enterprise-compliance shaped.

The important finding is not AgentNotary itself. It is that **"hash-chained,
tamper-evident audit trail for AI agents" is a crowded niche as of September
2026**, not an empty one. A single curated governance list carries a dozen-plus
entries in it, most open source and most signing with Ed25519:

| Tool | Shape |
| --- | --- |
| NotaryOS | hash-chained, Ed25519-signed receipts; zero-dep Python + TS; offline verification; BUSL-1.1 |
| AgentSeal, AgentLock, writ | hash-chained signed audit trails for agent actions |
| Clay Seal, HELM, Nobulex, decision-os-min | signed, offline-verifiable decision receipts |
| Kepil, KYDE Gateway | append-only hash-chained journals behind a policy gateway |
| Provenrail, Etch | Merkle chains plus RFC 3161 timestamps, transparency logs, Bitcoin anchoring |
| agent-eval | RFC 6962 Merkle audit log, Ed25519 evidence bundles mapped to EU AI Act articles |

**A hash chain is table stakes in this niche, not a moat.** The plan's stated
differentiation — "hash-chain integrity + causal analysis + exportable audit
reports" — is one third wrong, and the ADR is being written to say so rather than
to flatter the plan.

## What actually differentiates, after the review

Sort the field on two axes and the gap is visible:

|  | no integrity claim | tamper-evident |
| --- | --- | --- |
| **full run captured, replayable** | Retrace (retraceai), Arize Phoenix, agent-replay | *(empty)* |
| **decisions only, no replay** | — | NotaryOS, AgentLock, writ, KYDE, Nobulex, … |

The receipts tools record *decisions at a policy boundary* — allow / deny /
escalate — and mostly require manual instrumentation (`notary.seal(...)`). They
do not capture the prompt, the model's actual output, the tool arguments or the
result, so they cannot replay anything and they cannot answer *why*. The replay
tools capture all of that and make no claim that the record is trustworthy.

Postflight's defensible position is the empty cell, plus two things that appear
nowhere in the field:

1. **Automatic full-fidelity capture.** `init()` patches the OpenAI and Anthropic
   clients; the user writes no logging code. Every receipts tool reviewed
   requires explicit calls at every point of interest.
2. **Observed causality.** `caused_by` edges recorded because the model asked for
   that tool by name — not inferred from adjacency. NotaryOS chains receipts
   sequentially but, in its own docs' terms, does not explain why one step caused
   another. No reviewed tool models causality at all.

That is narrower than "we have a hash chain", and it is true.

## The finding that changes the product

**Postflight's chain is unsigned, and an unsigned hash chain does not support the
claim the README makes.**

The chain detects accidental corruption and naive edits. It does not stop anyone
who controls the file: edit event 12, re-seal 12 through the end, and the result
verifies perfectly. Every serious tool in the receipts niche signs — Ed25519 is
the default, and several go further with RFC 3161 timestamps or transparency-log
inclusion proofs, precisely because a self-consistent chain proves nothing about
who wrote it or when.

So "a trace you can prove was not edited" is currently **overstated**, and the
wording has been corrected across the README and the CLI. What the chain honestly
gives today is *integrity against tampering by anyone who does not hold the
trace file* — which is exactly the hosted-ingest case, and nothing more.

Closing this is [ADR 005](005-signing-and-anchoring.md), and it should land
before any marketing copy uses the word "prove".

## Response to the obvious threats

- **Retrace (retraceai) grows fast.** Lean into integrity and causality, the two
  things it does not claim. Do not race it on forking.
- **A receipts tool adds replay.** More likely than LangSmith adding
  tamper-evidence, and the sharper threat. The defence is capture fidelity and
  zero-instrumentation onboarding, not cryptography — they will match the
  cryptography in a weekend.
- **LangSmith / Langfuse add replay.** Tamper-evidence and audit exports remain
  the moat; metrics never were.
- **The name.** Two shipped products already use "Retrace", and the governance
  niche is dense with `agent*` names. Check the working name against this list
  before buying anything.
