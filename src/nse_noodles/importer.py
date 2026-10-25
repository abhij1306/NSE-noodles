"""`noodles import`: files you fetched yourself, filed under `raw/` exactly as `pull` files them,
so every build step reads them like downloads.

For when NSE blocks the downloads, moves a file the config cannot point at, or the files come
from elsewhere. Each file is recognised by its content (never its name), checked, and written
under its canonical name:

    bhavcopy      one day, zipped or a bare CSV, old or UDiFF   raw/bhavcopy/<NSE's zip name>
    indices       ind_close_all_DDMMYYYY.csv                    raw/indices/
    corporate_actions, announcements, board_meetings
                  the JSON NSE's site answers ([...] or {"data": [...]})
                                                  raw/nse/<dataset>/<market>/import-<hash>.json
    symbolchange, namechange, holidays                          raw/lists/

A folder is read through; a zip that is not itself a bhavcopy is opened and each file in it
read (zips inside it too, one level). A file that names a day in its name but holds another
day's rows is refused. Another data folder's `raw/` imports whole (its bookkeeping is skipped).
Only files under the home folder or the current folder are read (`contained`).

A day already on disk with other bytes is a conflict and left alone, unless `replace`:
replacing it deletes the day's parsed file, so the next build reads the new one. A list is a
snapshot: an import replaces it. JSON rows only add (a row already on disk counts once, see
`nse_json.read_shards`). Every file written is logged in `raw/imports.jsonl` with where it came
from and its sha256.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from . import bhavcopy, indices, lineage, nse_json
from .layout import Layout, NotFoundMemo, write_bytes

DAY_KINDS = ("bhavcopy", "indices")
JSON_KINDS = nse_json.ROW_KEYS
LIST_KINDS = {
    "symbolchange": "symbolchange.csv",
    "namechange": "namechange.csv",
    "holidays": "holidays.json",
}
KINDS = (*DAY_KINDS, *JSON_KINDS, *LIST_KINDS)
MAX_ENTRY = 512 * 1024 * 1024  # a zip entry larger than this is not read
_SKIP = ("__MACOSX/", ".")
OWN_FILES = {"not_found.json", "imports.jsonl", "noodles.toml"}  # noodles' bookkeeping, skipped


@dataclass(frozen=True)
class Planned:
    kind: str
    target: Path
    data: bytes
    derived: tuple[Path, ...] = ()  # parsed files made stale when the target is replaced
    memo_day: tuple[str, date] | None = None  # (kind, day) no longer a 404


@dataclass
class Report:
    imported: dict[str, int] = field(default_factory=dict)
    replaced: list[str] = field(default_factory=list)
    already: int = 0
    conflicts: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.conflicts and not self.rejected


def _text(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def _zip(entry: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        # a fixed timestamp: the same CSV imported twice is the same zip, never a conflict
        archive.writestr(zipfile.ZipInfo(entry, (1980, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def _plan_bhavcopy(layout: Layout, name: str, data: bytes) -> Planned:
    if not zipfile.is_zipfile(io.BytesIO(data)):  # a bare CSV: zipped as NSE names it
        day = bhavcopy.trade_day(_zip("bhavcopy.csv", data))
        data = _zip(bhavcopy.archive_name(day).removesuffix(".zip"), data)
    day = bhavcopy.trade_day(data)
    bhavcopy.format_for(day)  # before ISINs, or between the formats: refused
    named = bhavcopy.day_of(Path(name).name)
    if named is not None and named != day:
        raise ValueError(f"named for {named} but its rows are dated {day}")
    if not bhavcopy.parse(day, data).rows:
        raise ValueError(f"no valid equity row for {day}")
    return Planned(
        "bhavcopy",
        layout.bhavcopy / bhavcopy.archive_name(day),
        data,
        (layout.bars / f"date={day.isoformat()}.parquet",),
        ("bhavcopy", day),
    )


def _plan_indices(layout: Layout, name: str, data: bytes) -> Planned:
    text = _text(data)
    day = indices.trade_day(text)
    if Path(name).name.startswith("ind_close_all_") and Path(name).name != indices.archive_name(
        day
    ):
        raise ValueError(f"named {Path(name).name} but its rows are dated {day}")
    if not indices.parse(text, day):
        raise ValueError(f"no index row with a close for {day}")
    return Planned(
        "indices",
        layout.indices_raw / indices.archive_name(day),
        data,
        (layout.index / f"date={day.isoformat()}.parquet",),
        ("indices", day),
    )


def _json_rows(data: bytes) -> list:
    try:
        rows = nse_json.rows(json.loads(_text(data)))
    except ValueError:
        rows = None
    if not rows or not all(isinstance(r, dict) for r in rows):
        raise ValueError("not a non-empty JSON list of rows")
    return rows


def _plan_json(kind: str, layout: Layout, data: bytes, market: str) -> Planned:
    rows = _json_rows(data)
    missing = JSON_KINDS[kind] - set(rows[0])
    if missing:
        raise ValueError(f"rows lack {sorted(missing)}: not {kind}")
    body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()[:16]
    return Planned(kind, layout.nse(kind, market) / f"import-{digest}.json", body)


def _plan_list(kind: str, layout: Layout, data: bytes) -> Planned:
    name = LIST_KINDS[kind]
    text = _text(data)
    if kind == "holidays":
        ok = nse_json.list_ok(name, data) and isinstance(json.loads(text).get("CM"), list)
    elif kind == "symbolchange":
        ok = bool(lineage.parse_symbol_changes(text))
    else:
        ok = bool(lineage.parse_name_changes(text))
    if not ok:
        raise ValueError(f"no {kind} row in it")
    return Planned(kind, layout.lists / name, data)


def detect(name: str, data: bytes) -> str:
    """The kind a file is, from its content (a list's name breaks a tie); ValueError if none."""
    if bhavcopy.readable(data):
        return "bhavcopy"
    text = _text(data).lstrip()
    if text[:1] in "[{":
        return _detect_json(text)
    cells = [h.strip().strip('"') for h in text.split("\n", 1)[0].split(",")]
    header = {c.upper() for c in cells}
    try:
        bhavcopy.columns(cells)
        return "bhavcopy"
    except ValueError:
        pass
    if {"INDEX NAME", "INDEX DATE"} <= header:
        return "indices"
    if header & {"SM_KEY_SYMBOL", "OLD SYMBOL"} or "symbolchange" in name.lower():
        return "symbolchange"
    if lineage.parse_name_changes(text):
        return "namechange"
    raise ValueError("not a file noodles knows (give --kind if it is one)")


def _detect_json(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError:
        raise ValueError("not valid JSON") from None
    if isinstance(payload, dict) and isinstance(payload.get("CM"), list):
        return "holidays"
    rows = nse_json.rows(payload)
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    for kind, keys in JSON_KINDS.items():
        if keys <= set(first):
            return kind
    raise ValueError("JSON of no known NSE dataset (give --kind if it is one)")


def plan(layout: Layout, name: str, data: bytes, kind: str, market: str) -> Planned:
    planners: dict[str, Callable[[], Planned]] = {
        "bhavcopy": lambda: _plan_bhavcopy(layout, name, data),
        "indices": lambda: _plan_indices(layout, name, data),
        **{k: (lambda k=k: _plan_json(k, layout, data, market)) for k in JSON_KINDS},
        **{k: (lambda k=k: _plan_list(k, layout, data)) for k in LIST_KINDS},
    }
    return planners[kind]()


def files(path: Path) -> list[Path]:
    """`path`, or every file under it in name order; hidden files and noodles' own bookkeeping
    (another data folder's `raw/` imports as is) left out."""
    found = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    return [p for p in found if not p.name.startswith(".") and p.name not in OWN_FILES]


def contained(path: Path) -> Path:
    """`path` resolved (links followed), when it lies under the home folder or the current
    folder; ValueError otherwise. An agent running `noodles import` never reads system files,
    and a link in a folder never leads out of those two."""
    real = os.path.realpath(path)
    for base in (os.path.realpath(Path.home()), os.path.realpath(Path.cwd())):
        if real == base or real.startswith(os.path.join(base, "")):
            return Path(real)
    raise ValueError(
        "outside your home folder and the current folder: run noodles from the folder that "
        "holds it, or move it under your home folder"
    )


def _entries(label: str, data: bytes) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            base = info.filename.rsplit("/", 1)[-1]
            if info.is_dir() or info.filename.startswith(_SKIP) or base.startswith("."):
                continue
            if info.file_size > MAX_ENTRY:
                raise ValueError(f"{info.filename} is over {MAX_ENTRY >> 20} MB")
            yield f"{label}!{info.filename}", archive.read(info)


class _Importer:
    def __init__(self, layout: Layout, kind: str | None, market: str, replace: bool) -> None:
        self.layout, self.kind, self.market, self.replace = layout, kind, market, replace
        self.report = Report()
        self.memo = NotFoundMemo(layout.raw / "not_found.json")
        self.log: list[dict] = []

    def read(self, path: Path) -> None:
        try:
            data = contained(path).read_bytes()
        except (OSError, ValueError) as exc:
            self.report.rejected.append(f"{path}: {exc}")
            return
        self.one(str(path), data)

    def one(self, label: str, data: bytes, depth: int = 0) -> None:
        try:
            if zipfile.is_zipfile(io.BytesIO(data)) and not bhavcopy.readable(data):
                if depth >= 2:
                    raise ValueError("zip nested too deep")
                for inner, got in _entries(label, data):
                    self.one(inner, got, depth + 1)
                return
            kind = self.kind or detect(label, data)
            self.store(label, plan(self.layout, label, data, kind, self.market))
        except (ValueError, zipfile.BadZipFile) as exc:  # UnicodeError is a ValueError
            self.report.rejected.append(f"{label}: {exc}")

    def store(self, label: str, p: Planned) -> None:
        replaced = False
        if p.target.exists():
            if p.target.read_bytes() == p.data:
                self.report.already += 1
                return
            if p.kind in DAY_KINDS and not self.replace:
                self.report.conflicts.append(
                    f"{label}: {p.target.name} already holds another file (--replace swaps it)"
                )
                return
            replaced = True
        write_bytes(p.target, p.data)
        for stale in p.derived:
            stale.unlink(missing_ok=True)
        if p.memo_day is not None:
            self.memo.discard(*p.memo_day)  # type: ignore[arg-type]
        self.report.imported[p.kind] = self.report.imported.get(p.kind, 0) + 1
        if replaced:
            self.report.replaced.append(p.target.name)
        self.log.append(
            {
                "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "kind": p.kind,
                "from": label,
                "stored_as": p.target.relative_to(self.layout.root).as_posix(),
                "sha256": hashlib.sha256(p.data).hexdigest(),
                "replaced": replaced,
            }
        )

    def close(self) -> None:
        if self.log:
            self.memo.save()
            self.layout.raw.mkdir(parents=True, exist_ok=True)
            with (self.layout.raw / "imports.jsonl").open("a", encoding="utf-8") as out:
                out.writelines(json.dumps(row) + "\n" for row in self.log)


def run(
    layout: Layout,
    paths: list[Path],
    kind: str | None = None,
    market: str = "equities",
    replace: bool = False,
) -> Report:
    """File every file under `paths` (each read as `kind`, else recognised) into `raw/`."""
    importer = _Importer(layout, kind, market, replace)
    try:
        for top in paths:
            for path in files(top):
                importer.read(path)
    finally:
        importer.close()
    return importer.report
