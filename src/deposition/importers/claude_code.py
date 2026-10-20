"""Turn a Claude Code session transcript into a Deposition trace.

Claude Code already records what it did - every model turn, every Bash command,
every file write - as JSONL under ``~/.claude/projects/<project>/<session>.jsonl``.
What it does not give you is a record anyone can check: the file is plain,
mutable JSON, so "the agent ran this command against the repo" is an assertion
by whoever holds the file.

:func:`convert` reads that transcript and rebuilds it as a hash-chained
Deposition trace, so it can be verified, signed, and read in the existing
viewer - via ``deposition import claude-code``. :func:`tail_once` does the same
thing repeatedly as the source grows, for ``deposition tail claude-code``.

**The honest grade.** This is an importer, not a recorder: nothing here runs at
the moment a tool call happens, and the source file was plain and mutable right
up until the instant it was read. The chain it produces attests "unchanged
since the last import" - a window of seconds for ``tail``, however long the
session sat on disk first for a one-shot ``import``. An evidence-grade version
records through Claude Code's own hooks, at the moment of each call, and is a
separate piece of work this module does not attempt.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..chain import ChainBuilder
from ..schema import canonical_json

__all__ = [
    "DEFAULT_CLAUDE_CODE_ROOT",
    "MUTATING",
    "TranscriptError",
    "convert",
    "discover_latest_session",
    "guess_project_dir",
    "read_records",
    "tail_once",
]

#: Where Claude Code keeps every project's session transcripts.
DEFAULT_CLAUDE_CODE_ROOT = Path.home() / ".claude" / "projects"

#: Tools that change something outside the agent. Recorded like any other tool,
#: but flagged, because these are the events an audit is actually about.
MUTATING = frozenset({"Bash", "Write", "Edit", "NotebookEdit"})

#: The stable pointer :func:`tail_once` keeps updated, so a viewer can point at
#: one path once and follow whatever session is newest from then on.
CURRENT_POINTER = "current.jsonl"


class TranscriptError(Exception):
    """A transcript could not be read or contained nothing to import."""


# -- reading the transcript ---------------------------------------------------


def read_records(path: Path) -> list[dict[str, Any]]:
    """Parse a transcript's JSONL, skipping lines that are not valid JSON.

    A half-written line at the end of a file still being appended to is not an
    error here - ``tail_once`` reads a file that may be mid-write, and the
    event that line would have become simply is not imported yet.
    """
    records = []
    for line in Path(path).read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def _tokens(usage: dict[str, Any]) -> int:
    """Fresh tokens for one turn: input + output + cache writes.

    Cache *reads* are deliberately excluded. The same cached prefix is re-read
    on every turn, so summing ``cache_read_input_tokens`` across a long session
    counts the same context hundreds of times - measured on four real
    sessions of 800-5,500 API calls, it overstated token usage by 24-50x,
    growing with session length. A trace that
    reports a number nobody can reconcile is worse than one that reports less.
    """
    if not isinstance(usage, dict):
        return 0
    return sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")
    )


def _context_tokens(usage: dict[str, Any]) -> int:
    """What the model actually saw this turn: fresh input plus every cached
    token re-read. This is the context window people never see, and - paired
    with the delta from the previous turn - names the turn that grew it."""
    if not isinstance(usage, dict):
        return 0
    return sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )


def _merge_responses(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One entry per API response, keyed by ``message.id``.

    Claude Code writes a separate record for each content block of a response
    (thinking, text, each tool_use), and every one repeats the response's full
    ``usage``. Imported record by record, one response became two or three
    model turns and its tokens were counted two or three times. Later records
    carry the final usage and stop_reason, since earlier ones were written
    mid-stream, so the last value wins.
    """
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        response_id = message.get("id")
        if not response_id:
            continue
        entry = merged.setdefault(
            response_id, {"content": [], "usage": {}, "stop_reason": None, "tool_ids": set()}
        )
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("id") in entry["tool_ids"]:
                    continue
                entry["tool_ids"].add(block.get("id"))
            entry["content"].append(block)
        if message.get("usage"):
            entry["usage"] = message["usage"]
        if message.get("stop_reason"):
            entry["stop_reason"] = message["stop_reason"]
    return merged


# -- conversion ----------------------------------------------------------------


