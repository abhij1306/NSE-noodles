"""The checks a human reads: the jump report, the session / holiday coverage check and the
combined review list (all written under `out/`, all also printed as counts).

- Jumps: a session where the adjusted open is more than 25% away from the previous adjusted
  close (within 14 calendar days). NSE's widest band is 20%, so an unexplained jump is almost
  always a missing or wrong factor (split, bonus, demerger, capital reduction, ETF unit split)
  or a relisting; a few are genuine moves (a huge special dividend, a tick-size artefact on a
  penny stock, an insolvency relisting). `triage-jumps` classifies them.
- Coverage: every weekday is downloaded, an explained holiday, a gap (NSE had no file and no
  holiday list names the day) or unaccounted (never asked, or the request failed). Weekend
  sessions (Budget Saturdays, Muhurat) are listed. Index days missing after the first index
  day are listed too.
- Adjustment log: every event at its hindsight version with when the market first heard of it
  (`announced_on`), its ex-date, when that version could first be used (`available_at`,
  `available_basis`) and the sessions between ex-date and then (`lag_sessions`).
- Factor versions: every version of every event, with its value, basis, timestamps and band.
- Demerger exits: the session a position held into each demerger exits at, or why it cannot
  (`exits`).
"""

from __future__ import annotations

import json
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta

import duckdb
import polars as pl

from . import exits
from .bars import sessions as bar_sessions
from .factors import JUMP_TOL
from .layout import Layout, NotFoundMemo, atomic_write

JUMP_MAX_DAYS = 14  # a relisting after a longer suspension may jump on its own
HINT_DAYS = 7
REVIEW_SQL = """
SELECT 'actions' AS list, "table", symbol, isin, ex_date AS day, reason, text, hint, evidence
FROM review
UNION ALL
SELECT 'lineage', 'lineage', json_extract_string(evidence, '$.new_symbol'),
       old_isin || '>' || new_isin, effective, reason, '', '', evidence
FROM lineage WHERE status = 'review'
ORDER BY day DESC
"""


def _by_key(rows: list[tuple]) -> dict[tuple[str, str], list[tuple[date, str]]]:
    """(symbol, isin, ex_date, text) rows indexed by symbol and by ISIN, so each jump reads
    only its own rows."""
    out: dict[tuple[str, str], list[tuple[date, str]]] = defaultdict(list)
    for sym, isin, ex, text in rows:
        if ex:
            out[("s", sym)].append((ex, text))
            out[("i", isin)].append((ex, text))
    return out


def jumps(layout: Layout) -> pl.DataFrame:
    adj = pl.scan_parquet(layout.out / "hindsight_daily.parquet")
    found = (
        adj.select(
            "chain_isin", "isin", "symbol", "date", "adj_open", "adj_close", "ordinary_equity"
        )
        .sort("chain_isin", "date")
        .with_columns(
            pl.col("adj_close").shift(1).over("chain_isin").alias("previous_close"),
            pl.col("date").shift(1).over("chain_isin").alias("previous_day"),
        )
        .filter(
            pl.col("previous_close").is_not_null()
            & ((pl.col("date") - pl.col("previous_day")).dt.total_days() <= JUMP_MAX_DAYS)
            & ((pl.col("adj_open") / pl.col("previous_close") - 1).abs() > JUMP_TOL)
        )
        .with_columns((pl.col("adj_open") / pl.col("previous_close")).alias("ratio"))
        .collect()
    )
    kind = (
        pl.when(pl.col("isin").str.starts_with("INF"))
        .then(pl.lit("fund_unit"))
        .when(pl.col("isin").str.slice(7, 2) == "20")
        .then(pl.lit("rights_entitlement"))
        .when(pl.col("ordinary_equity"))
        .then(pl.lit("equity"))
        .otherwise(pl.lit("other"))
    )
    found = found.with_columns(kind.alias("security"))
    # hints: what NSE's feed and the review list say near the day
    with duckdb.connect(str(layout.db), read_only=True) as con:
        feed = (
            con.execute("SELECT symbol, isin, ex_date, subject FROM ca_feed").fetchall()
            if _has(con, "ca_feed")
            else []
        )
        review = (
            con.execute("SELECT symbol, isin, ex_date, reason FROM review").fetchall()
            if _has(con, "review")
            else []
        )
    feed_by, review_by = _by_key(feed), _by_key(review)
    feed_hint, review_hint = [], []
    for row in found.iter_rows(named=True):
        lo, hi = row["date"] - timedelta(days=HINT_DAYS), row["date"] + timedelta(days=HINT_DAYS)
        keys = (("s", row["symbol"]), ("i", row["isin"]))
        f = {s for k in keys for ex, s in feed_by.get(k, ()) if lo <= ex <= hi}
        r = {s for k in keys for ex, s in review_by.get(k, ()) if lo <= ex <= hi}
        feed_hint.append(" | ".join(sorted(f))[:300])
        review_hint.append(",".join(sorted(r)))
    found = found.with_columns(
        pl.Series("feed_near", feed_hint, pl.Utf8), pl.Series("review_near", review_hint, pl.Utf8)
    )
    return found.sort("date", descending=True)


