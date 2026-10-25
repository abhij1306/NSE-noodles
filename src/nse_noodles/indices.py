"""NSE's daily index archive (`ind_close_all_DDMMYYYY.csv`): every NSE index's OHLC for a day.

The archive is fetched only for days that have a bhavcopy (a session), on the same paced
client. Names from before NSE's 2015 renaming are read as today's names.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from .http import NseClient
from .layout import Layout, NotFoundMemo, atomic_write, write_bytes

ARCHIVE_URL = "https://nsearchives.nseindia.com/content/indices/"
NIFTY_50, NIFTY_500 = "NIFTY 50", "NIFTY 500"
DEFAULT_NAMES = (NIFTY_50, NIFTY_500, "NIFTY SMALLCAP 250", "INDIA VIX")
ALIASES = {
    "CNX NIFTY": NIFTY_50,
    "S&P CNX NIFTY": NIFTY_50,
    "CNX 500": NIFTY_500,
    "S&P CNX 500": NIFTY_500,
}
INDEX_SCHEMA = {
    "date": pl.Date,
    "index_name": pl.Utf8,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
}


def archive_name(day: date) -> str:
    return f"ind_close_all_{day:%d%m%Y}.csv"


def _num(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def parse(text: str, day: date, names: tuple[str, ...] | None = None) -> list[dict]:
    """The rows of one archive file; `names` limits them (None keeps every index)."""
    wanted = {n.upper() for n in names} if names else None
    out = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name = row.get("index name", "").upper()
        name = ALIASES.get(name, name)
        if not name or (wanted is not None and name not in wanted):
            continue
        values = [
            _num(row.get(k, ""))
            for k in (
                "open index value",
                "high index value",
                "low index value",
                "closing index value",
            )
        ]
        if values[3] is None:
            continue
        o, h, low, c = values  # a missing open stays missing: never the close in its place
        out.append({"date": day, "index_name": name, "open": o, "high": h, "low": low, "close": c})
    return out


@dataclass
class PullReport:
    downloaded: int = 0
    already: int = 0
    not_found: list[date] = field(default_factory=list)
    errors: list[date] = field(default_factory=list)


def session_days(layout: Layout) -> list[date]:
    out = []
    for path in layout.bars.glob("date=*.parquet"):
        out.append(date.fromisoformat(path.stem.removeprefix("date=")))
    return sorted(out)


def pull(
    layout: Layout, client: NseClient, days: list[date], *, recheck: bool = False
) -> PullReport:
    memo = NotFoundMemo(layout.raw / "not_found.json")
    report = PullReport()
    for day in days:
        path = layout.indices_raw / archive_name(day)
        if path.exists():
            report.already += 1
            continue
        if memo.has("indices", day) and not recheck:
            continue
        got = client.get(ARCHIVE_URL + archive_name(day))
        if got.status == "ok" and parse(got.data.decode("utf-8-sig", errors="replace"), day):
            write_bytes(path, got.data)
            report.downloaded += 1
        elif got.status == "not_found":
            memo.add("indices", day)
            report.not_found.append(day)
        else:  # an error, or a body that is no index file (a block page): asked again next run
            report.errors.append(day)
    return report


def build(layout: Layout, names: tuple[str, ...] | None = DEFAULT_NAMES) -> int:
    """Parse every downloaded archive into `index/date=YYYY-MM-DD.parquet`; returns days written."""
    written = 0
    for path in sorted(layout.indices_raw.glob("ind_close_all_*.csv")):
        m = re.fullmatch(r"ind_close_all_(\d{2})(\d{2})(\d{4})\.csv", path.name)
        if not m:
            continue
        day = date(int(m[3]), int(m[2]), int(m[1]))
        target = layout.index / f"date={day.isoformat()}.parquet"
        if target.exists():
            continue
        rows = parse(path.read_bytes().decode("utf-8-sig", errors="replace"), day, names)
        frame = pl.DataFrame(rows, schema=INDEX_SCHEMA)
        atomic_write(target, lambda tmp, f=frame: f.write_parquet(tmp))
        written += 1
    return written


def levels(layout: Layout, name: str) -> dict[date, tuple[float, float]]:
    """{day: (open, close)} of one index from the parsed files; empty when none were built."""
    if not any(layout.index.glob("date=*.parquet")):
        return {}
    frame = pl.read_parquet(layout.index_glob()).filter(pl.col("index_name") == name)
    return {d: (o, c) for d, o, c in frame.select("date", "open", "close").iter_rows()}
