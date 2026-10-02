"""Mandates: audit a trace against the authority recorded inside it (ADR 010).

The mandate lives in ``run_start.body.mandate``, inside the hashed body, so the
policy and the conduct seal together - the rule the run was judged by cannot be
quietly rewritten after the fact any more than the conduct can.

Like chain verification, auditing never raises on bad input: the outcome is a
result the caller decides about. And like the rest of this SDK, the audit says
what it could *not* check - a mandate key this version cannot enforce is listed,
never silently skipped, because an audit that is quiet about its own blind spots
is exactly the overclaim this product exists to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ENFORCED_KEYS", "INFORMATIONAL_KEYS", "AuditResult", "Violation", "audit"]

#: Mandate keys this version actually enforces.
ENFORCED_KEYS = (
    "allowed_tools",
    "forbidden_tools",
    "external_tools",
    "max_cost_usd",
    "max_tokens",
    "max_steps",
)

#: Recorded, shown, never enforced.
INFORMATIONAL_KEYS = ("issuer", "notes")


@dataclass(frozen=True)
class Violation:
    """One way the run exceeded its mandate."""

    code: str
    message: str
    seq: int
    caused_by: tuple[int, ...] = ()

    def __str__(self) -> str:
        where = f"seq {self.seq}"
        if self.caused_by:
            where += f" (caused_by {', '.join(map(str, self.caused_by))})"
        return f"{where}: {self.code} - {self.message}"


@dataclass
class AuditResult:
    """Outcome of auditing a trace against its own mandate.

    ``attestable`` is the headline: it is true only when a mandate exists, no
    violation was found, and the recorder itself reported no dropped events.
    Compliance cannot be attested on a record that admits it is incomplete.
    """

    mandate: dict[str, Any] | None = None
    violations: list[Violation] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    dropped: int = 0
    adopted: int = 0

    @property
    def attestable(self) -> bool:
        return self.mandate is not None and not self.violations and self.dropped == 0

    def summary(self) -> str:
        if self.mandate is None:
            return "NO MANDATE  this trace does not record what the run was allowed to do"
        if self.violations:
            return f"EXCEEDED  {len(self.violations)} violation(s); first: {self.violations[0]}"
        if self.dropped:
            return (
                f"NOT ATTESTABLE  no violation found, but the recorder reports "
                f"{self.dropped} dropped event(s) - the record admits gaps"
            )
        return f"WITHIN MANDATE  {len(self.checked)} limit(s) checked"


def _events(events: list[Any]) -> list[dict[str, Any]]:
    return [e for e in events if isinstance(e, dict)]


def audit(events: list[Any]) -> AuditResult:
    """Replay a trace against the mandate its own run_start carries."""
    result = AuditResult()
    rows = _events(events)

    start = next((e for e in rows if e.get("type") == "run_start"), None)
    mandate = (start or {}).get("body", {}).get("mandate")
    if not isinstance(mandate, dict):
        return result
    result.mandate = mandate

    for key in mandate:
        if key in ENFORCED_KEYS:
            result.checked.append(key)
        elif key not in INFORMATIONAL_KEYS:
            result.unchecked.append(key)

    # Which tools reach outside the agent - send a message, move money, write
    # somewhere durable. Declared by the operator, never guessed: whether a tool
    # is consequential is a fact about their system, not about its name.
    external = set(mandate.get("external_tools") or ())
    allowed = mandate.get("allowed_tools")
    forbidden = set(mandate.get("forbidden_tools") or ())
    max_cost = mandate.get("max_cost_usd")
    max_tokens = mandate.get("max_tokens")
    max_steps = mandate.get("max_steps")

    cost = 0.0
    tokens = 0
    steps = 0
    cost_flagged = tokens_flagged = steps_flagged = False

    def flag(code: str, message: str, event: dict[str, Any]) -> None:
        result.violations.append(
            Violation(
                code=code,
                message=message,
                seq=event.get("seq", -1),
                caused_by=tuple(event.get("caused_by") or ()),
            )
        )

    for event in rows:
        kind = event.get("type")
        body = event.get("body") or {}

        if kind == "tool_call":
            tool = body.get("tool")
            if tool in forbidden:
                flag("forbidden_tool", f"tool {tool!r} is forbidden by the mandate", event)
            elif isinstance(allowed, list) and tool not in allowed:
                flag("tool_not_allowed", f"tool {tool!r} is not in allowed_tools", event)

        if kind not in ("run_start", "run_end"):
            steps += 1
            if max_steps is not None and steps > max_steps and not steps_flagged:
                steps_flagged = True
                flag("max_steps", f"step {steps} exceeds the ceiling of {max_steps}", event)

        usage = body.get("usage")
        if isinstance(usage, dict):
            tokens += int(usage.get("total_tokens") or 0)
            if max_tokens is not None and tokens > max_tokens and not tokens_flagged:
                tokens_flagged = True
                flag(
                    "max_tokens",
                    f"cumulative tokens reached {tokens}, over the ceiling of {max_tokens}",
                    event,
                )
        if isinstance(body.get("cost_usd"), (int, float)):
            cost += float(body["cost_usd"])
            if max_cost is not None and cost > max_cost and not cost_flagged:
                cost_flagged = True
                flag(
                    "max_cost_usd",
                    f"cumulative cost reached ${cost:.4f}, over the ceiling of ${max_cost:.2f}",
                    event,
                )

        if kind == "run_end":
            totals = body.get("totals") or {}
            result.dropped = int(totals.get("dropped") or 0)
            result.adopted = int(totals.get("adopted") or 0)

    # Provenance becomes a violation only where the operator said it matters.
    #
    # A value that came from fetched content is an ordinary fact - every
    # retrieval produces one, and alerting on all of them would bury the finding
    # that counts under thousands that do not. It is when such a value reaches a
    # tool the operator declared *external* that it stops being a fact about
    # data flow and becomes an unauthorised instruction crossing a boundary.
    if external:
        from .taint import tainted_arguments

        for finding in tainted_arguments(rows):
            if finding.tool in external:
                result.violations.append(
                    Violation(
                        code="tainted_external",
                        message=(
                            f"{finding.tool} is declared external and was called with "
                            f"{finding.field}={finding.value[:60]!r}, a value that first "
                            f"appeared in {finding.source_tool}'s result at seq "
                            f"{finding.source_seq} rather than in the instruction"
                        ),
                        seq=finding.seq,
                        caused_by=(finding.source_seq,),
                    )
                )
        result.violations.sort(key=lambda v: v.seq)

    return result
