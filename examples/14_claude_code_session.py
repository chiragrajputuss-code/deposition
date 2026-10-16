"""Replay a real Claude Code session - yours, not a canned one.

Needs an existing Claude Code session on this machine. No API key, no network,
and nothing bundled: every other example in this directory ships a scenario so
it runs the same way for everyone, but a Claude Code transcript is a verbatim
record of a real working session - commands run, files edited, the exact text
exchanged - and this project's own privacy stance (see "Privacy" in the
README) is that your prompts are your data. Shipping someone else's session as
fixture data would be exactly the thing that stance exists to rule out, so
this example imports *your* newest session instead, for whichever directory
you run it from:

    cd any/project/claude-code/has/touched
    python /path/to/examples/14_claude_code_session.py

If Claude Code has no recorded session for the current directory, it says so
and exits rather than failing confusingly.
"""

import json
import sys
from pathlib import Path

from deposition.chain import verify_file
from deposition.importers.claude_code import TranscriptError, convert, discover_latest_session


def main() -> int:
    transcript = discover_latest_session()
    if transcript is None:
        print(
            "No Claude Code session found for this directory.\n"
            "Run this from a directory Claude Code has worked in, or pass a "
            "transcript path to deposition.importers.claude_code.convert() directly.",
            file=sys.stderr,
        )
        return 1

    # Not "./deposition": that name collides with any checkout of this very
    # project. Run this from a parent directory containing one, as
    # `~/projects` does for the session that built this SDK, and a bare
    # "deposition" output folder resolves to the project's own repo root -
    # writing a real, private transcript straight into a git working tree,
    # past the .gitignore rule that only excludes that name as a
    # *subdirectory*, not as the cwd itself. Found by running this example.
    out_dir = Path.cwd() / ".deposition-import"
    try:
        trace = convert(transcript, out_dir)
    except TranscriptError as exc:
        print(f"Could not import {transcript}: {exc}", file=sys.stderr)
        return 1

    # The same claim every Deposition trace makes, now about a session that
    # already happened - a hash chain over events someone else's tool wrote.
    result = verify_file(trace)
    print(f"imported {transcript.name}")
    print(f"  -> {trace}")
    print(f"  {result.summary()}")

    # The numbers nobody sees while the session is running: what the agent
    # actually touched, and how much the context window grew doing it.
    events = [json.loads(line) for line in trace.read_text("utf-8").splitlines()]
    mutating = [
        e for e in events if e["type"] == "tool_call" and e["body"].get("mutates_environment")
    ]
    failed = [e for e in events if e["type"] == "tool_call" and e["body"].get("error")]
    contexts = [e["body"]["context_tokens"] for e in events if e["type"] == "llm_call"]

    print(f"\n{len(events)} events")
    print(f"  {len(mutating)} tool calls changed something outside the agent")
    print(f"  {len(failed)} tool calls failed")
    if contexts:
        print(f"  context window: {contexts[0]:,} -> {contexts[-1]:,} tokens")

    if mutating:
        example = mutating[0]
        tool, args = example["body"]["tool"], example["body"]["arguments"]
        print(f"\nfirst thing it changed (seq {example['seq']}): {tool}({args!r:.80})")
        print(f"  caused by seq {example['caused_by']} - open it in the viewer to see why")

    print(f"\n  deposition view {trace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
