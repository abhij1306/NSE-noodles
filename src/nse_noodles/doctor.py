"""`noodles doctor`: is each NSE source still where the settings say, in a format noodles reads?

A few requests per source (a daily archive walks back from yesterday to the first day that is
not a 404), each answer classified:

    ok              answered in the known format
    not_found       404 for days that should have a file: NSE moved it
    blocked         no answer after the retries, an HTTP error, or a web page instead of data
    format_changed  data, but in a format noodles does not read
    manual          `mode = "manual"` in noodles.toml: not asked

Every source that is not ok comes with what to do (`FIXES`). Old-format bhavcopies are asked for
on their last day (5 Jul 2024): that archive no longer grows, but a moved folder still breaks a
first full pull.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from . import bhavcopy, indices, lineage, nse_json
from .config import FILE_NAME, Config
from .http import Fetched, NseClient

WALK_BACK = 7  # weekdays a daily archive is asked for, from yesterday back
FIXES = {
    "not_found": (
        "NSE answered 404: the file has likely moved. Find it on nseindia.com and set this "
        f"source's url in DATA/{FILE_NAME} (`noodles config init` writes one), or set its "
        'mode = "manual" and add its files with `noodles import`.'
    ),
    "blocked": (
        "No usable answer: NSE blocks or is down. Try again later or with a larger [network] "
        "interval; or save the files from a browser and `noodles import` them."
    ),
    "format_changed": (
        "NSE answers in a format noodles does not read yet: the parser needs a code change "
        "(open an issue with the URL). Meanwhile `noodles import` takes files in the known "
        "formats."
    ),
}


@dataclass(frozen=True)
class Probe:
    source: str
    status: str
    detail: str  # the URL asked, and the day for a daily archive

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "manual")


def classify(got: Fetched, valid: Callable[[bytes], bool]) -> str:
    if got.status == "not_found":
        return "not_found"
    if got.status == "error" or got.data.lstrip()[:1] == b"<":  # a block page is HTML
        return "blocked"
    return "ok" if valid(got.data) else "format_changed"


def _valid_bhavcopy(data: bytes) -> bool:
    try:
        bhavcopy.trade_day(data)
    except ValueError:
        return False
    return True


def _valid_indices(data: bytes) -> bool:
    try:
        indices.trade_day(data.decode("utf-8-sig", errors="replace"))
    except ValueError:
        return False
    return True


def _valid_json(dataset: str) -> Callable[[bytes], bool]:
    def valid(data: bytes) -> bool:
        try:
            rows = nse_json.rows(json.loads(data.decode("utf-8", errors="replace")))
        except ValueError:
            return False
        if rows is None:
            return False
        return not rows or nse_json.ROW_KEYS[dataset] <= set(rows[0])  # a quiet week is ok

    return valid


_LIST_READERS = {
    "symbolchange.csv": lineage.parse_symbol_changes,
    "namechange.csv": lineage.parse_name_changes,
}


def _valid_list(name: str) -> Callable[[bytes], bool]:
    def valid(data: bytes) -> bool:
        if not nse_json.list_ok(name, data):
            return False
        text = data.decode("utf-8-sig", errors="replace")
        if name == "holidays.json":
            return isinstance(json.loads(text).get("CM"), list)
        return bool(_LIST_READERS[name](text))

    return valid


def _weekdays_back(today: date, count: int) -> list[date]:
    out, day = [], today
    while len(out) < count:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return out


def _daily(
    client: NseClient,
    source: str,
    days: list[date],
    url: Callable[[date], str],
    valid: Callable[[bytes], bool],
) -> Probe:
    """The first day that is not a 404, classified; not_found when every day was."""
    for day in days:
        got = client.get(url(day))
        if got.status != "not_found":
            return Probe(source, classify(got, valid), f"{day} {url(day)}")
    return Probe(source, "not_found", f"{days[0]}..{days[-1]} {url(days[0])}")


def _daily_probes(client: NseClient, cfg: Config, today: date) -> list[Probe]:
    recent = _weekdays_back(today, WALK_BACK)
    old = [bhavcopy.OLD_LAST_DAY]

    def bhavcopy_url(day: date) -> str:
        return bhavcopy.archive_url(day, cfg)

    def indices_url(day: date) -> str:
        return cfg.url("indices", day, indices.archive_name(day))

    probes = []
    if not cfg.manual("bhavcopy"):
        probes.append(_daily(client, "bhavcopy", recent, bhavcopy_url, _valid_bhavcopy))
        probes.append(_daily(client, "bhavcopy (old format)", old, bhavcopy_url, _valid_bhavcopy))
    if not cfg.manual("indices"):
        probes.append(_daily(client, "indices", recent, indices_url, _valid_indices))
    return probes


def _feed_probes(client: NseClient, cfg: Config, today: date) -> list[Probe]:
    probes = []
    week = {
        "index": "equities",
        "from_date": f"{today - timedelta(days=7):%d-%m-%Y}",
        "to_date": f"{today:%d-%m-%Y}",
    }
    for dataset in nse_json.DATASETS:
        if cfg.manual(dataset):
            continue
        got = client.get(cfg.url(dataset), week, cookies=True)
        probes.append(Probe(dataset, classify(got, _valid_json(dataset)), cfg.url(dataset)))
    for name, (source, cookies) in nse_json.LISTS.items():
        if cfg.manual(source):
            continue
        got = client.get(cfg.url(source), cookies=cookies)
        probes.append(Probe(source, classify(got, _valid_list(name)), cfg.url(source)))
    return probes


def run(client: NseClient, cfg: Config, today: date | None = None) -> list[Probe]:
    """One probe per source in `download` mode, and a `manual` line for each of the others."""
    today = today or date.today()
    manual = [Probe(s, "manual", "not asked") for s in cfg.sources if cfg.manual(s)]
    return _daily_probes(client, cfg, today) + _feed_probes(client, cfg, today) + manual


def report(probes: list[Probe]) -> list[str]:
    """The lines to print: one per source, then what to do about each kind of problem."""
    lines = [f"  {p.source:<22} {p.status:<15} {p.detail}" for p in probes]
    for status, fix in FIXES.items():
        hit = [p.source for p in probes if p.status == status]
        if hit:
            lines += ["", f"{status} ({', '.join(hit)}):", f"  {fix}"]
    return lines
