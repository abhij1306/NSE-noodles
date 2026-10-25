"""NSE daily bhavcopy archives: file names, the polite resumable download, and the parser.

Two formats cover 22 Jun 2011 to today: the old CM bhavcopy up to 5 Jul 2024 and UDiFF from
8 Jul 2024. Files before 22 Jun 2011 have no ISIN column, so no row can be keyed to a security
across symbol changes: the history starts there.

Every security that traded on a day is in that day's file, delisted ones included, so a
history built from the archives (and never filtered by today's listing) is survivorship-bias
free by construction.

A file's format is read from its header, never assumed from its date: a file in neither known
format (NSE changed it) fails the build loudly instead of parsing as empty rows. When NSE only
renames UDiFF columns, `[bhavcopy_columns]` in noodles.toml names the new headers (`renamed`):
a header with them is read as a third format, its dates in any of the usual spellings.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from .config import DEFAULT, FIRST_ISIN_DAY, Config
from .http import NseClient
from .layout import Layout, NotFoundMemo, atomic_write, write_bytes
from .parse import ISIN_RE

logger = logging.getLogger(__name__)

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


def archive_url(day: date, cfg: Config = DEFAULT) -> str:
    return cfg.url("bhavcopy", day, archive_name(day), old=format_for(day) == "old")


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
    layout: Layout,
    client: NseClient,
    days: Iterable[date],
    *,
    recheck: bool = False,
    cfg: Config = DEFAULT,
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
        got = client.get(archive_url(day, cfg))
        if got.status == "ok" and readable(got.data, cfg.bhavcopy_columns):
            write_bytes(path, got.data)
            memo.discard("bhavcopy", day)
            report.downloaded += 1
        elif got.status == "not_found":
            memo.add("bhavcopy", day)
            report.not_found.append(day)
        else:  # an error, or a body that is no bhavcopy (a block page): asked again next run
            report.errors.append(day)
    memo.save()
    return report


# --- parsing ------------------------------------------------------------------------------------

_OLD = {
    "date": "TIMESTAMP", "isin": "ISIN", "series": "SERIES", "symbol": "SYMBOL", "open": "OPEN",
    "high": "HIGH", "low": "LOW", "close": "CLOSE", "prev_close": "PREVCLOSE",
    "volume": "TOTTRDQTY", "traded_value": "TOTTRDVAL",
}  # fmt: skip
_UDIFF = {
    "date": "TradDt", "isin": "ISIN", "series": "SctySrs", "symbol": "TckrSymb",
    "open": "OpnPric", "high": "HghPric", "low": "LwPric", "close": "ClsPric",
    "prev_close": "PrvsClsgPric", "volume": "TtlTradgVol", "traded_value": "TtlTrfVal",
}  # fmt: skip
_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d", "%d%m%Y")


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


def _any_date(raw: str) -> date | None:
    """A renamed format's date: the first of the usual spellings that reads."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
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


Renamed = Mapping[str, str]  # field (`config.BHAVCOPY_FIELDS`) -> NSE's new header


def columns(
    header: Iterable[str] | None, renamed: Renamed | None = None
) -> tuple[dict[str, str], Callable[[str], date | None]]:
    """The column names of the format a header is in, and how it writes a date; ValueError for
    a header in no known format (nor UDiFF with the `renamed` headers)."""
    got = {(h or "").strip() for h in header or ()}
    formats = [(_OLD, _old_date), (_UDIFF, _iso_date)]
    if renamed:
        formats.append((_UDIFF | dict(renamed), _any_date))
    for cols, parse_date in formats:
        if set(cols.values()) <= got:
            return cols, parse_date
    raise ValueError(
        f"not a known bhavcopy format (columns {sorted(got)[:12]}); if NSE only renamed "
        "columns, name them in [bhavcopy_columns] in noodles.toml"
    )


def readable(data: bytes, renamed: Renamed | None = None) -> bool:
    """A zip of one CSV in a known bhavcopy format."""
    try:
        text = _csv_text(data)
        columns(next(csv.reader(io.StringIO(text)), None), renamed)
    except (ValueError, zipfile.BadZipFile):
        return False
    return True


def trade_day(data: bytes, renamed: Renamed | None = None) -> date:
    """The day a zip's rows are dated (the most common date in it); ValueError when the zip is
    no bhavcopy or names no day."""
    reader = csv.DictReader(io.StringIO(_csv_text(data)))
    cols, parse_date = columns(reader.fieldnames, renamed)
    days = Counter(parse_date(row.get(cols["date"]) or "") for row in reader)
    days.pop(None, None)
    if not days:
        raise ValueError("no row names a trading day")
    return days.most_common(1)[0][0]  # type: ignore[return-value]


def _bar(row: dict[str, str], cols: dict[str, str], day: date, series: str) -> dict | None:
    """The row as a `BAR_SCHEMA` bar, or None when a field is missing or fails `row_ok`."""
    isin = row.get(cols["isin"], "").upper()
    o, h, low, c = (_num(row.get(cols[k])) for k in ("open", "high", "low", "close"))
    prev, value = _num(row.get(cols["prev_close"])), _num(row.get(cols["traded_value"]))
    vol = _num(row.get(cols["volume"]))
    if prev is not None and prev <= 0:
        prev = None  # a listing day can carry PREVCLOSE 0: the bar stands without it
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


def parse(day: date, data: bytes, renamed: Renamed | None = None) -> ParsedDay:
    """One zip as rows of `BAR_SCHEMA`, one per ISIN (the best-ranked series)."""
    reader = csv.DictReader(io.StringIO(_csv_text(data)))
    cols, parse_date = columns(reader.fieldnames, renamed)
    out = ParsedDay(day)
    best: dict[str, dict] = {}
    for raw in reader:
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


def day_of(name: str) -> date | None:
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
    wrong_date: int = 0
    failed: list[str] = field(default_factory=list)


def build(layout: Layout, *, rebuild: bool = False, renamed: Renamed | None = None) -> BuildReport:
    """Parse every downloaded zip without a parquet day file into `bars/date=YYYY-MM-DD.parquet`."""
    report = BuildReport()
    for path in sorted(layout.bhavcopy.glob("*.zip")):
        day = day_of(path.name)
        if day is None:
            continue
        target = layout.bars / f"date={day.isoformat()}.parquet"
        if target.exists() and not rebuild:
            report.already += 1
            continue
        try:
            parsed = parse(day, path.read_bytes(), renamed)
        except (ValueError, zipfile.BadZipFile) as exc:
            report.failed.append(f"{path.name}: {exc}")
            continue
        report.wrong_date += parsed.wrong_date
        if not parsed.rows:  # a stale or renamed-column file would otherwise fake a session
            report.failed.append(
                f"{path.name}: no valid rows (wrong date {parsed.wrong_date}, "
                f"invalid {parsed.invalid}, other series {parsed.skipped_series})"
            )
            continue
        frame = pl.DataFrame(parsed.rows, schema=BAR_SCHEMA)
        atomic_write(target, lambda tmp, f=frame: f.write_parquet(tmp))
        report.days += 1
        report.rows += frame.height
        report.invalid += parsed.invalid
    return report
