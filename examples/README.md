# Examples

Run any of these from the repository root. Each writes a trace to `./postflight/`.

| Example | Needs | Shows |
| --- | --- | --- |
| [`01_minimal.py`](01_minimal.py) | nothing | `init` / `record` / `step`, causal links |
| [`02_tool_loop.py`](02_tool_loop.py) | nothing | a failing tool call, a retry, and the causal path to the fix |
| [`03_anthropic_agent.py`](03_anthropic_agent.py) | `anthropic`, `ANTHROPIC_API_KEY` | a real Claude call plus a tool, recorded end to end |

```console
$ python examples/02_tool_loop.py
$ postflight verify ./postflight/run_*.jsonl
OK  8 events verified for run_aB3kQ2

$ postflight view ./postflight/run_*.jsonl
postflight: serving replay viewer on http://127.0.0.1:7878
```

Then break one on purpose — edit a `body` field in the `.jsonl` by hand and run
`postflight verify` again. That failure is the product.
