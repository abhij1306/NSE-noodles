"""Where a run keeps its files, under the data directory the user passes (`--data`).

    raw/bhavcopy/<NSE file name>.zip      untouched downloads, never rewritten
    raw/indices/ind_close_all_DDMMYYYY.csv
    raw/nse/<dataset>/<index>/YYYY-MM.json one month of an NSE JSON endpoint
    raw/lists/symbolchange.csv, namechange.csv, holidays.json
    raw/not_found.json                    archive days NSE answered 404 for (holiday candidates)
    bars/date=YYYY-MM-DD.parquet          parsed bhavcopy, one row per ISIN and day
    index/date=YYYY-MM-DD.parquet         parsed index levels
    noodles.duckdb                        corporate actions, lineage, factors, review lists
    out/                                  the published outputs (adjusted series, reports)

Every file is written to a temporary name and renamed, so an interrupted run never leaves half
a file and a rerun skips what is already there.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path


def atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    write(tmp)
    os.replace(tmp, path)


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
    def out(self) -> Path:
        return self.root / "out"

    def bars_glob(self) -> str:
        return str(self.bars / "date=*.parquet")

    def index_glob(self) -> str:
        return str(self.index / "date=*.parquet")


class NotFoundMemo:
    """Days an archive answered 404 for, per kind (`bhavcopy`, `indices`), so a resume does
    not ask again. `--recheck` forgets them."""

    def __init__(self, path: Path) -> None:
        self.path = path
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
        self.days.setdefault(kind, set()).add(day)
        self.save()

    def discard(self, kind: str, day: date) -> None:
        self.days.get(kind, set()).discard(day)

    def save(self) -> None:
        payload = {k: sorted(d.isoformat() for d in v) for k, v in self.days.items()}
        atomic_write(self.path, lambda tmp: tmp.write_text(json.dumps(payload), encoding="utf-8"))
