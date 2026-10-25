"""`noodles` with no arguments: a guided menu over the same commands.

It shows what the data directory already holds, asks only for what a command needs (with a
default for each answer), prints the equivalent command line and runs it through `cli.main`.
It does nothing the flags cannot: a front door for people who don't know them yet.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import bhavcopy, nse_json
from .layout import Layout

Ask = Callable[[str], str]
Say = Callable[[str], None]
Run = Callable[[list[str]], int]

BUILD_STEPS = ("bars", "ca-db", "actions", "hindsight", "reports")
PULL_SOURCES = (
    ("bhavcopy", "daily price archives (one zip per session)"),
    ("indices", "daily index levels, for days that have a bhavcopy"),
    ("nse", "corporate actions, announcements or board meetings (JSON feed)"),
    ("lists", "symbol and name changes, this year's holidays"),
)


# --- what the data directory holds -------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    bhavcopy_files: int = 0
    bar_days: tuple[date, ...] = ()
    index_days: int = 0
    nse_files: dict[str, int] = field(default_factory=dict)
    lists: tuple[str, ...] = ()
    database: bool = False
    outputs: tuple[str, ...] = ()
    review_rows: int | None = None

    @property
    def last_bar_day(self) -> date | None:
        return self.bar_days[-1] if self.bar_days else None


def _day_files(folder: Path) -> tuple[date, ...]:
    days = []
    for path in folder.glob("date=*.parquet"):
        try:
            days.append(date.fromisoformat(path.stem.removeprefix("date=")))
        except ValueError:
            continue
    return tuple(sorted(days))


def status(layout: Layout) -> Status:
    nse_files = {}
    for dataset in nse_json.DATASETS:
        for index in nse_json.INDEXES:
            count = len(list(layout.nse(dataset, index).glob("*.json")))
            if count:
                nse_files[f"{dataset}/{index}"] = count
    review = layout.out / "review.csv"
    review_rows = None
    if review.exists():
        with review.open(encoding="utf-8") as f:
            review_rows = max(sum(1 for _ in f) - 1, 0)
    return Status(
        bhavcopy_files=len(list(layout.bhavcopy.glob("*.zip"))),
        bar_days=_day_files(layout.bars),
        index_days=len(_day_files(layout.index)),
        nse_files=nse_files,
        lists=tuple(sorted(p.name for p in layout.lists.glob("*"))),
        database=layout.db.exists(),
        outputs=tuple(sorted(p.name for p in layout.out.glob("*") if p.is_file())),
        review_rows=review_rows,
    )


def describe(st: Status) -> list[str]:
    if not (st.bhavcopy_files or st.bar_days or st.nse_files or st.lists or st.database):
        return ["  empty: start with 'Run everything for a date range'"]
    days = st.bar_days
    bars = f"{len(days)} days, {days[0]} to {days[-1]}" if days else "none built"
    lines = [
        f"  bhavcopy zips     {st.bhavcopy_files}",
        f"  price bars        {bars}",
        f"  index days        {st.index_days}",
    ]
    lines += [f"  {key:<17} {count} month files" for key, count in st.nse_files.items()]
    lines += [
        f"  lists             {', '.join(st.lists) or 'not pulled'}",
        f"  database          {'built' if st.database else 'not built'}",
        f"  outputs           {', '.join(st.outputs) or 'none yet'}",
    ]
    if st.review_rows is not None:
        lines.append(f"  review list       {st.review_rows} rows waiting (out/review.csv)")
    return lines


# --- prompts -----------------------------------------------------------------------------------


class Cancelled(Exception):
    """The person typed `q` at a prompt: back to the menu."""


def _answer(ask: Ask, prompt: str) -> str:
    raw = ask(prompt).strip()
    if raw.lower() in ("q", "quit"):
        raise Cancelled
    return raw


def choose(ask: Ask, say: Say, title: str, options: list[str], default: int = 1) -> int:
    """The 0-based index of the option picked by number; Enter takes `default` (1-based)."""
    say(title)
    for n, option in enumerate(options, 1):
        say(f"  {n}) {option}")
    while True:
        raw = _answer(ask, f"Choose 1-{len(options)} [{default}]: ")
        if not raw:
            return default - 1
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        say("  Type a number from the list (q goes back).")


def ask_day(ask: Ask, say: Say, label: str, default: date) -> date:
    while True:
        raw = _answer(ask, f"{label} (YYYY-MM-DD) [{default}]: ")
        if not raw:
            return default
        try:
            return date.fromisoformat(raw)
        except ValueError:
            say("  Not a date: write it as 2026-10-23.")


def ask_yes(ask: Ask, question: str, default: bool) -> bool:
    raw = _answer(ask, f"{question} [{'Y/n' if default else 'y/N'}]: ").lower()
    return default if not raw else raw.startswith("y")


def _inside_git(path: Path) -> bool:
    return any((p / ".git").exists() for p in (path, *path.parents))


def ask_data(ask: Ask, say: Say, default: Path) -> Path:
    while True:
        raw = _answer(ask, f"Data directory [{default}]: ")
        path = Path(raw).expanduser().resolve() if raw else default
        if not _inside_git(path):
            return path
        say("  That is inside a git repository; data must never be committed. Pick another.")


# --- the menu ------------------------------------------------------------------------------------


def _range(ask: Ask, say: Say, st: Status, today: date) -> list[str]:
    if st.last_bar_day:
        start = st.last_bar_day
        say(f"  Prices run to {start}; starting there resumes (finished days are skipped).")
    else:
        start = today - timedelta(days=30)
        say(
            f"  Full history starts {bhavcopy.FIRST_ISIN_DAY}; at the polite 2 s per request"
            " it takes several hours. A month is a quick first run."
        )
    first = ask_day(ask, say, "From", start)
    last = ask_day(ask, say, "To", today)
    return ["--from", first.isoformat(), "--to", last.isoformat()]


def plan_run(ask: Ask, say: Say, st: Status, today: date) -> list[str]:
    argv = ["run", *_range(ask, say, st, today)]
    if ask_yes(ask, "Also pull announcements and board meetings (slow)?", False):
        argv.append("--with-announcements")
    return argv


def plan_pull(ask: Ask, say: Say, st: Status, today: date) -> list[str]:
    picked = choose(
        ask, say, "Pull which source?", [f"{name:<9} {what}" for name, what in PULL_SOURCES]
    )
    source = PULL_SOURCES[picked][0]
    if source == "lists":
        return ["pull", "lists"]
    argv = ["pull", source, *_range(ask, say, st, today)]
    if source == "nse":
        datasets = list(nse_json.DATASETS)
        argv += ["--dataset", datasets[choose(ask, say, "Which dataset?", datasets)]]
        markets = ["equities", "sme", "equities and sme"]
        market = markets[choose(ask, say, "Which market?", markets)]
        argv += ["--index", *market.split(" and ")]
    return argv


def plan_build(ask: Ask, say: Say, st: Status, today: date) -> list[str]:
    steps = [*BUILD_STEPS, "replay (point in time, as of a day)"]
    step = steps[choose(ask, say, "Build which step? (each needs the ones before it)", steps)]
    if step.startswith("replay"):
        return plan_replay(ask, say, st, today)
    return ["build", step]


def plan_replay(ask: Ask, say: Say, st: Status, today: date) -> list[str]:
    as_of = ask_day(ask, say, "As of the close of", st.last_bar_day or today)
    return ["build", "replay", "--as-of", as_of.isoformat()]


MENU: tuple[tuple[str, Callable[[Ask, Say, Status, date], list[str]] | None], ...] = (
    ("Run everything for a date range", plan_run),
    ("Pull one source", plan_pull),
    ("Build one step", plan_build),
    ("Point-in-time replay as of a day", plan_replay),
    ("Show what the data directory holds", None),
    ("Quit", None),
)


def command(argv: list[str]) -> str:
    """The command line to type: arguments quoted only where a shell needs it."""
    plain = re.compile(r"[\w@%+=:,./\\-]+")  # a Windows path's backslashes need no quotes
    return "noodles " + " ".join(a if plain.fullmatch(a) else shlex.quote(a) for a in argv)


def _do(picked: int, ask: Ask, say: Say, run: Run, layout: Layout, today: date) -> int | None:
    """Carry out one menu choice; the exit code when a command ran."""
    plan = MENU[picked][1]
    if plan is None:
        for line in describe(status(layout)):
            say(line)
        return None
    try:
        argv = ["--data", str(layout.root), *plan(ask, say, status(layout), today)]
        say(f"  = {command(argv)}")
        if not ask_yes(ask, "Run it?", True):
            return None
    except Cancelled:
        return None
    code = run(argv)
    say("  done." if code == 0 else f"  finished with exit code {code}: see the counts above.")
    return code


def main(ask: Ask, say: Say, run: Run, today: date | None = None) -> int:
    """The menu loop; returns the exit code of the last command run (0 when none ran)."""
    today = today or date.today()
    say("NSE-noodles: build a survivorship-bias-free NSE dataset. Type q at any prompt to go back.")
    try:
        root = ask_data(ask, say, Path(os.environ.get("NOODLES_DATA", "~/nse-data")).expanduser())
    except Cancelled:
        return 0
    layout, code = Layout(root), 0
    say("")
    say(f"{root}:")
    for line in describe(status(layout)):
        say(line)
    while True:
        say("")
        try:
            picked = choose(ask, say, "What next?", [label for label, _ in MENU])
        except Cancelled:
            return code
        if MENU[picked][0] == "Quit":
            return code
        ran = _do(picked, ask, say, run, layout, today)
        code = code if ran is None else ran
