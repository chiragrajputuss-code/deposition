"""What a trace shows and does not show is said the same way everywhere.

The README section, `depo verify --help` and the standalone verifier's header
each state the trust boundary. If one of them is reworded and the others are
not, a reader gets two different claims from one project - so the sentences
that carry the claim are pinned here, in all three.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from deposition.cli import main

ROOT = Path(__file__).parent.parent

KEY_SENTENCES = [
    "The file was not altered after it was written.",
    "It was signed by a specific key, if you pin that key",
    "What the run was authorised to do was sealed together with what it did",
    "Which tool definitions the model was shown, as digests",
    "That the trace existed no later than a time asserted by an independent timestamp authority",
    "That the recorded events happened.",
    "The process that writes the trace is the process that runs the tools.",
    "Completeness where events were dropped.",
]


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("**", "").replace("`", ""))


def readme_section() -> str:
    readme = (ROOT / "README.md").read_text("utf-8")
    start = readme.index("## What a Deposition trace proves, and what it does not")
    end = readme.index("\n## ", start + 1)
    return readme[start:end]


def verify_help(capsys) -> str:
    with pytest.raises(SystemExit):
        main(["verify", "--help"])
    return capsys.readouterr().out


def standalone_header() -> str:
    tree = ast.parse((ROOT / "scripts" / "verify_standalone.py").read_text("utf-8"))
    return ast.get_docstring(tree) or ""


@pytest.mark.parametrize("sentence", KEY_SENTENCES)
def test_the_trust_boundary_is_worded_the_same_in_readme_help_and_standalone_verifier(
    capsys, sentence
):
    for name, text in (
        ("README", readme_section()),
        ("depo verify --help", verify_help(capsys)),
        ("verify_standalone.py", standalone_header()),
    ):
        assert sentence in normalise(text), f"{name} is missing: {sentence!r}"


def test_the_trust_boundary_section_never_claims_tamper_proof():
    section = readme_section().lower()
    assert "tamper-proof" not in section
    assert "tamperproof" not in section