def _has(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return bool(row and row[0])


COVERAGE_LISTS = (
    "holiday",
    "gap",
    "unaccounted",
    "on_listed_holiday",
    "weekend_session",
    "index_missing",
)


def _day_status(day: date, have: set[date], listed: set[date], memo: NotFoundMemo) -> str | None:
    """The coverage list a calendar day belongs on, or None for an ordinary day."""
    if day.weekday() >= 5:
        return "weekend_session" if day in have else None
    if day in have:
        return "on_listed_holiday" if day in listed else None
    if memo.has("bhavcopy", day):
        return "holiday" if day in listed else "gap"
    if not (date(2024, 7, 5) < day < date(2024, 7, 8)):
        return "unaccounted"
    return None


def _index_missing(layout: Layout, sessions: list[date]) -> list[str]:
    """Sessions from the first index day on that have no index file."""
    index_days = sorted(
        date.fromisoformat(p.stem.removeprefix("date="))
        for p in layout.index.glob("date=*.parquet")
    )
    if not index_days:
        return []
    have_index = set(index_days)
    return [d.isoformat() for d in sessions if d >= index_days[0] and d not in have_index]


def coverage(layout: Layout, holidays_file: str | None = None) -> dict[str, list[str]]:
    sessions = bar_sessions(layout)
    if not sessions:
        return {}
    memo = NotFoundMemo(layout.raw / "not_found.json")
    listed = exits.listed_holidays(layout, holidays_file)
    have = set(sessions)
    out: dict[str, list[str]] = {k: [] for k in COVERAGE_LISTS}
    day = sessions[0]
    while day <= sessions[-1]:
        status = _day_status(day, have, listed, memo)
        if status:
            out[status].append(day.isoformat())
        day += timedelta(days=1)
    out["index_missing"] = _index_missing(layout, sessions)
    return out


LOG_SQL = """
SELECT chain_isin, isin, symbol, action_type, version, value AS factor, cash, basis, method,
       source, announced_on, notice_url, ex_date, source_published_at, first_observed_at,
       available_at, available_basis, text
FROM actions ORDER BY ex_date, chain_isin, action_type
"""
VERSIONS_SQL = """
SELECT chain_isin, isin, symbol, ex_date, action_type, version, status, value, cash, basis,
       raw_gap, band_low, band_high, source_published_at, first_observed_at, available_at,
       available_basis, method, version_id
FROM factor_versions ORDER BY ex_date, chain_isin, action_type, version
"""


def lag_sessions(sessions: list[date], ex: date, known: datetime | None) -> int | None:
    """Sessions from the ex-date's session to the session of `known` (0: known on the ex-date
    itself); None when `known` is unknown."""
    if known is None:
        return None
    return max(0, bisect_left(sessions, known.date()) - bisect_left(sessions, ex))


def adjustment_log(layout: Layout) -> pl.DataFrame:
    with duckdb.connect(str(layout.db), read_only=True) as con:
        log = con.sql(LOG_SQL).pl()
    sessions = bar_sessions(layout)
    lags = [
        lag_sessions(sessions, e, k)
        for e, k in zip(log["ex_date"], log["available_at"], strict=True)
    ]
    return log.with_columns(pl.Series("lag_sessions", lags, dtype=pl.Int64))


def write_all(layout: Layout, holidays_file: str | None = None) -> dict[str, object]:
    j = jumps(layout)
    atomic_write(layout.out / "jumps.csv", lambda tmp: j.write_csv(tmp))
    cov = coverage(layout, holidays_file)
    atomic_write(
        layout.out / "coverage.json",
        lambda tmp: tmp.write_text(json.dumps(cov, indent=1), encoding="utf-8"),
    )
    with duckdb.connect(str(layout.db), read_only=True) as con:
        review = con.sql(REVIEW_SQL).pl()
    atomic_write(layout.out / "review.csv", lambda tmp: review.write_csv(tmp))
    log = adjustment_log(layout)
    atomic_write(layout.out / "adjustment_log.csv", lambda tmp: log.write_csv(tmp))
    with duckdb.connect(str(layout.db), read_only=True) as con:
        all_versions = con.sql(VERSIONS_SQL).pl()
    atomic_write(layout.out / "factor_versions.csv", lambda tmp: all_versions.write_csv(tmp))
    demerger_exits = exits.write(layout, holidays_file)
    by_security = j.group_by("security").len().sort("security").rows() if j.height else []
    return {
        "jumps": j.height,
        "jumps_by_security": dict(by_security),
        "review": review.height,
        "coverage": {k: len(v) for k, v in cov.items()},
        "adjustment_log": {
            "rows": log.height,
            "by_available_basis": dict(
                log.group_by("available_basis").len().sort("available_basis").rows()
            ),
            "available_after_ex_date": int((log["lag_sessions"] > 0).sum()),
            "available_at_unknown": int(log["available_at"].is_null().sum()),
            "announced_on_found": int(log["announced_on"].is_not_null().sum()),
        },
        "demerger_exits": demerger_exits,
        "factor_versions": {
            "rows": all_versions.height,
            "events_with_several": int(
                (
                    all_versions.group_by("chain_isin", "ex_date", "action_type").len()["len"] > 1
                ).sum()
            ),
            "disputed": int((all_versions["status"] == "disputed").sum()),
        },
    }
