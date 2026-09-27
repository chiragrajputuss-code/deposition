# Postflight

**A flight recorder for AI agents.**

Postflight records every LLM call, tool call and decision your agent makes into a
tamper-evident, hash-chained trace, then replays that run step by step so you can
see exactly what happened and why.

It is not a metrics dashboard. LangSmith, Langfuse and AgentOps already own that
ground. Postflight's wedge is **replay + tamper-evidence + root cause**: a trace
you can prove was not edited, and a causal chain that answers *why did the agent
do that?*

```python
import postflight

postflight.init(project="support-triage")

@postflight.record(name="triage-agent")
def run_agent(ticket):
    ...

with postflight.step("decision", label="issue refund", caused_by=[13]):
    ...
```

```console
$ postflight verify ./postflight/run_9f3kQ2.jsonl
OK  42 events verified for run_9f3kQ2

$ postflight view ./postflight/run_9f3kQ2.jsonl
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
| OpenAI / Anthropic auto-instrumentation | in progress |
| Hosted mode | Phase 2 |

## Install

```console
pip install postflight            # SDK + CLI
pip install 'postflight[viewer]'  # adds the local replay viewer
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

Edit an event, drop one, or reorder two, and `postflight verify` names the first
event where the trace stopped being trustworthy. That is the whole point: a trace
is evidence, not a log file.

Any event may carry `caused_by: [seq, …]` pointing at the earlier events that
produced it. The SDK fills this in where it can — a tool call caused by the LLM
message that requested it — and the viewer walks it backwards to answer "why did
this happen?".

## Privacy

Your prompts are your data. `postflight.init(redact=...)` runs on every event
body **before** anything is hashed or written to disk:

```python
postflight.init(project="support-triage", redact=strip_pii)
```

In local mode nothing leaves your machine at all.

## Documentation

- [Trace schema v0](docs/decisions/002-schema-v0.md)
- [Architecture decisions](docs/decisions/)
- [Examples](examples/)

## License

Apache 2.0 — see [LICENSE](LICENSE).
