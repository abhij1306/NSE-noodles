"""NSE daily bhavcopy archives: file names, the polite resumable download, and the parser.

Two formats cover 22 Jun 2011 to today: the old CM bhavcopy up to 5 Jul 2024 and UDiFF from
8 Jul 2024. Files before 22 Jun 2011 have no ISIN column, so no row can be keyed to a security
across symbol changes: the history starts there.

Every security that traded on a day is in that day's file, delisted ones included, so a
history built from the archives (and never filtered by today's listing) is survivorship-bias
free by construction.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from .http import NseClient
from .layout import Layout, NotFoundMemo, atomic_write, write_bytes
from .parse import ISIN_RE

logger = logging.getLogger(__name__)

FIRST_ISIN_DAY = date(2011, 6, 22)
OLD_LAST_DAY = date(2024, 7, 5)
UDIFF_FIRST_DAY = date(2024, 7, 8)
_MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
# Equity series: normal market (EQ), trade-for-trade (BE, BZ) and the block / institutional
# windows that trade beside them (BL, BT, IL, IQ). Debt, ETF-only and SME series are left out.
EQUITY_SERIES = frozenset({"EQ", "BE", "BZ", "BL", "BT", "IL", "IQ"})
MARKET_SERIES = ("EQ", "BE", "BZ")  # a day keeps one row per ISIN: EQ, then BE, BZ, the rest

BAR_SCHEMA = {
    "date": pl.Date,
    "isin": pl.Utf8,
    "symbol": pl.Utf8,
    "series": pl.Utf8,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "prev_close": pl.Float64,
    "volume": pl.Int64,
    "traded_value": pl.Float64,
}


def format_for(day: date) -> str:
    if day < FIRST_ISIN_DAY:
        raise ValueError("bhavcopy before 2011-06-22 has no ISIN")
    if day <= OLD_LAST_DAY:
        return "old"
    if day >= UDIFF_FIRST_DAY:
        return "udiff"
    raise ValueError("no file between the two formats (6-7 Jul 2024 is a weekend)")


def archive_name(day: date) -> str:
    if format_for(day) == "old":
        return f"cm{day.day:02d}{_MONTHS[day.month - 1]}{day.year}bhav.csv.zip"
    return f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"


def archive_url(day: date) -> str:
    name = archive_name(day)
    if format_for(day) == "old":
        return (
            "https://nsearchives.nseindia.com/content/historical/EQUITIES/"
            f"{day.year}/{_MONTHS[day.month - 1]}/{name}"
        )
    return f"https://nsearchives.nseindia.com/content/cm/{name}"


def candidate_days(start: date, end: date) -> list[date]:
    """Every weekday from `start` (never before `FIRST_ISIN_DAY`) to `end`. Weekend special
    sessions (Budget Saturdays, Muhurat Sundays) are added with `--extra-days`."""
    day, out = max(start, FIRST_ISIN_DAY), []
    while day <= end:
        if day.weekday() < 5 and not (OLD_LAST_DAY < day < UDIFF_FIRST_DAY):
            out.append(day)
        day += timedelta(days=1)
    return out


@dataclass
class PullReport:
    downloaded: int = 0
    already: int = 0
    not_found: list[date] = field(default_factory=list)  # holiday candidates
    skipped_known: int = 0
    errors: list[date] = field(default_factory=list)  # transient: a rerun retries them


def pull(
    layout: Layout, client: NseClient, days: Iterable[date], *, recheck: bool = False
) -> PullReport:
    """Download each missing day's zip once into `raw/bhavcopy/`. A 404 is remembered as a
    holiday candidate; an error is retried by the next run."""
    memo = NotFoundMemo(layout.raw / "not_found.json")
    report = PullReport()
    for day in days:
        path = layout.bhavcopy / archive_name(day)
        if path.exists():
            report.already += 1
            continue
        if memo.has("bhavcopy", day) and not recheck:
            report.skipped_known += 1
            continue
        got = client.get(archive_url(day))
        if got.status == "ok":
            write_bytes(path, got.data)
            memo.discard("bhavcopy", day)
            report.downloaded += 1
        elif got.status == "not_found":
            memo.add("bhavcopy", day)
            report.not_found.append(day)
        else:
            report.errors.append(day)
    memo.save()
    return report


# --- parsing ------------------------------------------------------------------------------------

_OLD = {
    "date": "TIMESTAMP", "isin": "ISIN", "series": "SERIES", "symbol": "SYMBOL", "open": "OPEN",
    "high": "HIGH", "low": "LOW", "close": "CLOSE", "prev": "PREVCLOSE", "volume": "TOTTRDQTY",
    "value": "TOTTRDVAL",
}  # fmt: skip
_UDIFF = {
    "date": "TradDt", "isin": "ISIN", "series": "SctySrs", "symbol": "TckrSymb",
    "open": "OpnPric", "high": "HghPric", "low": "LwPric", "close": "ClsPric",
    "prev": "PrvsClsgPric", "volume": "TtlTradgVol", "value": "TtlTrfVal",
}  # fmt: skip


def _old_date(raw: str) -> date | None:
    parts = raw.strip().upper().split("-")
    if len(parts) != 3 or parts[1] not in _MONTHS:
        return None
    year = "20" + parts[2] if len(parts[2]) == 2 else parts[2]  # some 2020 files write "20"
    try:
        return date(int(year), _MONTHS.index(parts[1]) + 1, int(parts[0]))
    except ValueError:
        return None


def _iso_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _num(raw: str | None) -> float | None:
    try:
        value = float((raw or "").replace(",", "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def series_rank(series: str) -> tuple[int, str]:
    try:
        return (MARKET_SERIES.index(series), "")
    except ValueError:
        return (len(MARKET_SERIES), series)


def row_ok(o: float, h: float, low: float, c: float, prev: float | None, volume: int) -> bool:
    """Every price positive, low <= open, close <= high, prev close positive when present,
    volume not negative. A failing row is counted and left out, never repaired."""
    return (
        min(o, h, low, c) > 0
        and low <= o <= h
        and low <= c <= h
        and (prev is None or prev > 0)
        and volume >= 0
    )


@dataclass
class ParsedDay:
    day: date
    rows: list[dict] = field(default_factory=list)
    skipped_series: int = 0
    invalid: int = 0
    wrong_date: int = 0
    other_series: int = 0  # a second series of an ISIN that traded under a better-ranked one


def _csv_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError("bhavcopy archive must hold exactly one CSV")
        return archive.read(names[0]).decode("utf-8-sig", errors="replace")


def _bar(row: dict[str, str], cols: dict[str, str], day: date, series: str) -> dict | None:
    """The row as a `BAR_SCHEMA` bar, or None when a field is missing or fails `row_ok`."""
    isin = row.get(cols["isin"], "").upper()
    o, h, low, c = (_num(row.get(cols[k])) for k in ("open", "high", "low", "close"))
    prev, value = _num(row.get(cols["prev"])), _num(row.get(cols["value"]))
    vol = _num(row.get(cols["volume"]))
    symbol = row.get(cols["symbol"], "").upper()
    if (
        not ISIN_RE.match(isin)
        or not symbol
        or None in (o, h, low, c, vol)
        or vol != int(vol)  # type: ignore[arg-type]
        or not row_ok(o, h, low, c, prev, int(vol))  # type: ignore[arg-type]
    ):
        return None
    return {
        "date": day, "isin": isin, "symbol": symbol, "series": series, "open": o,
        "high": h, "low": low, "close": c, "prev_close": prev, "volume": int(vol),  # type: ignore[arg-type]
        "traded_value": value,
    }  # fmt: skip


def parse(day: date, data: bytes) -> ParsedDay:
    """One zip as rows of `BAR_SCHEMA`, one per ISIN (the best-ranked series)."""
    fmt = format_for(day)
    cols, parse_date = (_OLD, _old_date) if fmt == "old" else (_UDIFF, _iso_date)
    out = ParsedDay(day)
    best: dict[str, dict] = {}
    for raw in csv.DictReader(io.StringIO(_csv_text(data))):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        series = row.get(cols["series"], "").upper()
        if series not in EQUITY_SERIES:
            out.skipped_series += 1
            continue
        if parse_date(row.get(cols["date"], "")) != day:
            out.wrong_date += 1
            continue
        bar = _bar(row, cols, day, series)
        if bar is None:
            out.invalid += 1
            continue
        held = best.get(bar["isin"])
        if held is not None:
            out.other_series += 1  # one of the two series is left out either way
        if held is None or series_rank(series) < series_rank(held["series"]):
            best[bar["isin"]] = bar
    out.rows = sorted(best.values(), key=lambda r: r["isin"])
    return out


def _day_of(name: str) -> date | None:
    m = re.fullmatch(r"cm(\d{2})([A-Z]{3})(\d{4})bhav\.csv\.zip", name)
    if m:
        return date(int(m[3]), _MONTHS.index(m[2]) + 1, int(m[1]))
    m = re.fullmatch(r"BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv\.zip", name)
    if m:
        return date(int(m[1][:4]), int(m[1][4:6]), int(m[1][6:]))
    return None


@dataclass
class BuildReport:
    days: int = 0
    rows: int = 0
    already: int = 0
    invalid: int = 0
    failed: list[str] = field(default_factory=list)


def build(layout: Layout, *, rebuild: bool = False) -> BuildReport:
    """Parse every downloaded zip without a parquet day file into `bars/date=YYYY-MM-DD.parquet`."""
    report = BuildReport()
    for path in sorted(layout.bhavcopy.glob("*.zip")):
        day = _day_of(path.name)
        if day is None:
            continue
        target = layout.bars / f"date={day.isoformat()}.parquet"
        if target.exists() and not rebuild:
            report.already += 1
            continue
        try:
            parsed = parse(day, path.read_bytes())
        except (ValueError, zipfile.BadZipFile) as exc:
            report.failed.append(f"{path.name}: {exc}")
            continue
        frame = pl.DataFrame(parsed.rows, schema=BAR_SCHEMA)
        atomic_write(target, lambda tmp, f=frame: f.write_parquet(tmp))
        report.days += 1
        report.rows += frame.height
        report.invalid += parsed.invalid
    return report
