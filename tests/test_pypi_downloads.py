"""The PyPI download tracker (scripts/pypi_downloads.py).

pypistats serves a rolling window - ask it again in six months and early dates
are gone from the response. These tests are the claim that the CSV survives
that: a date already on file is kept even once the API stops mentioning it,
and a date both sides report gets the newer number, because pypistats revises
a day's count for a day or two after mirrors catch up.

Not a package module - deliberately outside src/deposition, since it has
nothing to do with recording a trace. Imported here by path, the standard way
to test a standalone repo script.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "pypi_downloads.py"
spec = importlib.util.spec_from_file_location("pypi_downloads", SCRIPT)
pypi_downloads = importlib.util.module_from_spec(spec)
sys.modules["pypi_downloads"] = pypi_downloads
spec.loader.exec_module(pypi_downloads)

from pypi_downloads import (  # noqa: E402
    FetchError,
    fetch_daily_downloads,
    main,
    merge,
    read_existing,
    write_csv,
)

# -- merge: the whole point of keeping a CSV at all --------------------------


def test_a_date_only_the_fresh_fetch_has_is_added():
    assert merge({}, {"2026-10-02": 118}) == {"2026-10-02": 118}


def test_a_date_pypistats_no_longer_serves_is_kept_from_the_existing_file():
    existing = {"2026-01-01": 5}
    fresh = {"2026-10-02": 118}  # pypistats' rolling window has moved on
    assert merge(existing, fresh) == {"2026-01-01": 5, "2026-10-02": 118}


def test_a_date_both_sides_report_takes_the_fresh_revised_number():
    # pypistats revises a day's count for a day or two as mirrors replicate -
    # the newer fetch is the one worth trusting.
    existing = {"2026-10-02": 40}
    fresh = {"2026-10-02": 118}
    assert merge(existing, fresh) == {"2026-10-02": 118}


def test_merge_is_a_pure_function_and_does_not_mutate_its_arguments():
    existing = {"2026-10-02": 40}
    merge(existing, {"2026-10-02": 118})
    assert existing == {"2026-10-02": 40}


# -- CSV read/write ------------------------------------------------------------


def test_write_csv_then_read_existing_round_trips(tmp_path):
    path = tmp_path / "out.csv"
    write_csv(path, {"2026-10-03": 12, "2026-10-02": 118})
    assert read_existing(path) == {"2026-10-02": 118, "2026-10-03": 12}


def test_write_csv_sorts_by_date_regardless_of_insertion_order(tmp_path):
    path = tmp_path / "out.csv"
    write_csv(path, {"2026-10-09": 3, "2026-10-02": 118, "2026-10-06": 3})
    with path.open(newline="", encoding="utf-8") as fh:
        dates = [row["date"] for row in csv.DictReader(fh)]
    assert dates == ["2026-10-02", "2026-10-06", "2026-10-09"]


def test_read_existing_on_a_missing_file_is_an_empty_dict_not_an_error(tmp_path):
    assert read_existing(tmp_path / "does-not-exist.csv") == {}


def test_write_csv_creates_its_parent_directory(tmp_path):
    path = tmp_path / "nested" / "metrics" / "out.csv"
    write_csv(path, {"2026-10-02": 1})
    assert path.exists()


# -- fetch_daily_downloads: the network boundary, mocked ----------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def test_fetch_keeps_only_without_mirrors_rows(monkeypatch):
    payload = {
        "data": [
            {"category": "without_mirrors", "date": "2026-10-02", "downloads": 118},
            {"category": "with_mirrors", "date": "2026-10-02", "downloads": 4000},
        ]
    }
    monkeypatch.setattr(
        pypi_downloads.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
    )
    assert fetch_daily_downloads("deposition") == {"2026-10-02": 118}


def test_fetch_raises_fetcherror_on_a_network_failure(monkeypatch):
    def boom(*a, **k):
        raise URLError("no route to host")

    monkeypatch.setattr(pypi_downloads.urllib.request, "urlopen", boom)
    with pytest.raises(FetchError):
        fetch_daily_downloads("deposition")


def test_fetch_raises_fetcherror_on_an_unexpected_response_shape(monkeypatch):
    monkeypatch.setattr(
        pypi_downloads.urllib.request, "urlopen", lambda *a, **k: _FakeResponse({"oops": True})
    )
    with pytest.raises(FetchError):
        fetch_daily_downloads("deposition")


# -- main(): the CLI end to end, network mocked --------------------------------


def test_main_writes_the_csv_and_exits_zero(tmp_path, monkeypatch, capsys):
    payload = {"data": [{"category": "without_mirrors", "date": "2026-10-02", "downloads": 5}]}
    monkeypatch.setattr(
        pypi_downloads.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
    )
    out = tmp_path / "out.csv"
    exit_code = main(["--package", "deposition", "--out", str(out)])
    assert exit_code == 0
    assert read_existing(out) == {"2026-10-02": 5}
    assert "1 day(s) on file" in capsys.readouterr().out


def test_main_exits_nonzero_and_leaves_no_half_written_file_on_fetch_failure(
    tmp_path, monkeypatch, capsys
):
    def boom(*a, **k):
        raise URLError("timed out")

    monkeypatch.setattr(pypi_downloads.urllib.request, "urlopen", boom)
    out = tmp_path / "out.csv"
    exit_code = main(["--package", "deposition", "--out", str(out)])
    assert exit_code == 1
    assert not out.exists()


def test_a_second_run_reports_zero_new_days_when_nothing_changed(tmp_path, monkeypatch, capsys):
    payload = {"data": [{"category": "without_mirrors", "date": "2026-10-02", "downloads": 5}]}
    monkeypatch.setattr(
        pypi_downloads.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
    )
    out = tmp_path / "out.csv"
    main(["--package", "deposition", "--out", str(out)])
    capsys.readouterr()
    main(["--package", "deposition", "--out", str(out)])
    assert "+0 new day(s), 0 revised" in capsys.readouterr().out
