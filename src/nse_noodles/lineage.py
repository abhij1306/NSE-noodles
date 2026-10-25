"""ISIN lineage: one price history per company across ISIN changes, never across two companies.

NSE gives the same shares a new ISIN at a face-value split or consolidation, at a reissue, and
sometimes with a symbol or name change. Each ISIN then holds a piece of one history. A
succession old -> new is LINKED only when every rule holds; anything else is a REVIEW case,
never a link (a false link is worse than a missing one):

- Succession: the new ISIN's first session follows the old one's last within MAX_GAP_SESSIONS
  (NSE suspends a few sessions around the change); they never trade on the same day; neither
  side has another candidate (a chain never forks).
- Same security: the same symbol across the change, or an NSE symbol-change record from the old
  symbol to the new one near the change.
- NSE carried the old close: the new ISIN's first PREVCLOSE is the old last close, raw or times
  the factors between the two days, within PREV_CLOSE_TOL (1%).
- Price continuity: the new first open is within OPEN_TOL (10%) of that adjusted old close.
- No unexplained move at the edges: every session move in the last and first EDGE_DAYS of the
  two ISINs is within JUMP_TOL (25%) once known factors are applied.
- No restructuring: no corporate action of either side near the change names a demerger,
  amalgamation, merger, scheme of arrangement or capital reduction.

Demergers, mergers, schemes and relistings keep separate histories. The owner's verdict on a
found succession (`overrides`: link / unlink) wins over the rules and holds across rebuilds.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from .bars import Span

MAX_GAP_SESSIONS = 5
REVIEW_GAP_SESSIONS = 30  # a longer gap is no succession candidate at all
PREV_CLOSE_TOL = 0.01
OPEN_TOL = 0.10
JUMP_TOL = 0.25
SYMBOL_CHANGE_SLACK = timedelta(days=7)
RESTRUCTURING_WINDOW = timedelta(days=30)
RESTRUCTURING = re.compile(
    r"demerg|amalgam|merger|arrangement|capital reduction|reduction (?:of|in) (?:share )?capital",
    re.IGNORECASE,
)
LINKED, REVIEW = "linked", "review"
_OLD_SYMBOL, _NEW_SYMBOL, _EFFECTIVE = "OLD SYMBOL", "NEW SYMBOL", "EFFECTIVE DATE"  # NSE headers


@dataclass(frozen=True)
class SymbolChange:
    old: str
    new: str
    effective: date


@dataclass(frozen=True)
class NameChange:
    symbol: str
    effective: date


@dataclass(frozen=True)
class Action:
    """A corporate action as the rules read it (factor 1 for text-only rows)."""

    isin: str
    ex_date: date
    action_type: str
    factor: float
    text: str = ""


@dataclass(frozen=True)
class Candidate:
    old: Span
    new: Span
    gap_sessions: int
    via: str  # same_symbol | symbol_change
    change: SymbolChange | None = None


@dataclass(frozen=True)
class Decision:
    old_isin: str
    new_isin: str
    effective: date
    status: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _nse_date(raw: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _csv(text: str) -> list[dict[str, str]]:
    return [
        {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
        for row in csv.DictReader(io.StringIO(text))
    ]


def parse_symbol_changes(text: str) -> list[SymbolChange]:
    """NSE's symbolchange.csv (company, old symbol, new symbol, effective date). The file has
    had a header row in some versions and none in others: a header-less file is read by
    position."""
    rows = _csv(text)
    if rows and _OLD_SYMBOL not in rows[0] and "SM_KEY_SYMBOL" not in rows[0]:
        rows = [
            {_OLD_SYMBOL: r[1], _NEW_SYMBOL: r[2], _EFFECTIVE: r[3]}
            for r in csv.reader(io.StringIO(text))
            if len(r) >= 4
        ]
    out = []
    for row in rows:
        old = (row.get(_OLD_SYMBOL) or row.get("SM_KEY_SYMBOL") or "").upper()
        new = (row.get(_NEW_SYMBOL) or row.get("SM_NEW_SYMBOL") or "").upper()
        day = _nse_date(row.get(_EFFECTIVE) or row.get("SM_APPLICABLE_FROM") or "")
        if old and new and old != new and day is not None:
            out.append(SymbolChange(old, new, day))
    return out


def parse_name_changes(text: str) -> list[NameChange]:
    out = []
    for row in _csv(text):
        symbol = (row.get("SYMBOL") or row.get("NCH_SYMBOL") or "").upper()
        day = _nse_date(row.get(_EFFECTIVE) or row.get("NCH_DATE") or "")
        if symbol and day is not None:
            out.append(NameChange(symbol, day))
    return out


def sessions_between(sessions: Sequence[date], first: date, last: date) -> int:
    """Sessions strictly between two days (from the sorted session list)."""
    return max(0, bisect_left(sessions, last) - bisect_right(sessions, first))


def _targets(
    old: Span, by_first: Mapping[str, list[Span]], renamed: Mapping[str, list[SymbolChange]]
) -> list[tuple[Span, SymbolChange | None]]:
    """Spans that could continue `old`: those starting under its last symbol, then those
    starting under a symbol it changed to near the change."""
    targets: list[tuple[Span, SymbolChange | None]] = [
        (new, None) for new in by_first.get(old.last_symbol, ())
    ]
    for change in renamed.get(old.last_symbol, ()):
        targets += [
            (new, change)
            for new in by_first.get(change.new, ())
            if old.last_day - SYMBOL_CHANGE_SLACK
            <= change.effective
            <= new.first_day + SYMBOL_CHANGE_SLACK
        ]
    return targets


def candidates(
    spans: Iterable[Span], sessions: Sequence[date], changes: Iterable[SymbolChange]
) -> list[Candidate]:
    spans = list(spans)
    by_first: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        by_first[span.first_symbol].append(span)
    renamed: dict[str, list[SymbolChange]] = defaultdict(list)
    for change in changes:
        renamed[change.old].append(change)
    out: dict[tuple[str, str], Candidate] = {}
    for old in spans:
        for new, change in _targets(old, by_first, renamed):
            key = (old.isin, new.isin)
            if key not in out:
                found = _candidate(old, new, change, sessions)
                if found:
                    out[key] = found
    return sorted(out.values(), key=lambda c: (c.new.first_day, c.old.isin, c.new.isin))


def _candidate(
    old: Span, new: Span, change: SymbolChange | None, sessions: Sequence[date]
) -> Candidate | None:
    """`new` as a successor of `old`: another ISIN starting after `old` ends, within
    REVIEW_GAP_SESSIONS."""
    if old.isin == new.isin or new.first_day <= old.last_day:
        return None
    gap = sessions_between(sessions, old.last_day, new.first_day)
    if gap > REVIEW_GAP_SESSIONS:
        return None
    return Candidate(old, new, gap, "same_symbol" if change is None else "symbol_change", change)


def _price_actions(actions: Iterable[Action]) -> tuple[dict[tuple[date, str], Action], bool]:
    chosen: dict[tuple[date, str], Action] = {}
    conflict = False
    for a in actions:
        if a.factor != 1:
            held = chosen.setdefault((a.ex_date, a.action_type), a)
            conflict = conflict or abs(held.factor - a.factor) > 1e-9
    return chosen, conflict


def _factor(chosen: Mapping[tuple[date, str], Action], after: date, through: date) -> float:
    f = 1.0
    for (ex, _), a in chosen.items():
        if after < ex <= through:
            f *= a.factor
    return f


def _edge_move(old: Span, new: Span, chosen: Mapping[tuple[date, str], Action]) -> str | None:
    """The first session move beyond JUMP_TOL across the two spans' edge bars, if any."""
    for before, after in itertools.pairwise([*old.tail, *new.head]):
        base = before.close * _factor(chosen, before.day, after.day)
        if base > 0 and abs(after.open / base - 1) > JUMP_TOL:
            return f"{before.day} -> {after.day} x{after.open / base:.4f}"
    return None


