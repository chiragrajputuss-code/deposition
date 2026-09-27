"""Shared fixtures.

The SDK keeps one process-wide recorder, so every test that calls ``init()``
must leave it shut down again or the next test inherits a live run.
"""

from __future__ import annotations

import pytest

import postflight


@pytest.fixture(autouse=True)
def _reset_recorder():
    yield
    postflight.shutdown()


@pytest.fixture
def trace_dir(tmp_path):
    """A recording directory that vanishes with the test."""
    return tmp_path / "postflight"
