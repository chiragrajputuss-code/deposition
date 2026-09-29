# Examples

Run any of these from the repository root. Each writes a trace to `./deposition/`.
Everything except `03` runs with **no API key and no network** — the model calls
are stand-ins so the scenarios stay runnable, free and deterministic. Against a
real OpenAI or Anthropic client, `deposition.init()` records those calls on its
own and no logging code changes.

## Start here

| Example | Needs | Shows |
| --- | --- | --- |
| [`01_minimal.py`](01_minimal.py) | nothing | `init` / `record` / `step`, causal links |
| [`02_tool_loop.py`](02_tool_loop.py) | nothing | a failing tool call, a retry, and the causal path to the fix |
| [`03_anthropic_agent.py`](03_anthropic_agent.py) | `anthropic`, `ANTHROPIC_API_KEY` | a real Claude call plus a tool, recorded automatically |

## Cases where the record is the point

Each of these is a situation where someone later disputes what happened, or where
a failure has to be reconstructed after the fact — and where a summary log cannot
answer the question.

| Example | The question it answers |
| --- | --- |
| [`04_refund_dispute.py`](04_refund_dispute.py) | A chargeback lands six weeks on. What did the agent approve, under which policy version, and can the record survive its own holder? **Signs the trace** (`[signing]` extra). |
| [`05_loan_adverse_action.py`](05_loan_adverse_action.py) | A credit denial needs specific reasons, a year later, after the model has been retired. Which inputs drove it? |
| [`06_clinical_triage.py`](06_clinical_triage.py) | A red flag appeared at turn 1 and escalation fired at turn 2. What did the agent do in between? |
| [`07_rag_contract_qa.py`](07_rag_contract_qa.py) | The answer was wrong — did the model hallucinate, or did retrieval hand it a superseded document? Also exercises **blob externalisation**: the 150KB passage lands in `./deposition/blobs/` with a `$blob` reference in the event. |
| [`08_prompt_injection.py`](08_prompt_injection.py) | A fetched page contained instructions to the agent and it obeyed. Where did the exfiltration instruction enter? |
| [`09_multi_agent_handoff.py`](09_multi_agent_handoff.py) | Planner → researcher → writer. Which one introduced the error? (It was the handoff.) |
| [`10_code_change_agent.py`](10_code_change_agent.py) | An autonomous PR broke production. Which tests did the agent choose not to run, and did anyone approve? |
| [`11_model_fallback_diff.py`](11_model_fallback_diff.py) | The provider failed and the agent fell back to a cheaper model. Two runs, one `deposition diff`. |
| [`12_gdpr_erasure.py`](12_gdpr_erasure.py) | Prove what was deleted and what was retained under exemption — when the database can no longer tell you. |
| [`13_pii_redaction.py`](13_pii_redaction.py) | Record an agent that handles card numbers and medical ids **without** recording them. `redact` runs before anything is hashed or written. |

## The thing to actually try

```console
$ python examples/02_tool_loop.py
$ deposition verify ./deposition/run_*.jsonl
OK  7 events verified for run_aB3kQ2
  -- signature: unsigned (no .sig file beside this trace)

$ deposition view ./deposition/run_*.jsonl
```

Then break one on purpose — edit a `body` field in the `.jsonl` by hand and run
`deposition verify` again. That failure is the product.

For the harder version, run `04_refund_dispute.py`, edit an event, and re-seal
the whole chain yourself with `ChainBuilder`. The chain will verify perfectly and
the signature will not:

```console
$ deposition verify ./deposition/run_*.jsonl --pubkey ./deposition/refunds.pem.pub
OK  6 events verified for run_itkWaU
  !! signature: signature covers head 9ce059683c05... but this trace ends at
     e36ebb6fc31b... - the trace was rewritten after it was signed
```

## A note on what these do not show

None of these measure how an agent is *performing*. There is no aggregation across
runs anywhere in the SDK — no latency percentiles, no cost trend, no failure rate,
no quality scoring. One trace at a time, or two compared with `diff`. Deposition
answers "what happened in this run, and is this record real?", which is a
different question from "is my agent getting better?".
