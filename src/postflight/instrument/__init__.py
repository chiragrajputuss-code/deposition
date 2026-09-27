"""Auto-instrumentation: patch the LLM clients the user already has.

v0 covers the OpenAI and Anthropic Python SDKs only. LangGraph and CrewAI
adapters are Phase 4 - adapters chase frameworks, and frameworks change faster
than a solo developer can follow. Patching is best-effort: if a client is not
installed, or its internals moved, instrumentation silently stays off rather
than breaking the host agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .. import Recorder

_installed: set[str] = set()


def install(recorder: Recorder) -> list[str]:
    """Patch whichever supported clients are importable. Returns what was patched."""
    patched: list[str] = []
    for name, module in (("openai", _openai), ("anthropic", _anthropic)):
        if name in _installed:
            continue
        try:
            if module.install(recorder):
                _installed.add(name)
                patched.append(name)
        except Exception:  # noqa: BLE001 - instrumentation is never worth a crash
            continue
    return patched


def _import_optional(name: str) -> Any | None:
    try:
        return __import__(name)
    except Exception:  # noqa: BLE001
        return None


from . import anthropic as _anthropic  # noqa: E402
from . import openai as _openai  # noqa: E402

__all__ = ["install"]
