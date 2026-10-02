"""Where did this tool call's arguments come from?

An agent is told to do one thing by its operator, and then reads content it did
not write - a web page, a document, a tool result, another agent's reply. Prompt
injection is the case where that content gives the agent new instructions, and
the agent follows them.

Detecting *instructions* in text is a judgment call, and a probabilistic guess
has no business being sealed into an evidentiary record. Provenance is not a
judgment. If a value handed to a tool appears verbatim in content the agent
fetched, and nowhere in what the operator actually asked for, that is a
mechanical fact about the trace - and it is the signature of an exfiltration,
because the attacker's address has to come from somewhere.

So this module reports origin, never intent. "This argument came from fetched
content" is a fact a reader can check in the trace. "This was an attack" is a
conclusion for a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Tainted", "MIN_VALUE_LENGTH", "tainted_arguments"]

#: Shorter values collide by accident - a status word, a small number, an id
#: fragment - and a finding nobody trusts is worse than no finding.
MIN_VALUE_LENGTH = 10


@dataclass(frozen=True)
class Tainted:
    """One tool argument whose value originated in content the agent read."""

    seq: int
    tool: str
    field: str
    value: str
    source_seq: int
    source_tool: str

    def __str__(self) -> str:
        return (
            f"seq {self.seq}: {self.tool}({self.field}=…) carries a value that first "
            f"appeared in {self.source_tool}'s result at seq {self.source_seq}, "
            f"not in the instruction"
        )


def _strings(value: Any, field: str = "", depth: int = 0) -> list[tuple[str, str]]:
    """Every string leaf in an argument structure, with the field path it sits at."""
    if depth > 6:
        return []
    if isinstance(value, str):
        return [(field, value)]
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for key, item in value.items():
            out.extend(_strings(item, f"{field}.{key}" if field else str(key), depth + 1))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for index, item in enumerate(value):
            out.extend(_strings(item, f"{field}[{index}]" if field else str(index), depth + 1))
        return out
    return []


def _trusted_text(events: list[dict[str, Any]]) -> str:
    """What the operator actually asked for.

    The run header, every human annotation, and the first model turn's prompt -
    which is where a framework puts the task it was handed. Anything appearing
    here is the operator's own words and cannot be tainted by definition.
    """
    parts: list[str] = []
    seen_first_call = False
    for event in events:
        kind = event.get("type")
        body = event.get("body") or {}
        if kind == "run_start":
            parts.append(str(body))
        elif kind == "annotation" and body.get("author") == "user":
            parts.append(str(body.get("note") or ""))
        elif kind == "llm_call" and not seen_first_call:
            seen_first_call = True
            parts.append(str(body.get("messages") or ""))
    return "\n".join(parts)


def tainted_arguments(events: list[Any]) -> list[Tainted]:
    """Tool arguments carrying values that came from earlier tool output.

    Never raises: like the rest of the read path, a malformed trace yields no
    findings rather than an exception.
    """
    rows = [e for e in events if isinstance(e, dict)]
    trusted = _trusted_text(rows)
    findings: list[Tainted] = []
    #: Results already read by the agent, newest last: (seq, tool, text).
    read_so_far: list[tuple[int, str, str]] = []

    for event in rows:
        if event.get("type") != "tool_call":
            continue
        body = event.get("body") or {}
        tool = str(body.get("tool") or "tool")

        for field, value in _strings(body.get("arguments")):
            if len(value) < MIN_VALUE_LENGTH or value in trusted:
                continue
            for source_seq, source_tool, text in read_so_far:
                if value in text:
                    findings.append(
                        Tainted(
                            seq=event.get("seq", -1),
                            tool=tool,
                            field=field or "argument",
                            value=value,
                            source_seq=source_seq,
                            source_tool=source_tool,
                        )
                    )
                    break

        result = body.get("result")
        if result is not None:
            read_so_far.append((event.get("seq", -1), tool, str(result)))

    return findings
