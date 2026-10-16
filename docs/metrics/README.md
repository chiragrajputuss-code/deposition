# Metrics

`pypi_downloads.csv` — daily PyPI download counts for `deposition`, without
mirror traffic, updated weekly by
[`.github/workflows/pypi-stats.yml`](../../.github/workflows/pypi-stats.yml)
running [`scripts/pypi_downloads.py`](../../scripts/pypi_downloads.py).

pypistats.org only serves a rolling window of recent days - ask it again in six
months and today's date is gone from the response. The script merges each
week's fetch into the existing file rather than replacing it, so a date
survives here long after pypistats stops reporting it.

**Read the first few rows skeptically.** A package's first day or two on PyPI
routinely shows a spike with no real users behind it - package-index crawlers
and mirror-sync traffic check every newly published release, and `deposition`'s
own first day (2026-10-02) was 118 downloads against single digits every day
since. That is not adoption; it is what publishing looks like. The real signal
is the sustained trend once that spike passes, not the first number in the file.
