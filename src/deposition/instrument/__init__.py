"""Auto-instrumentation: patch the LLM clients the user already has.

Two layers. The provider adapters wrap the OpenAI and Anthropic SDKs, which is
how the model calls get recorded whatever framework sits on top. The tool adapter
wraps the one method each framework funnels tool execution through, because
without it a framework run records model turns and nothing in between, and the
causal graph is empty for the users most likely to try it.

Patching is best-effort: if a client is not installed, or its internals moved,
instrumentation silently stays off rather than breaking the host agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import anthropic as _anthropic
from . import openai as _openai
from . import tools as _tools
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
    try:
        patched.extend(_tools.install(recorder))
    except Exception:  # noqa: BLE001
        pass
    return patched


def uninstall() -> None:
    """Restore every patched method. Called by ``deposition.shutdown()``."""
    revert_all()
    _installed.clear()


def installed() -> list[str]:
    """Which providers are currently patched."""
    return list(_installed)
