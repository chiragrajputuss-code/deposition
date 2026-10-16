"""Converters from another tool's own session log into a Deposition trace.

Deposition's own recorder writes events as they happen, hash-chained from the
first. An importer does the opposite: it is handed a log another tool already
wrote - plain, mutable JSON, with no chain and no signature - and rebuilds it as
one. That is a strictly weaker claim than recording live, and every importer in
this package says so in its own module, because the honest grade of an imported
trace is part of what it proves.

* :mod:`deposition.importers.claude_code` - Claude Code session transcripts.
"""

from __future__ import annotations

__all__: list[str] = []
