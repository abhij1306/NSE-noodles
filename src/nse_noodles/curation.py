"""The human (or agent) curated inputs, kept in a curation folder outside the data:

    curation/research/*.csv        sourced research rows (RESEARCH_COLUMNS)
    curation/corrections.tsv       reviewed fixes to factors (CORRECTION_COLUMNS)
    curation/lineage_overrides.tsv owner verdicts on ISIN successions (lineage.read_overrides)
    curation/holidays.txt          past NSE holidays, one ISO date per line (coverage check)

All of it is untrusted text: every value is parsed here, and a line that does not parse is an
error naming its file and line, never a silent skip.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path

from . import parse

RESEARCH_COLUMNS = (
    "table",  # demerger | rights | bonus_instrument | split_bonus
    "symbol",
    "isin",
    "ex_date",  # YYYY-MM-DD, NSE's ex-date of the event researched
    "leg_type",  # demerger | amalgamation | rights | bonus | split | bonus_instrument
    "resulting_name",
    "resulting_symbol",
    "resulting_isin",
    "ratio_a",  # demerger: resulting shares; rights: new shares; bonus: new shares
    "ratio_b",  # ... for every ratio_b shares held
    "issue_price",  # rights: face value + premium, full price even if partly paid
    "face_value",
    "partly_paid",  # yes | no
    "coa_parent_pct",  # demerger: cost-of-acquisition % kept by the parent
    "coa_resulting_pct",
    "non_equity_consideration",  # warrants / CCDs / preference / cash alongside, else empty
    "value_per_share_rs",  # bonus_instrument: value of the debenture/preference per share
    "record_date",
    "our_data_wrong",  # yes / no / unknown + what
    "confidence",  # high | medium | low
    "source_url",
    "source_date",  # YYYY-MM-DD the source was published: when a backtest could have known it
    "quote",  # short verbatim quote from the source
    "notes",
)
RESEARCH_TABLES = ("demerger", "rights", "bonus_instrument", "split_bonus")
LEG_TYPES = ("", "demerger", "amalgamation", "rights", "bonus", "split", "bonus_instrument")
CORRECTION_COLUMNS = (
    "op",
    "isin",
    "ex_date",
    "type",
    "value",
    "new_ex_date",
    "note",
    "known_on",  # optional: the date the corrected value was public (empty = unknown)
)
CORRECTION_OPS = ("add", "replace", "delete", "move")
CORRECTION_TYPES = ("split", "bonus", "rights", "demerger", "other", "dividend")
CONFIDENCE = {"low": 0, "medium": 1, "high": 2}


class CurationError(ValueError):
    pass


def number(raw: str) -> Fraction | None:
    value = parse.money(raw)
    return None if value is None else Fraction(value)


@dataclass(frozen=True)
class ResearchRow:
    file: str
    line: int
    table: str
    symbol: str
    isin: str
    ex_date: date | None
    leg_type: str
    resulting_symbol: str
    resulting_isin: str
    ratio: tuple[Fraction, Fraction] | None
    issue_price: Fraction | None
    partly_paid: bool
    coa_parent: Fraction | None
    coa_resulting: Fraction | None
    non_equity: str
    value_per_share: Fraction | None
    record_date: date | None
    wrong: str
    confidence: int
    source_url: str
    source_date: date | None
    quote: str
    notes: str


def _date(r: dict[str, str], key: str, where: str, required: bool = False) -> date | None:
    """A research date cell: empty is None (unless required); anything unreadable is an error,
    never a silently dropped row."""
    if not r[key]:
        if required:
            raise CurationError(f"{where}: {key} is required")
        return None
    day = parse.nse_date(r[key])
    if day is None:
        raise CurationError(f"{where}: bad {key} {r[key][:20]!r}")
    return day


def _positive(r: dict[str, str], key: str, where: str) -> Fraction | None:
    """A research number cell: empty is None; anything unreadable or not above zero is an
    error, never a silent fall back to NSE's terms."""
    if not r[key]:
        return None
    value = number(r[key])
    if value is None or value <= 0:
        raise CurationError(f"{where}: bad {key} {r[key][:20]!r}")
    return value


def _ratio(r: dict[str, str], where: str) -> tuple[Fraction, Fraction] | None:
    a, b = _positive(r, "ratio_a", where), _positive(r, "ratio_b", where)
    if (a is None) != (b is None):
        raise CurationError(f"{where}: ratio_a and ratio_b go together")
    return None if a is None or b is None else (a, b)