def _price_break(
    old: Span,
    new: Span,
    chosen: Mapping[tuple[date, str], Action],
    expected: float,
    ev: dict[str, Any],
) -> str | None:
    """The first price rule the succession breaks (adding what it measured to `ev`), or None."""
    if old.last_close <= 0 or expected <= 0:
        return "price_jump"
    if new.first_prev_close is None:
        return "no_prev_close"
    raw_gap = new.first_prev_close / old.last_close - 1
    adj_gap = new.first_prev_close / expected - 1
    if min(abs(raw_gap), abs(adj_gap)) > PREV_CLOSE_TOL:
        return "prev_close_mismatch"
    open_gap = new.first_open / expected - 1
    ev["open_ratio"] = round(open_gap + 1, 6)
    if abs(open_gap) > OPEN_TOL:
        return "price_jump"
    edge = _edge_move(old, new, chosen)
    if edge:
        ev["edge_move"] = edge
        return "price_jump"
    return None


def _link_reason(c: Candidate, actions: Sequence[Action], renamed: bool) -> str:
    old, new = c.old, c.new
    near = [a for a in actions if old.last_day - SYMBOL_CHANGE_SLACK <= a.ex_date <= new.first_day]
    if any(a.action_type == "split" for a in near):
        return "face_value_change"
    if old.last_symbol != new.first_symbol:
        return "symbol_change"
    if renamed:
        return "name_change"
    return "isin_reissue"