def convert(path: str | Path, out_dir: str | Path) -> Path:
    """Rebuild one Claude Code session transcript as a hash-chained trace.

    Raises :class:`TranscriptError` rather than writing a partial trace if the
    transcript has nothing recognisable as a model turn in it.
    """
    path = Path(path)
    records = read_records(path)

    # Tool results arrive in a later record, keyed by tool_use_id. Index them
    # first so each tool call can be written with its own outcome attached.
    results: dict[str, dict[str, Any]] = {}
    for record in records:
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[block.get("tool_use_id")] = block

    turns = [r for r in records if r.get("type") in ("assistant", "user")]
    if not turns:
        raise TranscriptError(f"no model turns found in {path}")

    responses = _merge_responses(records)
    emitted: set[str] = set()

    first = turns[0]
    run_id = "run_" + (first.get("sessionId") or path.stem)[:6]
    builder = ChainBuilder(run_id)
    events: list[dict[str, Any]] = []

    events.append(
        builder.append(
            "run_start",
            {
                "agent": "claude-code",
                "project": Path(first.get("cwd") or ".").name,
                "session_id": first.get("sessionId"),
                "config": {
                    "entrypoint": first.get("entrypoint"),
                    "git_branch": first.get("gitBranch"),
                    "cli_version": first.get("version"),
                    "cwd": first.get("cwd"),
                },
                "imported_from": str(path),
            },
            ts=first.get("timestamp"),
        )
    )

    #: tool_use_id -> seq of the model turn that requested it, for causality.
    requested: dict[str, int] = {}
    totals = {"tokens": 0, "mutating": 0, "tools": 0}
    #: Context seen by the previous model turn, for per-turn growth deltas.
    previous_context = 0

    for record in records:
        kind = record.get("type")
        message = record.get("message") or {}
        ts = record.get("timestamp")
        content = message.get("content")

        if kind == "assistant":
            usage = message.get("usage") or {}
            stop_reason = message.get("stop_reason")
            response_id = message.get("id")
            if response_id in responses:
                if response_id in emitted:
                    continue
                emitted.add(response_id)
                merged = responses[response_id]
                content, usage, stop_reason = (
                    merged["content"],
                    merged["usage"],
                    merged["stop_reason"],
                )
            tool_uses = [
                b for b in (content or []) if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            tokens = _tokens(usage)
            totals["tokens"] += tokens
            context = _context_tokens(usage)
            sealed = builder.append(
                "llm_call",
                {
                    "context_tokens": context,
                    "context_delta": (context - previous_context) if previous_context else 0,
                    "provider": "anthropic",
                    "gen_ai.request.model": message.get("model"),
                    "response": {
                        "text": _text(content)[:4000],
                        "tool_calls": [
                            {"name": b.get("name"), "id": b.get("id")} for b in tool_uses
                        ],
                    },
                    "usage": {
                        "total_tokens": tokens,
                        "detail": {
                            k: usage.get(k)
                            for k in (
                                "input_tokens",
                                "output_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                            )
                            if usage.get(k) is not None
                        },
                    },
                    "stop_reason": stop_reason,
                },
                ts=ts,
            )
            events.append(sealed)
            if context:
                previous_context = context
            for block in tool_uses:
                requested[block.get("id")] = sealed["seq"]

            # Each tool the turn asked for, with the result that came back.
            for block in tool_uses:
                outcome = results.get(block.get("id")) or {}
                body: dict[str, Any] = {
                    "tool": block.get("name"),
                    "arguments": block.get("input") or {},
                    "tool_use_id": block.get("id"),
                }
                if outcome.get("is_error"):
                    body["error"] = {"message": _text(outcome.get("content"))[:2000]}
                else:
                    body["result"] = _text(outcome.get("content"))[:4000] or outcome.get("content")
                if block.get("name") in MUTATING:
                    body["mutates_environment"] = True
                    totals["mutating"] += 1
                totals["tools"] += 1
                events.append(
                    builder.append("tool_call", body, caused_by=[requested[block["id"]]], ts=ts)
                )

        elif kind == "user" and isinstance(content, str) and content.strip():
            # A human instruction. Recorded as an annotation so the trace shows
            # what the agent was asked, not only what it did.
            events.append(
                builder.append("annotation", {"note": content[:2000], "author": "user"}, ts=ts)
            )

    events.append(
        builder.append(
            "run_end",
            {
                "status": "ok",
                "totals": {
                    "steps": len(events) - 1,
                    "tokens": totals["tokens"],
                    "tool_calls": totals["tools"],
                    "mutating_tool_calls": totals["mutating"],
                    "dropped": 0,
                    "adopted": 0,
                },
            },
            ts=records[-1].get("timestamp"),
        )
    )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{run_id}.jsonl"
    target.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    return target


# -- finding a session without being told where it is --------------------------


def guess_project_dir(cwd: str | Path | None = None, *, root: Path | None = None) -> Path:
    """Where Claude Code *probably* keeps sessions for ``cwd``.

    Claude Code slugifies the absolute working directory into a folder name
    under ``~/.claude/projects`` by replacing every non-alphanumeric character
    with a hyphen - confirmed against real session directories, including
    paths containing spaces and underscores, not merely the common ``/`` case.

    That slugification is an internal detail Claude Code has never promised
    stable, so this guess is a fast path, never the only path - see
    :func:`discover_latest_session`, which verifies or falls back rather than
    trusting it blindly. (This project has already been burned once, in
    ADR 012, by trusting an interface nobody had promised to keep still.)
    """
    root = root if root is not None else DEFAULT_CLAUDE_CODE_ROOT
    resolved = str(Path(cwd or Path.cwd()).resolve())
    slug = re.sub(r"[^A-Za-z0-9]", "-", resolved)
    return root / slug


def _session_cwd(session: Path) -> str | None:
    """The ``cwd`` field Claude Code itself stamped on a session's first record.

    This is ground truth, unlike the slug: Claude Code wrote this value down
    about itself, rather than us reverse-engineering a naming scheme.
    """
    try:
        lines = session.read_text("utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = record.get("cwd")
        if isinstance(cwd, str):
            return cwd
    return None


def _newest_session(directory: Path) -> Path | None:
    sessions = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return sessions[-1] if sessions else None


def discover_latest_session(
    project_dir: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    root: Path | None = None,
) -> Path | None:
    """The newest Claude Code session transcript for ``cwd`` (default: the
    current directory), or ``None`` if none can be found.

    Three attempts, in order, each one cheaper to get wrong than the last:

    1. ``project_dir``, if given explicitly - trust the caller.
    2. The guessed slug directory (:func:`guess_project_dir`) - the common
       case, and free if it is right.
    3. Every directory under ``root`` whose newest session's own ``cwd`` field
       matches - slower, but depends on nothing Claude Code has not written
       down about itself.
    """
    root = root if root is not None else DEFAULT_CLAUDE_CODE_ROOT
    cwd = str(Path(cwd or Path.cwd()).resolve())

    if project_dir is not None:
        return _newest_session(Path(project_dir))

    guessed = guess_project_dir(cwd, root=root)
    if guessed.is_dir():
        found = _newest_session(guessed)
        if found is not None:
            return found

    if not root.is_dir():
        return None
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        session = _newest_session(directory)
        if session is not None and _session_cwd(session) == cwd:
            return session
    return None


# -- following a session as it grows -------------------------------------------


def tail_once(
    out_dir: str | Path,
    *,
    project_dir: str | Path | None = None,
    cwd: str | Path | None = None,
    root: Path | None = None,
    state: dict[str, Any],
) -> Path | None:
    """One polling iteration: reconvert the newest session if it changed.

    ``state`` is a plain dict the *caller* owns and passes back in on every
    call - never module-level mutable state, which would make two tail loops
    in one process (two tests, or a viewer and a script) step on each other.

    Returns the freshly written trace if it reconverted - because the session
    grew, or because a different (newer) session appeared - or ``None`` if
    nothing changed, or no session can be found at all.
    """
    transcript = discover_latest_session(project_dir, cwd=cwd, root=root)
    if transcript is None:
        return None

    size = transcript.stat().st_size
    if state.get("transcript") == str(transcript) and state.get("size") == size:
        return None
    state["transcript"] = str(transcript)
    state["size"] = size

    target = convert(transcript, out_dir)
    # A stable name, whatever session is newest: a viewer points here once and
    # follows the user across brand new chats without being restarted.
    shutil.copy2(target, Path(out_dir) / CURRENT_POINTER)
    return target