def _choice(r: dict[str, str], key: str, allowed: tuple[str, ...], where: str) -> str:
    value = r[key].lower()
    if value not in allowed:
        raise CurationError(f"{where}: bad {key} {r[key][:20]!r} (one of {', '.join(allowed)})")
    return value


def _research_row(file: str, n: int, r: dict[str, str]) -> ResearchRow:
    where = f"{file}:{n}"
    return ResearchRow(
        file,
        n,
        _choice(r, "table", RESEARCH_TABLES, where),
        r["symbol"].upper(),
        r["isin"].upper(),
        _date(r, "ex_date", where, required=True),
        _choice(r, "leg_type", LEG_TYPES, where),
        r["resulting_symbol"].upper(),
        r["resulting_isin"].upper(),
        _ratio(r, where),
        _positive(r, "issue_price", where),
        r["partly_paid"].lower().startswith("y"),
        _positive(r, "coa_parent_pct", where),
        _positive(r, "coa_resulting_pct", where),
        r["non_equity_consideration"],
        _positive(r, "value_per_share_rs", where),
        _date(r, "record_date", where),
        r["our_data_wrong"].lower(),
        CONFIDENCE.get(r["confidence"].lower(), 0),
        r["source_url"],
        _date(r, "source_date", where),
        r["quote"],
        r["notes"],
    )


def read_research(folder: Path) -> list[ResearchRow]:
    out = []
    for path in sorted(folder.glob("*.csv")) if folder.is_dir() else []:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = set(RESEARCH_COLUMNS) - set(reader.fieldnames or ())
            if missing:
                raise CurationError(f"{path.name}: missing columns {sorted(missing)}")
            for n, row in enumerate(reader, start=2):
                r = {k: (row.get(k) or "").strip()[:2000] for k in RESEARCH_COLUMNS}
                out.append(_research_row(path.name, n, r))
    return out


@dataclass(frozen=True)
class Correction:
    line: int
    op: str
    isin: str
    ex_date: date
    action_type: str
    value: Fraction | None
    new_ex_date: date | None
    note: str
    known_on: date | None = None


def read_corrections(path: Path) -> list[Correction]:
    """`op isin ex_date type value new_ex_date note [known_on]`, tab-separated. `value` is the
    factor (0 < f < 1 for a price factor; a public-only bonus is old shares / new shares; a
    split is new face value / old, so a consolidation (reverse split) is above 1) or, for type
    `dividend`, rupees per share. Every line must carry a note (the evidence).
    `known_on` is when the corrected value was public; without it an added or replaced value
    is left out of point-in-time (as-of) builds."""
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t")]
        if parts[0] == "op":
            continue
        parts += [""] * (len(CORRECTION_COLUMNS) - len(parts))
        found = _correction(n, parts)
        if found is None:
            raise CurationError(f"{path.name}:{n}: bad correction line")
        out.append(found)
    return out


def _value_ok(op: str, kind: str, f: Fraction | None) -> bool:
    """An add or replace carries a positive value: any dividend, a split other than 1 (above 1
    is a consolidation), any other factor below 1."""
    if op not in ("add", "replace"):
        return True
    if f is None or f <= 0:
        return False
    if kind == "split":
        return f != 1
    return kind == "dividend" or f < 1


def _correction(n: int, parts: list[str]) -> Correction | None:
    """Line `n`'s cells as a Correction, or None when any cell is invalid."""
    op, isin, ex, kind, value, new_ex, note, known = parts[:8]
    ex_date = parse.nse_date(ex)
    f = number(value)
    valid = (
        op in CORRECTION_OPS
        and kind in CORRECTION_TYPES
        and ex_date is not None
        and parse.ISIN_RE.match(isin.upper())
        and note
        and _value_ok(op, kind, f)
        and (op != "move" or parse.nse_date(new_ex) is not None)
        and (not known or parse.nse_date(known) is not None)
    )
    if not valid:
        return None
    return Correction(
        n,
        op,
        isin.upper(),
        ex_date,  # type: ignore[arg-type]
        kind,
        f,
        parse.nse_date(new_ex),
        note,
        parse.nse_date(known) if known else None,
    )


def read_holidays(path: Path) -> set[date]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            day = parse.nse_date(line)
            if day is None:
                raise CurationError(f"{path.name}: bad date {line[:20]!r}")
            out.add(day)
    return out
