# Architecture decisions

One short file per irreversible choice: what was decided, why, and what it costs.
Read these at the start of a session — they are how context survives fragmented
evenings.

| # | Decision | Status |
| --- | --- | --- |
| [001](001-positioning.md) | Positioning: how Deposition differs | accepted |
| [002](002-schema-v0.md) | Trace schema v0 | **frozen** |
| [003](003-zero-dependency-sdk.md) | The SDK has no required dependencies | accepted |
| [004](004-auto-instrumentation.md) | Auto-instrumentation scope and causality | accepted |
| [005](005-signing-and-anchoring.md) | Signing and anchoring the chain | accepted; layer 1 shipped, 2–3 pending |
| [006](006-otel-attribute-mapping.md) | OpenTelemetry GenAI attribute mapping | accepted |
| [007](007-the-name.md) | The name | accepted |
| [008](008-canonical-json.md) | Canonical JSON moves to RFC 8785; schema v1 | accepted; shipped |
| [009](009-interlocking-traces.md) | Interlocking traces: receipts between agents | **proposed** |
| [010](010-mandates.md) | Mandates: record what the run was allowed to do | accepted |

New ADRs are numbered sequentially and never edited after they are accepted —
supersede them with a new one instead, so the reasoning stays readable in order.
