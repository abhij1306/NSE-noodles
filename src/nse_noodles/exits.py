"""The demerger exit calendar: when a position held into a demerger should be sold, point in time.

On a demerger's ex-date the parent opens lower by the value of the resulting companies. The
adjusted series shows no drop (the factor removes it), raw prices show a crash: a backtest on
the adjusted series holds through, an engine on raw prices stops out at a false loss and never
counts the resulting shares. To treat both alike, a position held into a demerger exits at the
close of the last trading session before the ex-date (`exit_session`), but only when the record
date was public before that session (`exit_status`: `announced_on`, the first filing naming the
record date that held, strictly before the exit session; a filing on the session itself is too
late, an unknown date never qualifies). Otherwise the position holds through and the factor
adjusts it once available.

Under T+1 settlement the ex-date is the record date, so a sale on the last session before it
settles on the record date and the buyer gets the resulting shares (the sale price includes
them); a sale on the ex-date still gets them.

Outputs (`write`): `out/demerger_exits.csv`, every demerger of NSE's feed, and with `as_of` D
`out/demerger_exits_asof=D.csv`, only those announced by D, with the trading calendar and ISIN
links known by D. Columns: chain_isin, isin, symbol, ex_date, record_date, announced_on,
notice_source, notice_url, exit_session, exitable, reason.

The trading calendar is the days with a bhavcopy; past the last of them (an ex-date still to
come) a weekday that no listed holiday names (`listed_holidays`).
"""

from __future__ import annotations

import json
from bisect import bisect_left
from collections.abc import Collection, Sequence
from datetime import date, timedelta
from pathlib import Path

import duckdb
import polars as pl

from .adjusted import chain_map_as_of
from .bars import sessions as bar_sessions
from .curation import read_holidays
from .layout import Layout, atomic_write
from .parse import nse_date

LOOKBACK_DAYS = 30  # with no bars at all, how far back from an ex-date a session is looked for
COLUMNS = {
    "chain_isin": pl.Utf8,
    "isin": pl.Utf8,
    "symbol": pl.Utf8,
    "ex_date": pl.Date,
    "record_date": pl.Date,
    "announced_on": pl.Date,
    "notice_source": pl.Utf8,
    "notice_url": pl.Utf8,
    "exit_session": pl.Date,
    "exitable": pl.Boolean,
    "reason": pl.Utf8,
}


def listed_holidays(layout: Layout, holidays_file: str | None = None) -> set[date]:
    """The trading holidays known: `curation/holidays.txt`, an extra list, and NSE's current
    year's list (`raw/lists/holidays.json`) when pulled."""
    listed = read_holidays(layout.root / "curation" / "holidays.txt")
    if holidays_file:
        listed |= read_holidays(Path(holidays_file))
    live = layout.lists / "holidays.json"
    if live.exists():
        try:
            for row in json.loads(live.read_text(encoding="utf-8")).get("CM", []):
                day = nse_date(str(row.get("tradingDate", "")))
                if day:
                    listed.add(day)
        except (ValueError, AttributeError):
            pass
    return listed


def exit_session(
    sessions: Sequence[date], holidays: Collection[date], ex_date: date
) -> date | None:
    """The last trading session before `ex_date`: from the sorted `sessions` (days with a
    bhavcopy) when the ex-date is within them; past the last of them, the last weekday before
    the ex-date that is not in `holidays` (a future session is known only from the calendar),
    else the last session. None when no session comes before the ex-date."""
    floor = sessions[-1] if sessions else ex_date - timedelta(days=LOOKBACK_DAYS)
    if ex_date > floor:
        day = ex_date - timedelta(days=1)
        while day > floor:
            if day.weekday() < 5 and day not in holidays:
                return day
            day -= timedelta(days=1)
        return sessions[-1] if sessions else None
    at = bisect_left(sessions, ex_date)
    return sessions[at - 1] if at else None


