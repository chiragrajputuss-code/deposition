"""The links a newcomer clicks first point at the repository that exists.

PyPI's Homepage/Source links come from pyproject.toml, and they once pointed at
a repository that had never been created while the README pointed at the real
one. Read with a regex rather than tomllib, which Python 3.10 lacks.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
REPO = "https://github.com/chiragrajputuss-code/deposition"


def project_urls() -> dict[str, str]:
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    section = text.split("[project.urls]", 1)[1].split("\n[", 1)[0]
    return dict(re.findall(r'^(\w+)\s*=\s*"([^"]+)"', section, re.M))


def test_project_urls_point_at_the_same_repository_as_the_readme():
    readme = (ROOT / "README.md").read_text("utf-8")
    assert f"{REPO}/actions/workflows/ci.yml" in readme
    urls = project_urls()
    assert urls["Homepage"] == REPO
    assert urls["Source"] == REPO


def test_no_file_a_newcomer_reads_mentions_the_old_canvas_name_or_dead_repository():
    paths = [ROOT / "README.md", ROOT / "pyproject.toml", *ROOT.glob(".github/**/*.*")]
    paths += list((ROOT / "docs").rglob("*.md"))
    offenders = [
        str(p.relative_to(ROOT)) for p in paths if "canvas" in p.read_text("utf-8").lower()
    ]
    assert offenders == []


def test_the_package_author_is_the_maintainers_own_name():
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    assert 'authors = [{ name = "Chirag Rajput" }]' in text


def test_the_issue_template_contact_link_points_at_this_repository():
    config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text("utf-8")
    assert f"url: {REPO}/discussions" in config
