# Deposition — SDK repo

A verbatim record of an AI agent run: an append-only, hash-chained trace of every LLM
call, tool call and decision, plus a local replay viewer. Open source, Apache 2.0.
The hosted platform lives in the private `deposition-cloud` repo.

**Read `docs/decisions/` at the start of every session.** Schema v0 is frozen in
[`docs/decisions/002-schema-v0.md`](docs/decisions/002-schema-v0.md).

## Stack

Python 3.10+, standard library only for the core. FastAPI + uvicorn behind the
`viewer` extra. Hatchling build, pytest, ruff. See
[`003-zero-dependency-sdk.md`](docs/decisions/003-zero-dependency-sdk.md) —
adding a required dependency needs a new ADR.

## Layout

```
src/deposition/
  __init__.py      init(), record decorator, step context manager, Recorder/Run
  schema.py        trace schema v0: dataclasses, canonical JSON, validation
  chain.py         hash-chain build + verify
  signing.py       Ed25519 signatures over the head hash ([signing] extra)
  mandate.py       audit a run against the authority recorded in run_start
  taint.py         argument provenance: which tool values came from fetched content
  instrument/      auto-patching: openai.py, anthropic.py
  exporters/       jsonl.py (local), http.py (hosted, Phase 2), otel.py (Phase 4)
  viewer/          FastAPI app + pre-built static UI
  cli.py           deposition view / verify / audit / diff / keygen
tests/             chain + schema tests are non-negotiable
examples/          3 runnable example agents
docs/decisions/    ADRs
```

## Commands

```bash
uv venv --python 3.12 .venv                      # first time
uv pip install --python .venv/bin/python -e '.[dev,viewer]'   # dev pulls in signing too

.venv/bin/python -m pytest                       # all tests
.venv/bin/python -m pytest tests/test_chain.py   # the ones that matter most
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/pip-audit

.venv/bin/python examples/01_minimal.py          # writes ./deposition/run_*.jsonl
.venv/bin/deposition verify ./deposition/run_*.jsonl
.venv/bin/deposition view ./deposition/run_*.jsonl
```

## Standing rules

- **Every change to `chain.py`, `schema.py` or `signing.py` ships with new or
  updated tests in the same commit.** No exceptions. These three files are the
  product's credibility.
- **Never report a signature as verified without a pinned key.** A sidecar
  carries its own public key; whoever rewrites a trace can re-sign it. See the
  implementation note in ADR 005.
- **Token plaintext never touches logs, error messages or the database.**
- **Schema changes bump the `v` field** and add a migration note as a new ADR in
  `docs/decisions/`.
- **Deposition failing must never fail the user's app.** Every public entry point
  is wrapped. The worst case is a missing trace, never a broken agent. Note the
  cost: a swallowed exception hides real bugs, so anything inside a
  `contextlib.suppress` needs a test that proves the happy path actually ran.
- **`schema.py` and `chain.py` import nothing outside the standard library.**
  `signing.py` is the single exception, and imports `cryptography` lazily so the
  module stays importable without the extra.
- Never leave a session with a half-broken repo: end with tests green and a
  commit.
- **No copy claims novelty, anywhere.** Show a demonstration or a measurement;
  cite the prior art; differentiate on posture, never on mechanism. A search
  falsified our last invention claim in under a minute - ADR 011 is the policy.

## Conventions

- Comments explain *why*, never *what*. If a line needs a comment to say what it
  does, rewrite the line.
- Test names are sentences describing the behaviour under test
  (`test_editing_an_event_body_is_detected`), not the function called.
- Public API surface stays small — see the docstring at the top of `__init__.py`.
  Adding to it is a decision, not a convenience.
- Plan first: describe the feature, agree the approach, then implement.
  Correcting a plan costs minutes; correcting a wrong implementation costs an
  evening.
