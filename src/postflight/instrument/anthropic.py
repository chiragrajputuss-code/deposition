"""Anthropic Python SDK instrumentation.

Wraps ``Anthropic.messages.create`` (and the streaming variant) into an
``llm_call`` event. Same shape as the OpenAI adapter - the schema is provider
neutral, ``body.provider`` is what differs.

Phase 1 stub: see the note in ``openai.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .. import Recorder


def install(recorder: Recorder) -> bool:
    """Patch the Anthropic client. Returns False when it is not installed."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return False  # TODO(phase-1): wrap messages.create and messages.stream
