"""Where a run keeps its files, under the data directory the user passes (`--data`).

    raw/bhavcopy/<NSE file name>.zip      untouched downloads, never rewritten
    raw/indices/ind_close_all_DDMMYYYY.csv
    raw/nse/<dataset>/<index>/YYYY-MM.json one month of an NSE JSON endpoint
    raw/lists/symbolchange.csv, namechange.csv, holidays.json
    raw/not_found.json                    archive days NSE answered 404 for (holiday candidates)
    bars/date=YYYY-MM-DD.parquet          parsed bhavcopy, one row per ISIN and day
    index/date=YYYY-MM-DD.parquet         parsed index levels
    noodles.duckdb                        corporate actions, lineage, factor versions, review lists
    ledger/factor_versions.jsonl          every factor version ever produced, append-only
    out/                                  the published outputs (series, reports)

Everything but `raw/` and `ledger/` is rebuilt from them; the ledger holds when each factor
version was first observed, which no rebuild can recreate. Every other file is written to a
temporary name and renamed, so an interrupted run never leaves half a file and a rerun skips
what is already there.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


def atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(name)
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_bytes(path: Path, data: bytes) -> None:
    atomic_write(path, lambda tmp: tmp.write_bytes(data))


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def bhavcopy(self) -> Path:
        return self.raw / "bhavcopy"

    @property
    def indices_raw(self) -> Path:
        return self.raw / "indices"

    def nse(self, dataset: str, index: str) -> Path:
        return self.raw / "nse" / dataset / index

    @property
    def lists(self) -> Path:
        return self.raw / "lists"

    @property
    def bars(self) -> Path:
        return self.root / "bars"

    @property
    def index(self) -> Path:
        return self.root / "index"

    @property
    def db(self) -> Path:
        return self.root / "noodles.duckdb"

    @property
    def ledger(self) -> Path:
        return self.root / "ledger" / "factor_versions.jsonl"

    @property
    def out(self) -> Path:
        return self.root / "out"

    def bars_glob(self) -> str:
        return str(self.bars / "date=*.parquet")

    def bars_glob_sql(self) -> str:
        """`bars_glob` as a SQL string literal (a quote in the data path is doubled)."""
        return "'" + self.bars_glob().replace("'", "''") + "'"

    def index_glob(self) -> str:
        return str(self.index / "date=*.parquet")


RECENT_DAYS = 5


class NotFoundMemo:
    """Days an archive answered 404 for, per kind (`bhavcopy`, `indices`), so a resume does
    not ask again. `--recheck` forgets them."""

    def __init__(self, path: Path, today: date | None = None) -> None:
        self.path = path
        self.today = today or date.today()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        self.days: dict[str, set[date]] = {
            k: {date.fromisoformat(d) for d in v} for k, v in raw.items()
        }

    def has(self, kind: str, day: date) -> bool:
        return day in self.days.get(kind, set())

    def add(self, kind: str, day: date) -> None:
        """Remember a 404, unless the day is recent: NSE posts a day's files in the evening, so
        a 404 within `RECENT_DAYS` may be a file not yet published and is asked again."""
        if day > self.today - timedelta(days=RECENT_DAYS):
            return
        self.days.setdefault(kind, set()).add(day)
        self.save()

    def discard(self, kind: str, day: date) -> None:
        self.days.get(kind, set()).discard(day)

    def save(self) -> None:
        payload = {k: sorted(d.isoformat() for d in v) for k, v in self.days.items()}
        atomic_write(self.path, lambda tmp: tmp.write_text(json.dumps(payload), encoding="utf-8"))
