"""Read access to the parsed raw bars (`bars/date=*.parquet`) through DuckDB."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import duckdb

from .layout import Layout

EDGE_DAYS = 10  # calendar days of bars kept at each end of an ISIN's span


@dataclass(frozen=True)
class Bar:
    isin: str
    symbol: str
    day: date
    open: float
    close: float
    prev_close: float | None


@dataclass(frozen=True)
class Span:
    """One ISIN's run of daily bars."""

    isin: str
    first_day: date
    last_day: date
    first_symbol: str
    last_symbol: str
    first_prev_close: float | None
    head: tuple[Bar, ...]
    tail: tuple[Bar, ...]

    @property
    def first_open(self) -> float:
        return self.head[0].open

    @property
    def last_close(self) -> float:
        return self.tail[-1].close


def connect(layout: Layout) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"CREATE VIEW bars AS SELECT * FROM read_parquet('{layout.bars_glob()}')")
    return con


def has_bars(layout: Layout) -> bool:
    return any(layout.bars.glob("date=*.parquet"))


def sessions(layout: Layout) -> list[date]:
    """Every day with a bhavcopy: the exchange sessions the archive proves."""
    return sorted(
        date.fromisoformat(p.stem.removeprefix("date=")) for p in layout.bars.glob("date=*.parquet")
    )


_SPANS = """
WITH s AS (SELECT isin, min(date) f, max(date) l FROM bars GROUP BY isin)
SELECT s.isin, s.f, s.l, a.symbol, b.symbol, a.prev_close
FROM s JOIN bars a ON a.isin = s.isin AND a.date = s.f
       JOIN bars b ON b.isin = s.isin AND b.date = s.l
"""
_EDGES = """
WITH s AS (SELECT isin, min(date) f, max(date) l FROM bars GROUP BY isin)
SELECT r.isin, r.symbol, r.date, r.open, r.close, r.prev_close FROM bars r JOIN s USING (isin)
WHERE r.date - s.f <= ? OR s.l - r.date <= ?
ORDER BY r.isin, r.date
"""


def spans(layout: Layout) -> list[Span]:
    with connect(layout) as con:
        rows = con.execute(_SPANS).fetchall()
        edges = con.execute(_EDGES, [EDGE_DAYS, EDGE_DAYS]).fetchall()
    by_isin: dict[str, list[Bar]] = defaultdict(list)
    for isin, symbol, day, o, c, p in edges:
        by_isin[isin].append(Bar(isin, symbol, day, o, c, p))
    out = []
    for isin, first, last, fsym, lsym, prev in rows:
        own = by_isin[isin]
        head = tuple(b for b in own if (b.day - first).days <= EDGE_DAYS)
        tail = tuple(b for b in own if (last - b.day).days <= EDGE_DAYS)
        out.append(Span(isin, first, last, fsym, lsym, prev, head, tail))
    return sorted(out, key=lambda s: s.isin)


def symbol_runs(layout: Layout) -> list[tuple[str, str, date, date]]:
    """(symbol, isin, first day, last day) for every symbol an ISIN traded under."""
    with connect(layout) as con:
        return con.execute(
            "SELECT symbol, isin, min(date), max(date) FROM bars GROUP BY 1, 2 ORDER BY 1, 3"
        ).fetchall()


def window(layout: Layout, wanted: Iterable[tuple[str, date, date]]) -> dict[str, list[Bar]]:
    """Bars of each ISIN inside its [from, to] windows, oldest first, in one scan."""
    keys = list(wanted)
    out: dict[str, list[Bar]] = defaultdict(list)
    if not keys:
        return out
    with connect(layout) as con:
        con.execute("CREATE TEMP TABLE want (isin VARCHAR, f DATE, t DATE)")
        con.executemany("INSERT INTO want VALUES (?, ?, ?)", keys)
        rows = con.execute(
            "SELECT DISTINCT b.isin, b.symbol, b.date, b.open, b.close, b.prev_close "
            "FROM bars b JOIN want w ON b.isin = w.isin AND b.date BETWEEN w.f AND w.t "
            "ORDER BY b.isin, b.date"
        ).fetchall()
    for isin, symbol, day, o, c, p in rows:
        out[isin].append(Bar(isin, symbol, day, o, c, p))
    return out
