"""Log deposition's PyPI download counts to a CSV, so trend data exists from
day one rather than being reconstructed later from memory.

pypistats.org serves a rolling window, not permanent history - ask it again in
six months and early dates are simply gone from the response. This script is
what turns that rolling window into a permanent record: every run merges fresh
numbers into whatever is already in the CSV, so a date survives in the file
long after pypistats stops serving it, and a day already on file gets its
count refreshed if pypistats has revised it (mirrors catch up for a day or two
after it is first reported).

    python scripts/pypi_downloads.py                 # update docs/metrics/pypi_downloads.csv
    python scripts/pypi_downloads.py --package foo --out other.csv

Meant to run from a weekly GitHub Action
(.github/workflows/pypi-stats.yml), not by hand - but running it by hand is
exactly how it was tested against the real API before that Action existed.
"""

from __future__ import annotations

import argparse
import csv
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

PACKAGE = "deposition"
DEFAULT_OUT = Path(__file__).parent.parent / "docs" / "metrics" / "pypi_downloads.csv"
API_URL = "https://pypistats.org/api/packages/{package}/overall?mirrors=false"


class FetchError(Exception):
    """pypistats could not be reached or returned something unexpected."""


def fetch_daily_downloads(package: str, *, timeout: float = 20.0) -> dict[str, int]:
    """``{date: downloads}`` for every day pypistats currently reports.

    Mirror traffic is excluded (``mirrors=false``/``category: without_mirrors``)
    - mirrors replicate the whole index regardless of anyone using this
    package, so counting them would turn "nobody has downloaded this" into a
    number that looks like adoption.
    """
    url = API_URL.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"could not reach pypistats for {package!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"pypistats returned something that was not JSON: {exc}") from exc

    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FetchError(f"unexpected response shape from pypistats: {payload!r}")

    return {
        row["date"]: int(row["downloads"])
        for row in rows
        if row.get("category") == "without_mirrors"
    }


def read_existing(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["date"]: int(row["downloads"]) for row in csv.DictReader(fh)}


def merge(existing: dict[str, int], fresh: dict[str, int]) -> dict[str, int]:
    """Fresh numbers win for any date both sides have; dates only in
    ``existing`` - already aged out of what pypistats serves - are kept."""
    return {**existing, **fresh}


def write_csv(path: Path, rows: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "downloads"])
        for date in sorted(rows):
            writer.writerow([date, rows[date]])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--package", default=PACKAGE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    try:
        fresh = fetch_daily_downloads(args.package)
    except FetchError as exc:
        # Loud, not swallowed: this is maintenance tooling, not the SDK's
        # recording path - a failed week of trend data should be visible as a
        # red CI run, not a quietly shorter CSV nobody notices.
        print(f"pypi_downloads: {exc}")
        return 1

    existing = read_existing(args.out)
    merged = merge(existing, fresh)
    write_csv(args.out, merged)

    added = sorted(set(merged) - set(existing))
    changed = sorted(d for d in set(merged) & set(existing) if merged[d] != existing[d])
    total = sum(merged.values())
    print(
        f"{args.out}: {len(merged)} day(s) on file, {total} total downloads "
        f"(+{len(added)} new day(s), {len(changed)} revised)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
