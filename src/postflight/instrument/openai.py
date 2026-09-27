"""OpenAI Python SDK instrumentation.

Wraps ``OpenAI.chat.completions.create`` and records an ``llm_call`` event with
the request params, messages in, response out, token usage and latency. Field
names follow the OTel GenAI conventions (``gen_ai.request.model``).

Phase 1 stub: the hook points are fixed, the body mapping lands with the first
example agent so it is exercised by a real run rather than guessed at.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .. import Recorder


def install(recorder: Recorder) -> bool:
    """Patch the OpenAI client. Returns False when it is not installed."""
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return False  # TODO(phase-1): wrap chat.completions.create / responses.create
