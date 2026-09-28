# 003 — The SDK has no required dependencies

**Status:** accepted · **Date:** 2026-09-27

## Decision

`pip install deposition` pulls in **nothing**. The core SDK — schema, hash chain,
recording, local `.jsonl` export, CLI — is standard library only.

Everything heavier is an extra:

| Extra | Brings | For |
| --- | --- | --- |
| `viewer` | FastAPI, uvicorn | `deposition view` |
| `otel` | OpenTelemetry SDK | the OTel bridge (Phase 4) |
| `dev` | pytest, ruff, pip-audit, httpx | contributors |

## Why

A recorder is infrastructure that sits inside someone else's agent. Every
required dependency is a version conflict waiting to happen in an environment we
do not control, and a conflict at install time means the trace is never recorded
at all. Adoption is the whole game for the open-source wedge; a recorder nobody
can install records nothing.

It also keeps the trust surface small. The hash chain is the product's
credibility, and it is worth being able to say that computing it involves
`hashlib`, `json` and nothing else.

## Consequences

- `src/deposition/schema.py` and `chain.py` must never import outside the stdlib.
  This is a review rule, not a preference.
- The viewer's UI is a **pre-built** static bundle shipped inside the wheel: no
  build step at runtime, no CDN request at page load. `deposition view run.jsonl`
  works offline with zero setup.
- `deposition view` without the extra fails with an instruction, not a traceback.
- Anything tempting us to add a required dependency (pydantic for validation, a
  HTTP client for hosted mode) gets written by hand instead. Validation is ~120
  lines; hosted upload will use `urllib`.
