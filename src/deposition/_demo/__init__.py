"""Resource locators for the trace bundled with ``deposition demo``.

The trace, its signature sidecar and the public key to check it against are
data files in this package directory, found through :mod:`importlib.resources`
rather than a path relative to this file - the robust way to reach bundled
data regardless of how the package was installed (wheel, editable install,
zipapp). Zero added dependency: ``importlib.resources`` is the standard
library (ADR 003).

How the trace itself was produced is
`scripts/generate_demo_trace.py <https://github.com/chiragrajputuss-code/deposition/blob/main/scripts/generate_demo_trace.py>`_
in the repository, not here - this module only locates what is already built.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = ["trace_path", "public_key"]

_PACKAGE = __name__


def trace_path() -> Path:
    """Filesystem path to the bundled trace - a real ``.jsonl``, not a
    resource stream, because the viewer needs an actual path to serve and to
    re-read on every request."""
    return resources.files(_PACKAGE).joinpath("run_demo.jsonl")


def public_key() -> str:
    """The public key the bundled trace was signed with, so the viewer can
    report ``Authorship: verified`` with no configuration from the caller."""
    return resources.files(_PACKAGE).joinpath("run_demo.pem.pub").read_text("utf-8").strip()
