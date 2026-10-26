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
    "Verification checks the integrity of the record, not that the events in it happened.",
    "Host-attested: recorded only by the machine running the agent",
    "It means what the host claims happened, unedited since it was written.",
    "Everything Deposition records today is host-attested.",
    "Corroborated: an external event",
    "confirmed by the counterparty that handled it",
    "a receipt signed with a key the agent host cannot use. Not implemented yet.",
    "The file was not altered after it was written.",
    "It was signed by a specific key, if you pin that key",
    "What the run was authorised to do was sealed together with what it did",
    "Which tool definitions the model was shown, as digests",
    "Unedited since time T, as asserted by an independent timestamp authority",
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


def test_verified_is_used_only_for_signatures_never_for_host_attested_events(capsys):
    """ADR 016: verification checks the record's integrity. The word is kept only
    where a signature over the record is checked against a key."""
    for name, text in (
        ("README", readme_section()),
        ("depo verify --help", verify_help(capsys)),
        ("verify_standalone.py", standalone_header()),
    ):
        for sentence in re.split(r"(?<=[.:])\s+", normalise(text)):
            if re.search(r"\bverified\b", sentence, re.I):
                assert re.search(r"\bkey\b|signature", sentence, re.I), (
                    f"{name} uses 'verified' outside a signature: {sentence!r}"
                )


def test_the_raise_the_bar_steps_are_ordered_receipts_countersigning_keys():
    section = readme_section()
    table = section[section.index("**What would raise the bar**") :]
    positions = [
        table.index(step)
        for step in ("Counterparty receipts", "Hosted countersigning", "Moving signing keys off")
    ]
    assert positions == sorted(positions)


def test_the_trust_boundary_section_never_claims_tamper_proof():
    section = readme_section().lower()
    assert "tamper-proof" not in section
    assert "tamperproof" not in section