def exit_status(
    exit_on: date | None, announced_on: date | None, record_date: date | None = None
) -> tuple[bool, str]:
    """(exitable, reason when not): exitable only when the record date was announced strictly
    before the exit session. Announced on the exit session is too late (the decision to sell at
    its close is taken before it); unknown is never exitable."""
    if exit_on is None:
        return False, "no_session_before_ex_date"
    if announced_on is None:
        return False, "record_date_unknown" if record_date is None else "announced_on_unknown"
    if announced_on == exit_on:
        return False, "announced_on_exit_session"
    if announced_on > exit_on:
        return False, "announced_after_exit_session"
    return True, ""


def events(rows: Sequence[dict]) -> list[dict]:
    """One row per (isin, ex-date) of the feed's demerger rows (NSE repeats an event under
    several purpose texts): the earliest `announced_on` and its notice; the record date only
    when the rows agree on it."""
    out: dict[tuple[str, date], dict] = {}
    for r in sorted(rows, key=lambda r: (r["announced_on"] or date.max, r["symbol"])):
        if r["ex_date"] is None:
            continue
        key = (r["isin"], r["ex_date"])
        if key not in out:
            out[key] = {k: r[k] for k in ("isin", "symbol", "ex_date", "record_date")} | {
                k: r[k] for k in ("announced_on", "notice_source", "notice_url")
            }
        elif out[key]["record_date"] != r["record_date"]:
            out[key]["record_date"] = None
    return [out[k] for k in sorted(out, key=lambda k: (k[1], k[0]))]


def calendar(
    rows: Sequence[dict],
    sessions: Sequence[date],
    holidays: Collection[date],
    chain_of: dict[str, str],
    as_of: date | None = None,
) -> pl.DataFrame:
    """The exit calendar of the demerger `events`. With `as_of`, only events announced by then
    (an unknown announcement never is)."""
    out = []
    for e in events(rows):
        if as_of is not None and (e["announced_on"] is None or e["announced_on"] > as_of):
            continue
        exit_on = exit_session(sessions, holidays, e["ex_date"])
        ok, reason = exit_status(exit_on, e["announced_on"], e["record_date"])
        out.append(
            {
                "chain_isin": chain_of.get(e["isin"], e["isin"]),
                **e,
                "exit_session": exit_on,
                "exitable": ok,
                "reason": reason,
            }
        )
    return pl.DataFrame(out, schema=COLUMNS).select(list(COLUMNS))


def write(
    layout: Layout, holidays_file: str | None = None, as_of: date | None = None
) -> dict[str, int]:
    """Write the exit calendar (all of it, or as of the close of `as_of`); returns counts."""
    with duckdb.connect(str(layout.db), read_only=True) as con:
        has = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'demergers'"
        ).fetchone()
        rows = con.sql("SELECT * FROM demergers").pl().to_dicts() if has and has[0] else []
        if as_of is None:
            cmap = con.sql("SELECT isin, chain_isin FROM chain_map").pl()
        else:
            links = con.sql(
                "SELECT old_isin, new_isin, known_on FROM lineage WHERE status = 'linked'"
            ).pl()
            cmap = chain_map_as_of(links, sorted({r["isin"] for r in rows}), as_of)
    for r in rows:  # a demergers table built before `notice_source` existed
        r.setdefault("notice_source", "")
    sessions = bar_sessions(layout)
    if as_of is not None:
        sessions = [d for d in sessions if d <= as_of]
    frame = calendar(
        rows, sessions, listed_holidays(layout, holidays_file), dict(cmap.iter_rows()), as_of
    )
    name = "demerger_exits.csv" if as_of is None else f"demerger_exits_asof={as_of}.csv"
    atomic_write(layout.out / name, lambda tmp: frame.write_csv(tmp))
    return {
        "events": frame.height,
        "exitable": int(frame["exitable"].sum()),
        **{
            f"not_exitable:{k}": v
            for k, v in frame.filter(~pl.col("exitable")).group_by("reason").len().rows()
        },
    }