def decide(
    c: Candidate,
    actions: Sequence[Action],
    names: Iterable[NameChange] = (),
    *,
    ambiguous: bool = False,
) -> Decision:
    old, new = c.old, c.new
    chosen, conflict = _price_actions(actions)
    factor = _factor(chosen, old.last_day, new.first_day)
    ev: dict[str, Any] = {
        "old_symbol": old.last_symbol,
        "new_symbol": new.first_symbol,
        "old_last_day": old.last_day.isoformat(),
        "new_first_day": new.first_day.isoformat(),
        "gap_sessions": c.gap_sessions,
        "via": c.via,
        "old_last_close": old.last_close,
        "new_prev_close": new.first_prev_close,
        "new_first_open": new.first_open,
        "gap_factor": round(factor, 6),
    }
    lo, hi = old.last_day - RESTRUCTURING_WINDOW, new.first_day + RESTRUCTURING_WINDOW
    restructuring = sorted(
        {a.text for a in actions if lo <= a.ex_date <= hi and RESTRUCTURING.search(a.text)}
    )
    renamed = any(
        n.symbol in (old.last_symbol, new.first_symbol) and lo <= n.effective <= hi for n in names
    )

    def verdict(status: str, reason: str) -> Decision:
        return Decision(old.isin, new.isin, new.first_day, status, reason, ev)

    if ambiguous:
        return verdict(REVIEW, "ambiguous")
    if c.gap_sessions > MAX_GAP_SESSIONS:
        return verdict(REVIEW, "long_gap")
    if restructuring:
        ev["restructuring"] = restructuring[:3]
        return verdict(REVIEW, "restructuring")
    if conflict:
        return verdict(REVIEW, "factor_conflict")
    broken = _price_break(old, new, chosen, old.last_close * factor, ev)
    if broken:
        return verdict(REVIEW, broken)
    return verdict(LINKED, _link_reason(c, actions, renamed))


def decide_all(
    found: Sequence[Candidate],
    actions: Mapping[str, Sequence[Action]],
    names: Sequence[NameChange] = (),
) -> list[Decision]:
    olds: dict[str, int] = defaultdict(int)
    news: dict[str, int] = defaultdict(int)
    for c in found:
        olds[c.old.isin] += 1
        news[c.new.isin] += 1
    return [
        decide(
            c,
            [*actions.get(c.old.isin, ()), *actions.get(c.new.isin, ())],
            names,
            ambiguous=olds[c.old.isin] > 1 or news[c.new.isin] > 1,
        )
        for c in found
    ]


def apply_overrides(
    decisions: Iterable[Decision], overrides: Mapping[tuple[str, str], str]
) -> list[Decision]:
    """The owner's verdicts (old, new) -> link | unlink over the rules'. A rules link sharing a
    side with an owner link becomes a review case, so a chain never forks."""
    owned = [pair for pair, v in overrides.items() if v == "link"]
    olds, news = {o for o, _ in owned}, {n for _, n in owned}
    out = []
    for d in decisions:
        v = overrides.get((d.old_isin, d.new_isin))
        if v == "link":
            status, reason = LINKED, "owner_link"
        elif v == "unlink":
            status, reason = REVIEW, "owner_unlink"
        elif d.status == LINKED and (d.old_isin in olds or d.new_isin in news):
            status, reason = REVIEW, "owner_displaced"
        else:
            out.append(d)
            continue
        out.append(
            replace(
                d,
                status=status,
                reason=reason,
                evidence={**d.evidence, "rules": f"{d.status}:{d.reason}"},
            )
        )
    return out


def chains(links: Iterable[tuple[str, str]]) -> dict[str, tuple[str, int]]:
    """Each linked ISIN -> (its chain's current ISIN, distance from it). Raises on a fork or a
    cycle."""
    succ: dict[str, str] = {}
    pred: dict[str, str] = {}
    for old, new in links:
        if old in succ or new in pred:
            raise ValueError(f"lineage fork at {old} -> {new}")
        succ[old], pred[new] = new, old
    out: dict[str, tuple[str, int]] = {}
    for node in {*succ, *pred}:
        head, depth = node, 0
        while head in succ:
            head, depth = succ[head], depth + 1
            if depth > len(succ):
                raise ValueError("lineage cycle")
        out[node] = (head, depth)
    return out


def read_overrides(text: str) -> dict[tuple[str, str], str]:
    """`op<TAB>old_isin<TAB>new_isin<TAB>note` lines, op link | unlink; `#` starts a comment."""
    out = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if not line.strip() or line.startswith("#") or parts[0] == "op":
            continue
        if len(parts) < 3 or parts[0] not in ("link", "unlink"):
            raise ValueError(f"bad lineage override line: {line[:80]!r}")
        out[(parts[1].strip().upper(), parts[2].strip().upper())] = parts[0]
    return out


def evidence_json(d: Decision) -> str:
    return json.dumps(d.evidence, default=str, sort_keys=True)
