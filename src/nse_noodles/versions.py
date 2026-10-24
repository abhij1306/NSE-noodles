"""Factor versions: every value a factor ever had, with when it could have been known. Pure
functions, no I/O except the append-only ledger (`Ledger`).

A corporate-action event is (chain, ex-date, action type). It has one or more VERSIONS, never
overwritten: a later confirmation, a correction or a deletion adds a version. Each version has

- `value` (the price factor; 1.0 for a cash event or a deletion) and `cash` (rupees per share),
- `basis`: what the value was computed from (`ex_open_gate`, `terp`, `index_neutral_gap`,
  `raw_gap`, `resulting_ratio`, `feed_amount`, `research_value`, `correction`, ...),
- `source_published_at`: when the source of its terms was published (a research row's
  `source_date`, a correction's `known_on`, the first NSE filing found for a feed row); null
  when unknown,
- `first_observed_at`: when this pipeline first produced it (from the ledger; a history built
  today is first observed today),
- `available_at`: the earliest time the value, and the rule that accepts it, could have been
  known: the latest of every price and source it reads. Null when a source it needs has no date:
  such a version never enters a point-in-time replay. A time is never moved earlier than its
  evidence: a date-only source counts from the end of its day (`end_of`).

All times are India Standard Time, stored without a zone.

Two views, never mixed:
- HINDSIGHT (`select(versions, None)`): per event, the version of highest precedence, whatever
  its availability. For research on what happened.
- POINT-IN-TIME replay (`select(versions, cutoff)`): per event, the version of highest
  precedence among those with `available_at <= cutoff` and an ex-date on or before the cutoff
  day. A version with unknown availability never qualifies. Adding a version that becomes
  available after a cutoff cannot change that cutoff's replay.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

IST = ZoneInfo("Asia/Kolkata")
# The latest an ex-date OPEN is set: NSE's special pre-open for restructured F&O stocks (order
# entry 09:00-09:45, matching to 09:55, normal market from 10:00); the regular pre-open sets
# other stocks' open by 09:15. One conservative stamp for both.
OPEN_KNOWN = time(10, 0)
# The closing price (the last half hour's weighted average) is out by the end of the post-close
# session.
CLOSE_KNOWN = time(16, 0)
EVENT = ("chain_isin", "ex_date", "action_type")
ACCEPTED, DISPUTED = "accepted", "disputed"

SCHEMA = {
    "version_id": pl.Utf8,
    "chain_isin": pl.Utf8,
    "isin": pl.Utf8,
    "symbol": pl.Utf8,
    "ex_date": pl.Date,
    "action_type": pl.Utf8,
    "version": pl.Int64,
    "status": pl.Utf8,
    "value": pl.Float64,
    "cash": pl.Float64,
    "basis": pl.Utf8,
    "raw_gap": pl.Float64,
    "band_low": pl.Float64,
    "band_high": pl.Float64,
    "method": pl.Utf8,
    "source": pl.Utf8,
    "text": pl.Utf8,
    "evidence": pl.Utf8,
    "announced_on": pl.Date,
    "notice_url": pl.Utf8,
    "source_published_at": pl.Datetime("us"),
    "first_observed_at": pl.Datetime("us"),
    "available_at": pl.Datetime("us"),
    "available_basis": pl.Utf8,
}


def at_open(day: date) -> datetime:
    return datetime.combine(day, OPEN_KNOWN)


def at_close(day: date) -> datetime:
    return datetime.combine(day, CLOSE_KNOWN)


def end_of(day: date | None) -> datetime | None:
    """A date-only time (a filing date, a correction's `known_on`): the end of that day, the
    latest moment the date allows, so it is never earlier than the truth."""
    return None if day is None else datetime.combine(day, time(23, 59, 59))


def cutoff(day: date) -> datetime:
    """`--as-of D`: after everything of day D (its bhavcopy, its filings)."""
    return datetime.combine(day, time(23, 59, 59))


def latest(*times: datetime | None) -> datetime | None:
    """The latest of the times a value depends on; None (unknown) if any is unknown."""
    if not times or any(t is None for t in times):
        return None
    return max(times)  # type: ignore[type-var]


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def version_id(v: dict) -> str:
    """The identity of a version: what it says and when it was available. The same value from
    the same evidence gets the same id on every rebuild."""
    key = [
        v["isin"],
        str(v["ex_date"]),
        v["action_type"],
        v["basis"],
        round(v["value"], 10),
        None if v["cash"] is None else round(v["cash"], 6),
        str(v["available_at"]),
        str(v["source_published_at"]),
        v["status"],
    ]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]


def number(versions: list[dict]) -> list[dict]:
    """Give each version its `version` (1, 2, ... in list order per event: the precedence) and
    its `version_id`."""
    seen: dict[tuple, int] = {}
    for v in versions:
        key = tuple(v[k] for k in EVENT)
        seen[key] = seen.get(key, 0) + 1
        v["version"] = seen[key]
        v["version_id"] = version_id(v)
    return versions


def select(versions: pl.DataFrame, at: datetime | None) -> pl.DataFrame:
    """Per event, the accepted version of highest precedence: every one (`at` None, the
    hindsight view), or only those available by `at` with an ex-date on or before its day (the
    point-in-time replay). Unknown availability never qualifies for a replay."""
    v = versions.filter(pl.col("status") == ACCEPTED)
    if at is not None:
        v = v.filter(
            pl.col("available_at").is_not_null()
            & (pl.col("available_at") <= at)
            & (pl.col("ex_date") <= at.date())
        )
    return v.sort(*EVENT, "version").group_by(EVENT, maintain_order=True).last()


def first_available(versions: pl.DataFrame) -> pl.DataFrame:
    """Per event, the earliest time any accepted version was available (null if none had a
    known time): from then on a replay applies some factor."""
    return (
        versions.filter(pl.col("status") == ACCEPTED)
        .group_by(EVENT)
        .agg(pl.col("available_at").min().alias("first_available_at"))
    )


class Ledger:
    """`ledger/factor_versions.jsonl`: every version this pipeline ever produced, one JSON line
    each, appended and never rewritten. It is the one output a rebuild cannot recreate (it holds
    `first_observed_at`): keep it with the raw downloads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: dict[str, datetime] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    t = datetime.fromisoformat(row["first_observed_at"])
                    vid = row["version_id"]
                    self.seen[vid] = min(t, self.seen.get(vid, t))

    def stamp(self, versions: Iterable[dict], now: datetime) -> tuple[list[dict], int]:
        """Set `first_observed_at` on each version (from the ledger, else `now`) and append the
        new ones. Returns (versions, how many ledger versions this build no longer produced)."""
        out, new = stamp(list(versions), self.seen, now)
        if new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                for v in new:
                    fh.write(json.dumps(v, default=str, sort_keys=True) + "\n")
            self.seen.update({v["version_id"]: now for v in new})
        produced = {v["version_id"] for v in out}
        return out, len(set(self.seen) - produced)


def stamp(
    versions: list[dict], seen: dict[str, datetime], now: datetime
) -> tuple[list[dict], list[dict]]:
    """(versions with `first_observed_at`, the ones not seen before)."""
    new = []
    for v in versions:
        v["first_observed_at"] = seen.get(v["version_id"])
        if v["first_observed_at"] is None:
            v["first_observed_at"] = now
            new.append(v)
    return versions, new


def frame(versions: list[dict]) -> pl.DataFrame:
    rows = [{k: v.get(k) for k in SCHEMA} for v in versions]
    return pl.DataFrame(rows, schema=SCHEMA) if rows else pl.DataFrame(schema=SCHEMA)
