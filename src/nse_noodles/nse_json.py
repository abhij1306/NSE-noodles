"""NSE's website JSON endpoints, pulled in monthly shards: corporate actions (splits, bonuses,
rights, demergers, dividends, all in one feed), announcements and board meetings, plus the
symbol-change, name-change and holiday lists.

Each month is one file, `raw/nse/<dataset>/<index>/YYYY-MM.json` (the JSON list the endpoint
answered, concatenated over the month's request chunks), written only when every chunk of the
month succeeded. A rerun skips months already on disk, so a pull resumes where it stopped; the
current month is fetched again until it has ended (`--refresh-current`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from .http import Fetched, NseClient
from .layout import Layout, write_bytes

logger = logging.getLogger(__name__)

API = "https://www.nseindia.com/api/"
DATASETS = {
    # dataset: (endpoint, days per request inside a month)
    "corporate_actions": ("corporates-corporateActions", 31),
    "announcements": ("corporate-announcements", 7),  # busy: weekly requests
    "board_meetings": ("corporate-board-meetings", 31),
}
INDEXES = ("equities", "sme")
LIST_URLS = {
    "symbolchange.csv": "https://nsearchives.nseindia.com/content/equities/symbolchange.csv",
    "namechange.csv": "https://nsearchives.nseindia.com/content/equities/namechange.csv",
    "holidays.json": API + "holiday-master?type=trading",
}


def months(start: date, end: date) -> list[tuple[date, date]]:
    """(first, last) day of each calendar month touching [start, end], clipped to it."""
    out = []
    first = start.replace(day=1)
    while first <= end:
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(first, start), min(nxt - timedelta(days=1), end)))
        first = nxt
    return out


def chunks(first: date, last: date, days: int) -> list[tuple[date, date]]:
    out, day = [], first
    while day <= last:
        stop = min(day + timedelta(days=days - 1), last)
        out.append((day, stop))
        day = stop + timedelta(days=1)
    return out


def _rows(payload: object) -> list | None:
    if isinstance(payload, dict):
        payload = payload.get("data")
    return payload if isinstance(payload, list) else None


def _decoded(got: Fetched) -> list | None:
    if got.status != "ok":
        return None
    try:
        return _rows(json.loads(got.data.decode("utf-8", errors="replace")))
    except ValueError:
        return None


def _fetch(
    client: NseClient, endpoint: str, step: int, index: str, first: date, last: date
) -> tuple[list, str | None]:
    """Every row from `first` to `last` in `step`-day chunks, and the first chunk that failed."""
    rows: list = []
    for a, b in chunks(first, last, step):
        params = {"index": index, "from_date": f"{a:%d-%m-%Y}", "to_date": f"{b:%d-%m-%Y}"}
        found = _decoded(client.get(API + endpoint, params, cookies=True))
        if found is None:
            return rows, f"{a}..{b}"
        rows.extend(found)
    return rows, None


@dataclass
class PullReport:
    months_written: int = 0
    months_skipped: int = 0
    rows: int = 0
    failed: list[str] = field(default_factory=list)


def pull(
    layout: Layout,
    client: NseClient,
    dataset: str,
    start: date,
    end: date,
    index: str = "equities",
    *,
    refresh_current: bool = False,
    today: date | None = None,
) -> PullReport:
    endpoint, step = DATASETS[dataset]
    today = today or date.today()
    report = PullReport()
    folder = layout.nse(dataset, index)
    for first, last in months(start, end):
        path = folder / f"{first:%Y-%m}.json"
        whole_month = first.day == 1 and (last + timedelta(days=1)).day == 1
        current = last >= today.replace(day=1)
        if path.exists() and whole_month and not (current and refresh_current):
            report.months_skipped += 1
            continue
        rows, failed = _fetch(client, endpoint, step, index, first, last)
        if failed is not None:
            report.failed.append(f"{dataset}/{index} {failed}")
            continue
        if not whole_month:  # a partial month is named by its range, never mistaken for a full one
            path = folder / f"{first:%Y-%m}_{first.day:02d}-{last.day:02d}.json"
        write_bytes(path, json.dumps(rows, ensure_ascii=False).encode("utf-8"))
        report.months_written += 1
        report.rows += len(rows)
    return report


def pull_lists(layout: Layout, client: NseClient) -> dict[str, str]:
    """The symbol-change and name-change CSVs and this year's holiday list (NSE publishes only
    the current year's; past years come from your own list, see `coverage`)."""
    out = {}
    for name, url in LIST_URLS.items():
        got = client.get(url, cookies=url.startswith(API))
        if got.status == "ok":
            write_bytes(layout.lists / name, got.data)
        out[name] = got.status
    return out


def read_shards(layout: Layout, dataset: str, index: str = "equities") -> list[dict]:
    """Every row of every shard of a dataset, de-duplicated (a full-month file wins over a
    partial one of the same month; identical rows count once)."""
    folder = layout.nse(dataset, index)
    files = sorted(folder.glob("*.json"))
    full = {p.stem for p in files if len(p.stem) == 7}
    seen: set[str] = set()
    out = []
    for path in files:
        if len(path.stem) > 7 and path.stem[:7] in full:
            continue
        for row in json.loads(path.read_text(encoding="utf-8")):
            key = json.dumps(row, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out
