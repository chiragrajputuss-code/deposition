"""Auto-instrumentation: patch the LLM clients the user already has.

v0 covers the OpenAI and Anthropic Python SDKs only. LangGraph and CrewAI
adapters are Phase 4 - adapters chase frameworks, and frameworks change faster
than a solo developer can follow.

Patching is best-effort: if a client is not installed, or its internals moved,
instrumentation silently stays off rather than breaking the host agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import anthropic as _anthropic
from . import openai as _openai
from ._common import revert_all

if TYPE_CHECKING:  # pragma: no cover
    from .. import Recorder

__all__ = ["install", "uninstall", "installed"]

#: Providers patched in this process, in the order they were patched.
_installed: list[str] = []

_ADAPTERS = (("openai", _openai), ("anthropic", _anthropic))


def install(recorder: Recorder) -> list[str]:
    """Patch whichever supported clients are importable. Returns what was patched."""
    patched: list[str] = []
    for name, adapter in _ADAPTERS:
        if name in _installed:
            continue
        try:
            if adapter.install(recorder):
                _installed.append(name)
                patched.append(name)
        except Exception:  # noqa: BLE001 - instrumentation is never worth a crash
            continue
    return patched


def uninstall() -> None:
    """Restore every patched method. Called by ``deposition.shutdown()``."""
    revert_all()
    _installed.clear()


def installed() -> list[str]:
    """Which providers are currently patched."""
    return list(_installed)
