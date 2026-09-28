# Deposition

**A sworn record of what your AI agent actually did.**

*A deposition is sworn testimony, recorded verbatim outside a courtroom and used
as evidence when someone later disputes what happened. That is the idea.*

Deposition records every LLM call, tool call and decision your agent makes into a
tamper-evident, hash-chained trace, then replays that run step by step so you can
see exactly what happened and why.

It is not a metrics dashboard. LangSmith, Langfuse and AgentOps already own that
ground. Deposition's wedge is **replay + tamper-evidence + root cause**: a record
that cannot be altered behind your back, and a causal chain that answers *why did
the agent do that?*

```python
import deposition

deposition.init(project="support-triage")

@deposition.record(name="triage-agent")
def run_agent(ticket):
    # OpenAI and Anthropic calls are recorded automatically - request, response,
    # token usage and latency - with no logging code in your agent.
    response = client.messages.create(model="claude-sonnet-5", messages=[...])

    for block in response.content:
        if block.type == "tool_use":
            # No caused_by needed: Deposition saw the model ask for this tool.
            with deposition.step("tool_call", tool=block.name) as body:
                body["result"] = run_tool(block.name, block.input)
```

```console
$ depo verify ./deposition/run_9f3kQ2.jsonl
OK  42 events verified for run_9f3kQ2

$ depo view ./deposition/run_9f3kQ2.jsonl
serving replay viewer on http://127.0.0.1:7878
```

## Status

Pre-release, building in the open. v0.1 is the SDK, the hash chain and the local
replay viewer. A hosted platform (team storage, search, run diffing, causal
"why" analysis, exportable audit reports) follows.

| Piece | State |
| --- | --- |
| Trace schema v0 | frozen — see [`docs/decisions/002-schema-v0.md`](docs/decisions/002-schema-v0.md) |
| Hash chain (build + verify) | done |
| Local `.jsonl` recording | done |
| CLI (`view` / `verify` / `diff`) | done |
| Local replay viewer | done |
| OpenAI / Anthropic auto-instrumentation | done (non-streaming) |
| Hosted mode | Phase 2 |

## Install

```console
pip install deposition            # SDK + the `depo` CLI
pip install 'deposition[viewer]'  # adds the local replay viewer
```

Python 3.10+. The SDK itself has **zero** required dependencies — heavy deps kill
adoption, and a recorder you cannot install is a recorder that records nothing.

## How it works

One run is one append-only event log. Each line of the `.jsonl` file is one
event, and each event's hash covers the previous event's hash:

```json
{"v":"0.1","run_id":"run_9f3kQ2","seq":14,"ts":"2026-09-27T08:14:03.412Z",
 "type":"tool_call","body":{...},"prev_hash":"9c41…","hash":"e7f2…"}
```

Edit an event, drop one, or reorder two, and `deposition verify` names the first
event where the trace stopped being trustworthy.

**What that proves, precisely.** The chain is currently unsigned, so it detects
corruption and alteration by anyone who does not hold the trace file. It does not
stop someone who does: they can edit an event and re-seal the rest. Signing and
countersigning at ingest close that gap — see
[ADR 005](docs/decisions/005-signing-and-anchoring.md). Deposition says
*tamper-evident* today, and will not say *tamper-proof* until that ships.

Any event may carry `caused_by: [seq, …]` pointing at the earlier events that
produced it. The SDK fills this in where it can — a tool call caused by the LLM
message that requested it — and the viewer walks it backwards to answer "why did
this happen?".

## Privacy

Your prompts are your data. `deposition.init(redact=...)` runs on every event
body **before** anything is hashed or written to disk:

```python
deposition.init(project="support-triage", redact=strip_pii)
```

In local mode nothing leaves your machine at all.

## Documentation

- [Trace schema v0](docs/decisions/002-schema-v0.md)
- [Architecture decisions](docs/decisions/)
- [Examples](examples/)

## License

Apache 2.0 — see [LICENSE](LICENSE).
