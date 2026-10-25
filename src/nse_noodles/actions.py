"""From the corporate-action database, the bars and the curation folder to the accepted factors
(`actions`) and the review list (`review`), with the ISIN lineage (`lineage`, `chain_map`).

Order of work:
1. Lineage. The successions are decided with the text-parsed split/bonus factors (ungated) and
   every restructuring text near each ISIN, so a chain's bars read as one series.
2. Splits, bonuses and rights, gated together per (chain, ex-date session pair): the product of
   their factors must pass `factors.gate` against the ex-date open / previous close. A rights
   factor is TERP / cum price with the cum price the previous close. A rights gap that is merely
   `ambiguous` (small factors drown in a session's move) is accepted, tagged
   `price_unconfirmed`; anything `off_factor` goes to review.
3. Demergers. The ex-date version is the MARKET gap (ex open / previous close, divided by any
   factor accepted on the same day) divided by the NIFTY 500's move over the same night
   (`factors.NEUTRAL_INDEX`), accepted by `factors.market_factor`; the raw gap is kept beside
   it. Once every resulting company has a first close, the research ratio priced there
   (`factors.ratio_factor`, the child's close brought back to the ex-date by the index) is a
   second version, `disputed` and a review case when it is more than DEMERGER_VERSION_TOL from
   the first. Each demerger version records its band (`factors.band`).
4. Cash: dividends (total-return factor 1 - D / previous raw close; P&L gets raw prices plus
   cash) and bonuses of debentures / preference shares worth >= 1% of the price.
5. The reviewed correction list, last: each line ADDS a version (a deletion is a version of
   value 1), so what was believed before the correction stays replayable.

Nothing is guessed: a row that does not parse, has no bars, or is not confirmed by the bars is a
review row with a reason.

Every version carries `available_at` (see `versions`): the latest of the prices and sources the
value and its acceptance read, so the earliest time a backtest could have used it
(`available_basis` says which fact that was): the ex-date open for a gate-confirmed or TERP
factor and for a dividend; the close SETTLE_SESSIONS on for a demerger gap accepted because it
held; the resulting company's first close for a ratio-priced demerger; the research
`source_date` (end of that day) for a value only research supplies; a correction's `known_on`.
Null when a source it needs is undated: such a version never enters a replay. `announced_on`
(from `ca_db`) is the first NSE filing found for the event; for a demerger, its record-date
notice.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from . import factors, indices, lineage, parse, versions
from .bars import EDGE_DAYS, Bar, Span, spans, symbol_runs, window
from .bars import sessions as bar_sessions
from .curation import Correction, ResearchRow, read_corrections, read_research
from .layout import Layout
from .versions import ACCEPTED, DISPUTED, at_close, at_open, end_of, latest

Known = tuple[datetime | None, str]  # (available_at, available_basis)

BAR_DAYS = 14  # the previous session and the ex session must fall this close to the ex-date
WINDOW_DAYS = 25  # calendar days of bars read each side of an ex-date
SYMBOL_SLACK = 15  # a feed symbol names the ISINs that traded under it this close to the date
CHILD_DAYS = 366  # a resulting company lists within a year of the demerger
CHILD_SLACK = 5  # ... and never trades earlier than this before the ex-date
RECORD_DAYS = 7  # a record date falls this soon after the ex-date
DISPUTE_DAYS = 60
# feed dividend statuses that are not a cash dividend on an equity share: skipped, not reviewed
SKIP_DIVIDEND = ("not_equity_share", "not_equity_series", "preference_dividend", "no_dividend")
SKIP = "skip:"  # a proposal reason with this prefix is skipped, never reviewed
PRICE_TYPES = ("split", "bonus", "rights")  # the factors gated together per session pair
UNMATCHED = "research_unmatched"  # research about no feed row that it cannot stand in for
BONUS_BEFORE, BONUS_AFTER = 7, 3  # NSE's ex-date vs a researched bonus-debenture date
# a bonus of debentures / preference shares to some holders only is no per-share distribution
SOME_HOLDERS = ("non-promoter", "non promoter", "public only", "promoters excluded")
RESEARCH_TABLE = {  # research `table` -> the database table its rows are about
    "demerger": "demergers",
    "rights": "rights_issues",
    "bonus_instrument": "bonus_instruments",
    "split_bonus": "splits_bonus",
}


def fr(x: float) -> Fraction:
    return Fraction(str(round(x, 6)))


def f4(x: Fraction | float) -> str:
    return f"{float(x):.4f}"


@dataclass
class Proposal:
    table: str
    symbol: str
    isin: str
    ex_date: date
    text: str
    market: str = "equities"
    legs: tuple[parse.Leg, ...] = ()
    rights: parse.RightsTerms | None = None
    cash: Decimal | None = None
    hint: str = ""
    source: str = "nse_feed"
    research: list[ResearchRow] = field(default_factory=list)
    reason: str = ""  # set when the text alone already decides review / skip
    announced_on: date | None = None
    notice_url: str = ""


@dataclass
class Outcome:
    accepted: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


# --- resolving rows to chains and bars ----------------------------------------------------------


class Chains:
    """ISIN -> chain head (its current ISIN), members per head, and symbol lookups."""

    def __init__(
        self, all_spans: Sequence[Span], links: Iterable[tuple[str, str]], runs: Sequence[tuple]
    ) -> None:
        self.spans = {s.isin: s for s in all_spans}
        heads = lineage.chains(links)
        self.head = {isin: heads.get(isin, (isin, 0))[0] for isin in self.spans}
        self.depth = {isin: heads.get(isin, (isin, 0))[1] for isin in self.spans}
        self.members: dict[str, list[str]] = defaultdict(list)
        for isin, h in self.head.items():
            self.members[h].append(isin)
        self.by_symbol: dict[str, list[tuple[str, date, date]]] = defaultdict(list)
        for symbol, isin, first, last in runs:
            self.by_symbol[symbol].append((isin, first, last))

    def resolve(self, symbol: str, isin: str, day: date) -> str | None:
        """The chain a feed row (symbol, ISIN, date) belongs to: its ISIN's, else the ISIN that
        traded under the symbol around the date."""
        if isin in self.head:
            return self.head[isin]
        slack = timedelta(days=SYMBOL_SLACK)
        found = {
            self.head[i]
            for i, first, last in self.by_symbol.get(symbol, ())
            if first - slack <= day <= last + slack
        }
        return min(found) if len(found) == 1 else None

    def isins_near(self, symbol: str, isin: str, day: date) -> set[str]:
        slack = timedelta(days=SYMBOL_SLACK)
        out = {isin} if isin in self.spans else set()
        out |= {i for i, f, la in self.by_symbol.get(symbol, ()) if f - slack <= day <= la + slack}
        return out


def series_of(bars_by_isin: dict[str, list[Bar]], members: Iterable[str]) -> list[Bar]:
    by_day: dict[date, Bar] = {}
    for isin in members:
        for b in bars_by_isin.get(isin, ()):
            by_day.setdefault(b.day, b)
    return [by_day[d] for d in sorted(by_day)]


def around(series: Sequence[Bar], ex: date) -> tuple[Bar, Bar] | str:
    before = [b for b in series if b.day < ex and (ex - b.day).days <= BAR_DAYS]
    after = [b for b in series if b.day >= ex and (b.day - ex).days <= BAR_DAYS]
    if not before or not after:
        return "no_bars"
    if before[-1].close <= 0 or after[0].open <= 0:
        return "no_bars"
    return before[-1], after[0]


# --- proposals from the database and the research -----------------------------------------------


def _rows(con: duckdb.DuckDBPyConnection, table: str) -> list[dict]:
    # fetchall: an unread result would block the next call on this connection (duckdb >= 2)
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchall()
    if not exists[0][0]:
        return []
    cur = con.execute(f'SELECT * FROM "{table}"')
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _legs(text: str) -> tuple[parse.Leg, ...]:
    out = []
    for part in text.split(";") if text else ():
        kind, value = part.split("=")
        out.append(parse.Leg(kind, Fraction(value)))
    return tuple(out)


def _matches(r: ResearchRow, p: Proposal) -> bool:
    same = (r.isin and r.isin == p.isin) or (r.symbol and r.symbol == p.symbol)
    if not same or r.ex_date is None:
        return False
    if r.table == "bonus_instrument":
        return -BONUS_BEFORE <= (p.ex_date - r.ex_date).days <= BONUS_AFTER
    return r.ex_date == p.ex_date


def _noticed(p: Proposal, r: dict) -> Proposal:
    p.announced_on, p.notice_url = r.get("announced_on"), r.get("notice_url") or ""
    return p


def _event(table: str, r: dict, text: str = "subject") -> Proposal:
    return Proposal(table, r["symbol"], r["isin"], r["ex_date"], r[text], r["market"])


def _split_bonus(r: dict) -> Proposal:
    p = _event("splits_bonus", r)
    p.hint = f"class {r['instrument_class']} ({r['classification_source'] or 'no evidence'})"
    if r["parse_status"] != "ok":
        p.reason = SKIP + "non_equity_bonus" if parse.non_equity_bonus(r["subject"]) else "unparsed"
    else:
        p.legs = _legs(r["legs"])
    return p


def _rights_issue(r: dict) -> Proposal:
    p = _event("rights_issues", r)
    p.hint = f"FV {r['face_value']}"
    if r["parse_status"] == "ok":
        p.rights = parse.RightsTerms(fr(r["ratio_new"]), fr(r["ratio_held"]), fr(r["issue_price"]))
    else:
        p.reason = r["parse_status"]
    return p


def _dividend(r: dict) -> Proposal:
    p = _event("dividends", r, "subjects")
    if r["status"] == "ok":
        p.cash = Decimal(str(r["amount"]))
        p.hint = r["kinds"]
    else:
        p.reason = (SKIP if r["status"] in SKIP_DIVIDEND else "") + r["status"]
    return p


def _feed(con: duckdb.DuckDBPyConnection) -> list[Proposal]:
    """One proposal per dated feed row (a dividend row is kept undated too)."""
    dated = (
        ("splits_bonus", _split_bonus),
        ("rights_issues", _rights_issue),
        ("demergers", lambda r: _event("demergers", r)),
        ("bonus_instruments", lambda r: _event("bonus_instruments", r)),
    )
    out = [
        _noticed(make(r), r)
        for table, make in dated
        for r in _rows(con, table)
        if r["ex_date"] is not None
    ]
    return out + [_noticed(_dividend(r), r) for r in _rows(con, "dividends")]


def _attach_research(feed: list[Proposal], research: Sequence[ResearchRow]) -> set[int]:
    """Give each feed proposal the research rows about it; returns the rows used."""
    used: set[int] = set()
    for p in feed:
        for i, r in enumerate(research):
            if RESEARCH_TABLE.get(r.table) == p.table and _matches(r, p):
                p.research.append(r)
                used.add(i)
        # research saying the row itself is wrong (another ISIN, date or event) is never used
        # for its terms: the fix is a correction line, and the row waits in review until then
        if any(r.wrong.startswith("yes") for r in p.research):
            p.reason, p.research = "research_says_row_wrong", []
    return used


def _unmatched(r: ResearchRow) -> Proposal:
    """Research qualifying a feed row (a bonus instrument's value, a split or bonus) that names
    none: never an event of its own, a review row pointing at the research line."""
    table = RESEARCH_TABLE.get(r.table, r.table)
    p = Proposal(table, r.symbol, r.isin, r.ex_date, f"research {r.table}", source="research")  # type: ignore[arg-type]
    p.reason, p.hint = UNMATCHED, f"{r.file}:{r.line}"
    return p


def _research_only(research: Sequence[ResearchRow], used: set[int]) -> list[Proposal]:
    """Research events NSE's feed does not carry, one proposal per (table, company, ex-date)."""
    extra: dict[tuple[str, str, date], Proposal] = {}
    unmatched = []
    for i, r in enumerate(research):
        if i in used or r.ex_date is None:
            continue
        if r.table not in ("demerger", "rights"):
            unmatched.append(_unmatched(r))
            continue
        key = (r.table, r.symbol or r.isin, r.ex_date)
        if key not in extra:
            extra[key] = Proposal(
                RESEARCH_TABLE[r.table],
                r.symbol,
                r.isin,
                r.ex_date,
                f"research {r.table}",
                source="research",
            )
        extra[key].research.append(r)
    return [*extra.values(), *unmatched]


def proposals(con: duckdb.DuckDBPyConnection, research: Sequence[ResearchRow]) -> list[Proposal]:
    out = _feed(con)
    used = _attach_research(out, research)
    return out + _research_only(research, used)


# --- deciding -----------------------------------------------------------------------------------


def _row(p: Proposal, reason: str, evidence: dict | None = None) -> dict:
    return {
        "table": p.table,
        "symbol": p.symbol,
        "isin": p.isin,
        "ex_date": p.ex_date,
        "reason": reason,
        "text": p.text[:500],
        "hint": p.hint,
        "evidence": json.dumps(evidence or {}, default=str, sort_keys=True),
    }


@dataclass(frozen=True)
class Version:
    """What one version of an event says. `terms`: the research rows its value reads (their
    latest `source_date` is its `source_published_at`); without them, the first NSE filing found."""

    kind: str
    factor: Fraction | None
    method: str
    known: Known
    basis: str
    cash: Decimal | None = None
    terms: Sequence[ResearchRow] = ()
    status: str = ACCEPTED
    raw_gap: Fraction | None = None
    band: tuple[Fraction, Fraction] | None = None


def _float(x: Fraction | Decimal | None) -> float | None:
    return float(x) if x is not None else None


def _accept(chain: str, before: Bar, p: Proposal, evidence: dict, v: Version) -> dict:
    """One version of an event, as the row the factor tables store."""
    sources = [
        {"file": r.file, "line": r.line, "url": r.source_url, "confidence": r.confidence}
        for r in p.research
    ]
    if sources:
        evidence = {**evidence, "research": sources}
    published = (
        latest(*(end_of(r.source_date) for r in v.terms)) if v.terms else end_of(p.announced_on)
    )
    researched = (p.research and v.kind in ("rights", "demerger")) or p.source == "research"
    return {
        "chain_isin": chain,
        "isin": before.isin,
        "symbol": before.symbol,
        "ex_date": p.ex_date,
        "action_type": v.kind,
        "status": v.status,
        "value": float(v.factor) if v.factor is not None else 1.0,
        "cash": _float(v.cash),
        "basis": v.basis,
        "raw_gap": _float(v.raw_gap),
        "band_low": float(v.band[0]) if v.band else None,
        "band_high": float(v.band[1]) if v.band else None,
        "method": v.method,
        "source": "research" if researched else p.source,
        "text": p.text[:500],
        "evidence": json.dumps(evidence, default=str, sort_keys=True),
        "announced_on": p.announced_on,
        "notice_url": p.notice_url,
        "source_published_at": published,
        "available_at": v.known[0],
        "available_basis": v.known[1],
    }


def research_known(rows: Sequence[ResearchRow], floor: datetime) -> Known:
    """When a value taken from research could be used: the end of the latest `source_date` of
    the rows, never before `floor` (the price the rest of the decision read). (None, ...) when a
    row has no source date: the version then never enters a point-in-time replay."""
    days = [r.source_date for r in rows]
    if not days or any(d is None for d in days):
        return None, "research_undated"
    return latest(floor, *(end_of(d) for d in days)), "research_source_date"


def _bar_evidence(before: Bar, on: Bar) -> dict[str, Any]:
    return {
        "previous_day": before.day,
        "previous_close": before.close,
        "ex_day": on.day,
        "ex_open": on.open,
        "gap": round(on.open / before.close, 6),
    }


def _priced_research(p: Proposal) -> list[ResearchRow]:
    """The research rows confident and complete enough to price a rights issue."""
    return [r for r in p.research if r.confidence >= 1 and r.ratio and r.issue_price]


def _rights_terms(p: Proposal) -> parse.RightsTerms | str:
    """The terms to price: the research's when it is confident and complete, else NSE's."""
    best = _priced_research(p)
    if [r for r in p.research if r.non_equity]:
        return "non_simple_research"
    if best:
        r = best[0]
        return parse.RightsTerms(r.ratio[0], r.ratio[1], r.issue_price)  # type: ignore[index, arg-type]
    if p.research and all(r.confidence == 0 for r in p.research):
        return "low_confidence"
    return p.rights if p.rights is not None else (p.reason or "unparsed")


PriceLeg = tuple[Proposal, str, Fraction]  # (proposal, action type, factor)


def _price_factors(p: Proposal, before: Bar, on: Bar, out: Outcome) -> list[tuple[str, Fraction]]:
    """A proposal's (type, factor) legs: its stated ones, or its rights priced as TERP / previous
    close. Empty when the proposal is reviewed or skipped instead."""
    if p.table != "rights_issues":
        if not p.legs:  # research about a row whose text did not parse states no factor
            out.review.append(_row(p, p.reason or "unparsed", _bar_evidence(before, on)))
        return [(leg.action_type, leg.factor) for leg in p.legs]
    terms = _rights_terms(p)
    if isinstance(terms, str):
        out.review.append(_row(p, terms, _bar_evidence(before, on)))
        return []
    f = factors.rights_factor(terms.new, terms.held, terms.price, fr(before.close))
    if f == 1:
        out.skipped.append(_row(p, "rights_at_or_above_price"))
        return []
    return [("rights", f)]


def _price_legs(group: list[Proposal], before: Bar, on: Bar, out: Outcome) -> list[PriceLeg]:
    """Every distinct (type, factor) of the group, with the first proposal that states it."""
    legs: list[PriceLeg] = []
    seen: set[tuple[str, Fraction]] = set()
    for p in group:
        for kind, f in _price_factors(p, before, on, out):
            if (kind, f) not in seen:
                seen.add((kind, f))
                legs.append((p, kind, f))
    return legs


def _review_each(legs: list[PriceLeg], reason: str, evidence: dict, out: Outcome) -> None:
    for p in {id(p): p for p, _, _ in legs}.values():
        out.review.append(_row(p, reason, evidence))


def _price_version(
    p: Proposal, kind: str, f: Fraction, confirmed: bool, known: Known, priced: list[ResearchRow]
) -> Version:
    method = "gate_confirmed" if confirmed else "terp_price_unconfirmed"
    if kind != "rights":
        return Version(kind, f, method, known, "stated_ratio")
    terms = [r for r in priced if r in p.research]
    return Version(
        kind, f, ("terp; " + method) if confirmed else method, known, "terp", terms=terms
    )


def decide_price_group(
    chain: str, before: Bar, on: Bar, group: list[Proposal], out: Outcome
) -> list[dict]:
    """Gate the product of every split/bonus/rights factor of one chain and session pair."""
    legs = _price_legs(group, before, on, out)
    if not legs:
        return []
    kinds = [k for _, k, _ in legs]
    if len(set(kinds)) != len(kinds):
        _review_each(legs, "conflicting_rows", _bar_evidence(before, on), out)
        return []
    expected = math.prod((f for _, _, f in legs), start=Fraction(1))
    verdict = factors.gate(expected, fr(before.close), fr(on.open))
    evidence = {
        **_bar_evidence(before, on),
        "expected": f4(expected),
        "verdict": verdict.reason or "confirmed",
    }
    rights_only = all(k == "rights" for k in kinds)
    if not verdict.confirmed and not (rights_only and verdict.reason == "ambiguous"):
        _review_each(legs, verdict.reason, evidence, out)
        return []
    priced = [r for p, k, _ in legs if k == "rights" for r in _priced_research(p)]
    known: Known = (
        research_known(priced, at_open(on.day)) if priced else (at_open(on.day), "ex_open")
    )
    return [
        _accept(
            chain,
            before,
            p,
            {**evidence, "hint": p.hint},
            _price_version(p, kind, f, verdict.confirmed, known, priced),
        )
        for p, kind, f in legs
    ]


def _dates_disputed(p: Proposal) -> bool:
    for r in p.research:
        for kind, day in (("ex", r.ex_date), ("record", r.record_date)):
            if day is None or abs((day - p.ex_date).days) > DISPUTE_DAYS:
                continue
            ok = day == p.ex_date if kind == "ex" else 0 <= (day - p.ex_date).days <= RECORD_DAYS
            if not ok:
                return True
    return False


@dataclass(frozen=True)
class Night:
    """A demerger's ex-date night: `raw` is the ex-date open over the previous close, divided by
    any factor accepted on the same day; `gap` is `raw` over the neutral index's move that night."""

    before: Bar
    on: Bar
    same_day: Fraction
    index: dict[date, tuple[float, float]]
    prev: Fraction
    raw: Fraction
    overnight: Fraction | None
    gap: Fraction

    @classmethod
    def of(
        cls, before: Bar, on: Bar, same_day: Fraction, index: dict[date, tuple[float, float]]
    ) -> Night:
        prev = fr(before.close)
        raw = fr(on.open) / prev / same_day
        overnight = factors.index_move(index, before.day, on.day, at_open=True)
        gap = raw / overnight if overnight else raw
        return cls(before, on, same_day, index, prev, raw, overnight, gap)

    def neutral(self, price: Fraction, day: date) -> Fraction:
        """A close of `day` brought back to the previous close's market level."""
        drift = factors.index_move(self.index, self.before.day, day, at_open=False)
        return price / drift if drift else price

    def held(self, bar: Bar) -> Fraction:
        """A later close as a factor on the previous close, like `raw` but index-neutral."""
        return self.neutral(fr(bar.close), bar.day) / self.prev / self.same_day


def _demerger_refused(p: Proposal, legs: list[ResearchRow], out: Outcome) -> bool:
    """Research that rules the gap out: amalgamation only, doubtful, or dated otherwise."""
    if p.research and not legs:
        out.skipped.append(_row(p, "amalgamation_only"))
    elif legs and min(r.confidence for r in legs) < 1:
        out.review.append(_row(p, "low_confidence"))
    elif _dates_disputed(p):
        out.review.append(_row(p, "date_disputed"))
    else:
        return False
    return True


def _ratio_check(
    p: Proposal,
    legs: list[ResearchRow],
    night: Night,
    resulting: dict[str, list[Bar]],
    children: dict[tuple[str, int], str],
) -> tuple[Fraction, Known] | None:
    """The research ratio priced at each resulting company's first close, when every one has a
    close and nothing else went ex on the same day."""
    if not legs or night.same_day != 1:
        return None
    priced = []
    first_closes = []
    for r in legs:
        child = resulting.get(children.get((r.file, r.line), ""), [])
        first = next((b for b in child if b.day >= p.ex_date), None)
        if r.ratio and first is not None and first.close > 0:
            priced.append((r.ratio[0], r.ratio[1], night.neutral(fr(first.close), first.day)))
            first_closes.append(first.day)
    rf = factors.ratio_factor(priced, night.prev) if len(priced) == len(legs) else None
    if rf is None:
        return None
    known, why = research_known(legs, at_close(max(first_closes)))
    return rf, (known, "resulting_first_close" if known else why)


def _demerger_checks(
    p: Proposal,
    legs: list[ResearchRow],
    night: Night,
    resulting: dict[str, list[Bar]],
    children: dict[tuple[str, int], str],
) -> tuple[dict[str, Fraction], dict[str, Known], str]:
    """The cross-checks of the gap (ratio, cost of acquisition), when each was knowable, and a
    note on a CoA split that cannot be used."""
    checks: dict[str, Fraction] = {}
    check_known: dict[str, Known] = {}
    ratio = _ratio_check(p, legs, night, resulting, children)
    if ratio is not None:
        checks["ratio"], check_known["ratio"] = ratio
    coa = factors.coa_factor(
        next((r.coa_parent for r in legs if r.coa_parent is not None), None),
        [r.coa_resulting for r in legs],
    )
    if isinstance(coa, Fraction):
        checks["coa"] = coa
        check_known["coa"] = research_known(legs, at_open(night.on.day))
    return checks, check_known, coa if isinstance(coa, str) else ""


def _settle_bar(series: list[Bar], on: Bar) -> Bar | None:
    later = [b for b in series if b.day >= on.day]
    return later[factors.SETTLE_SESSIONS - 1] if len(later) >= factors.SETTLE_SESSIONS else None


def _demerger_evidence(
    night: Night, settled: Fraction | None, checks: dict[str, Fraction], note: str
) -> dict[str, Any]:
    return {
        **_bar_evidence(night.before, night.on),
        "raw_gap": f4(night.raw),
        "index": factors.NEUTRAL_INDEX if night.overnight else "missing",
        "index_overnight": f4(night.overnight) if night.overnight else None,
        "market_gap": f4(night.gap),
        "same_day_factor": f4(night.same_day),
        "settled": f4(settled) if settled is not None else None,
        "checks": {k: f4(v) for k, v in checks.items()},
        "note": note,
    }


def _gap_known(
    gap: Fraction,
    checks: dict[str, Fraction],
    check_known: dict[str, Known],
    settle: tuple[Bar, Fraction] | None,
) -> Known:
    """Usable as soon as the first route that accepts it was knowable: an agreeing check, or
    the gap holding SETTLE_SESSIONS on."""
    routes = [check_known[k] for k, v in checks.items() if factors.agrees(gap, v)]
    if settle and factors.market_factor(gap, {}, True, settle[1])[0] is not None:
        routes.append((at_close(settle[0].day), "settled_close"))
    dated = [r for r in routes if r[0] is not None]
    return min(dated) if dated else routes[0]  # type: ignore[type-var]


def _gap_method(gap: Fraction, checks: dict[str, Fraction]) -> str:
    method = "market_gap; " + "; ".join(
        f"{k} {f4(v)}" + ("" if factors.agrees(gap, v) else " disagrees") for k, v in checks.items()
    )
    return method.rstrip("; ")


def decide_demerger(
    chain: str,
    p: Proposal,
    series: list[Bar],
    same_day: Fraction,
    resulting: dict[str, list[Bar]],
    children: dict[tuple[str, int], str],
    index: dict[date, tuple[float, float]],
    out: Outcome,
) -> list[dict]:
    """The demerger's versions: the index-neutralised ex-date gap, then (when every resulting
    company has a first close) the ratio-priced factor. Empty when the gap is not accepted."""
    pair = around(series, p.ex_date)
    if isinstance(pair, str):
        out.review.append(_row(p, pair))
        return []
    legs = [r for r in p.research if r.leg_type in ("demerger", "")]
    if _demerger_refused(p, legs, out):
        return []
    night = Night.of(*pair, same_day, index)
    checks, check_known, note = _demerger_checks(p, legs, night, resulting, children)
    bar = _settle_bar(series, night.on)
    settle = (bar, night.held(bar)) if bar is not None else None
    settled = settle[1] if settle else None
    factor, why = factors.market_factor(night.gap, checks, True, settled)
    evidence = _demerger_evidence(night, settled, checks, note)
    if factor is None:
        out.review.append(_row(p, why if p.research else f"needs_research:{why}", evidence))
        return []
    known = _gap_known(night.gap, checks, check_known, settle)
    researched = known[1] in ("research_source_date", "resulting_first_close")
    basis = "index_neutral_gap" if night.overnight else "raw_gap"
    band = factors.band([night.raw, night.gap])
    gap = Version(
        "demerger", factor, _gap_method(night.gap, checks), known, basis,
        terms=legs if researched else (), raw_gap=night.raw, band=band,
    )  # fmt: skip
    found = [_accept(chain, night.before, p, evidence, gap)]
    ratio = checks.get("ratio")
    if ratio is None:
        return found
    agree = factors.versions_agree(night.gap, ratio)
    method = f"resulting_ratio; ex-date version {f4(night.gap)}"
    priced = Version(
        "demerger", ratio, method + ("" if agree else " disagrees: review"), check_known["ratio"],
        "resulting_ratio", terms=legs, status=ACCEPTED if agree else DISPUTED, raw_gap=night.raw,
        band=factors.band([night.raw, night.gap, ratio]),
    )  # fmt: skip
    found.append(_accept(chain, night.before, p, evidence, priced))
    if not agree:
        out.review.append(_row(p, "demerger_versions_disagree", evidence))
    return found


def _previous_bar(p: Proposal, series: list[Bar], out: Outcome) -> Bar | None:
    """The last session before the ex-date; None (skipped or reviewed) when there is none near."""
    before = [b for b in series if b.day < p.ex_date and (p.ex_date - b.day).days <= BAR_DAYS]
    if before:
        return before[-1]
    if series and p.ex_date <= series[0].day:
        out.skipped.append(_row(p, "before_bars"))
    else:
        out.review.append(_row(p, "no_bars"))
    return None


def _rupees(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _instrument_value(p: Proposal, p_close: Decimal, out: Outcome) -> ResearchRow | None:
    """The research row that values a bonus of debentures / preference shares as cash; None
    (reviewed or skipped) when there is none to use."""
    rows = [r for r in p.research if r.value_per_share]
    r = rows[0] if rows else None
    if r is None:
        out.review.append(_row(p, "needs_value"))
    elif r.confidence < 1:
        out.review.append(_row(p, "low_confidence"))
    elif any(w in f"{r.notes} {r.non_equity}".lower() for w in SOME_HOLDERS):
        out.review.append(_row(p, "some_holders_only"))
    elif not factors.bonus_instrument_counts(_rupees(r.value_per_share), p_close):  # type: ignore[arg-type]
        out.skipped.append(_row(p, "below_one_percent"))
    else:
        return r
    return None


def _cash_version(p: Proposal, p_close: Decimal, out: Outcome) -> Version | None:
    if p.table != "bonus_instruments":
        return Version(
            "dividend", None, "dividend", (at_open(p.ex_date), "ex_open"), "feed_amount", p.cash
        )
    r = _instrument_value(p, p_close, out)
    if r is None:
        return None
    known = research_known([r], at_open(p.ex_date))
    cash = _rupees(r.value_per_share)  # type: ignore[arg-type]
    return Version(
        "dividend", None, "bonus_instrument_as_cash", known, "research_value", cash, terms=[r]
    )


def decide_cash(chain: str, p: Proposal, series: list[Bar], out: Outcome) -> dict | None:
    prev = _previous_bar(p, series, out)
    if prev is None:
        return None
    p_close = Decimal(str(prev.close))
    v = _cash_version(p, p_close, out)
    if v is None:
        return None
    cash = v.cash
    assert cash is not None
    if factors.over_yield(cash, p_close):
        out.review.append(
            _row(p, "amount_vs_price", {"previous_close": prev.close, "cash": str(cash)})
        )
        return None
    f = factors.dividend_factor(cash, p_close)
    evidence = {
        "previous_day": prev.day,
        "previous_close": prev.close,
        "tr_factor": str(f),
        "kinds": p.hint,
    }
    return _accept(chain, prev, p, evidence, v)


def _held_value(held: dict, kind: str) -> float:
    return held["cash"] if kind == "dividend" else held["value"]


def _value(c: Correction) -> float:
    return float(c.value) if c.value is not None else 1.0


def _nothing(kind: str) -> float:
    """The value of a deleted event: no cash for a dividend, a factor of 1 for the rest."""
    return 0.0 if kind == "dividend" else 1.0


class _Corrector:
    """Applies correction lines to the found versions, keeping each event's current version
    (None once deleted) by (chain, ex-date, type)."""

    def __init__(self, found: list[dict], spans_by: dict[str, Span]) -> None:
        self.found = found
        self.spans_by = spans_by
        self.live: dict[tuple, dict | None] = {
            (v["chain_isin"], v["ex_date"], v["action_type"]): v
            for v in found
            if v["status"] == ACCEPTED
        }

    def version(
        self, c: Correction, held: dict, ex: date, value: float, how: str, basis: str, when: Known
    ) -> dict:
        dividend = held["action_type"] == "dividend"
        v = {
            **held,
            "ex_date": ex,
            "status": ACCEPTED,
            "value": 1.0 if dividend else value,
            "cash": value if dividend else None,
            "basis": basis,
            "raw_gap": None,
            "band_low": None,
            "band_high": None,
            "method": how,
            "source": "correction",
            "text": c.note[:500],
            "evidence": json.dumps({"line": c.line, "note": c.note}),
            "source_published_at": end_of(c.known_on),
            "available_at": when[0],
            "available_basis": when[1],
        }
        self.found.append(v)
        return v

    def apply(self, c: Correction, chain: str) -> str:
        key = (chain, c.ex_date, c.action_type)
        held = self.live.get(key)
        when: Known = (end_of(c.known_on), "correction" if c.known_on else "correction_undated")
        if c.op == "add":
            return self.add(c, chain, held, when)
        if held is None:
            return "not_found"
        if c.op == "delete":
            how = "correction: deleted"
            self.version(
                c, held, c.ex_date, _nothing(c.action_type), how, "correction_delete", when
            )
            self.live[key] = None
        elif c.op == "replace":
            how = f"correction (was {_held_value(held, c.action_type)})"
            self.live[key] = self.version(c, held, c.ex_date, _value(c), how, "correction", when)
        elif c.op == "move":
            return self.move(c, chain, held, when)
        return "applied"

    def add(self, c: Correction, chain: str, held: dict | None, when: Known) -> str:
        if held is not None:
            return "exists_use_replace"
        base = {
            "chain_isin": chain,
            "isin": c.isin,
            "symbol": self.spans_by[c.isin].last_symbol if c.isin in self.spans_by else "",
            "action_type": c.action_type,
            "announced_on": None,
            "notice_url": "",
        }
        self.live[(chain, c.ex_date, c.action_type)] = self.version(
            c, base, c.ex_date, _value(c), "correction", "correction", when
        )
        return "applied"

    def move(self, c: Correction, chain: str, held: dict, when: Known) -> str:
        to = (chain, c.new_ex_date, c.action_type)
        if self.live.get(to) is not None:
            return "destination_exists"  # never overwrite: replace or delete that one first
        how = f"correction: moved to {c.new_ex_date}"
        self.version(c, held, c.ex_date, _nothing(c.action_type), how, "correction_delete", when)
        moved = (latest(held["available_at"], when[0]), when[1])
        self.live[(chain, c.ex_date, c.action_type)] = None
        self.live[to] = self.version(
            c,
            held,
            c.new_ex_date,  # type: ignore[arg-type]
            _held_value(held, c.action_type),
            f"correction (moved from {c.ex_date})",
            "correction_move",
            moved,
        )
        return "applied"


def apply_corrections(
    found: list[dict],
    corrections: Sequence[Correction],
    chains: Chains,
    spans_by: dict[str, Span],
) -> list[dict]:
    """The reviewed list, last. Each line ADDS versions, never edits one: `add` a missing event,
    `replace` a value, `delete` (a version of value 1, cash 0), `move` (a deletion at the old
    ex-date and a version at the new one). Keys are (chain, ex-date, type), so a line may name
    any ISIN of the chain. A correction's version is available from its `known_on` (end of that
    day); without one it never enters a replay."""
    corrector = _Corrector(found, spans_by)
    return [
        {
            "line": c.line,
            "op": c.op,
            "isin": c.isin,
            "ex_date": c.ex_date,
            "type": c.action_type,
            "status": corrector.apply(c, chains.head.get(c.isin, c.isin)),
            "note": c.note[:300],
        }
        for c in corrections
    ]


# --- the build ----------------------------------------------------------------------------------


def _lineage_actions(
    con: duckdb.DuckDBPyConnection, chains_seed: Chains
) -> dict[str, list[lineage.Action]]:
    out: dict[str, list[lineage.Action]] = defaultdict(list)
    for r in _rows(con, "splits_bonus"):
        if r["ex_date"] is None:
            continue
        for isin in chains_seed.isins_near(r["symbol"], r["isin"], r["ex_date"]):
            for leg in _legs(r["legs"]):
                out[isin].append(
                    lineage.Action(
                        isin, r["ex_date"], leg.action_type, float(leg.factor), r["subject"]
                    )
                )
    for r in _rows(con, "ca_feed"):
        if r["ex_date"] is None or not lineage.RESTRUCTURING.search(r["subject"] or ""):
            continue
        for isin in chains_seed.isins_near(r["symbol"], r["isin"], r["ex_date"]):
            out[isin].append(lineage.Action(isin, r["ex_date"], "notice", 1.0, r["subject"]))
    return out


def _store(con: duckdb.DuckDBPyConnection, name: str, frame: pl.DataFrame) -> None:
    con.register("frame", frame)
    con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM frame')
    con.unregister("frame")


def _write(
    con: duckdb.DuckDBPyConnection, name: str, rows: list[dict], schema: dict[str, Any]
) -> None:
    _store(con, name, pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema))


REVIEW_SCHEMA = {
    "table": pl.Utf8,
    "symbol": pl.Utf8,
    "isin": pl.Utf8,
    "ex_date": pl.Date,
    "reason": pl.Utf8,
    "text": pl.Utf8,
    "hint": pl.Utf8,
    "evidence": pl.Utf8,
}
LINEAGE_SCHEMA = {
    "old_isin": pl.Utf8,
    "new_isin": pl.Utf8,
    "effective": pl.Date,
    "status": pl.Utf8,
    "reason": pl.Utf8,
    "evidence": pl.Utf8,
    "known_on": pl.Date,
}
CORRECTIONS_LOG_SCHEMA = {
    "line": pl.Int64,
    "op": pl.Utf8,
    "isin": pl.Utf8,
    "ex_date": pl.Date,
    "type": pl.Utf8,
    "status": pl.Utf8,
    "note": pl.Utf8,
}


def _read_lists(layout: Layout) -> tuple[list[lineage.SymbolChange], list[lineage.NameChange]]:
    changes: list[lineage.SymbolChange] = []
    names: list[lineage.NameChange] = []
    sc, nc = layout.lists / "symbolchange.csv", layout.lists / "namechange.csv"
    if sc.exists():
        changes = lineage.parse_symbol_changes(sc.read_text(encoding="utf-8", errors="replace"))
    if nc.exists():
        names = lineage.parse_name_changes(nc.read_text(encoding="utf-8", errors="replace"))
    return changes, names


def _link_known(effective: date, old_last: date, sessions: Sequence[date]) -> date | None:
    """When a succession decision was knowable: once every window the rules read has passed,
    whatever bars the data holds yet (a build stopped inside one must not date the link
    earlier). They read the new ISIN's bars EDGE_DAYS on, restructuring and name-change texts
    RESTRUCTURING_WINDOW on, and every other candidate (ambiguity), which may start up to
    REVIEW_GAP_SESSIONS sessions after the old ISIN's last day. None while the archive does not
    reach that session."""
    later = sessions[bisect_right(sessions, old_last) :]
    if len(later) <= lineage.REVIEW_GAP_SESSIONS:
        return None
    return max(
        effective + timedelta(days=EDGE_DAYS),
        effective + lineage.RESTRUCTURING_WINDOW,
        later[lineage.REVIEW_GAP_SESSIONS],
    )


def _lineage_row(d: lineage.Decision, known_on: date | None) -> dict:
    return {
        "old_isin": d.old_isin,
        "new_isin": d.new_isin,
        "effective": d.effective,
        "status": d.status,
        "reason": d.reason,
        "evidence": lineage.evidence_json(d),
        "known_on": known_on,
    }


def _build_lineage(
    con: duckdb.DuckDBPyConnection,
    layout: Layout,
    all_spans: Sequence[Span],
    sessions: Sequence[date],
    runs: Sequence[tuple],
    overrides: dict[tuple[str, str], str],
) -> tuple[list[lineage.Decision], Chains]:
    """Decide the ISIN successions and write `lineage` and `chain_map`."""
    seed = Chains(all_spans, [], runs)
    changes, names = _read_lists(layout)
    found = lineage.candidates(all_spans, sessions, changes)
    decisions = lineage.apply_overrides(
        lineage.decide_all(found, _lineage_actions(con, seed), names), overrides
    )
    links = [(d.old_isin, d.new_isin) for d in decisions if d.status == lineage.LINKED]
    chains = Chains(all_spans, links, runs)
    old_last = {(c.old.isin, c.new.isin): c.old.last_day for c in found}
    rows = [
        _lineage_row(d, _link_known(d.effective, old_last[(d.old_isin, d.new_isin)], sessions))
        for d in decisions
    ]
    _write(con, "lineage", rows, LINEAGE_SCHEMA)
    _write(
        con,
        "chain_map",
        [{"isin": i, "chain_isin": h, "depth": chains.depth[i]} for i, h in chains.head.items()],
        {"isin": pl.Utf8, "chain_isin": pl.Utf8, "depth": pl.Int64},
    )
    return decisions, chains


def _skip_sme(out: Outcome, p: Proposal, reason: str) -> None:
    """An SME-platform row the bars cannot place is skipped; a main-board one is reviewed."""
    (out.skipped if p.market == "sme" else out.review).append(_row(p, reason))


@dataclass
class Resolved:
    """The proposals placed on a chain, the resulting company each research row names
    ((file, line) -> ISIN, "" when unknown) and the bar windows the decisions read."""

    pairs: list[tuple[Proposal, str]] = field(default_factory=list)
    children: dict[tuple[str, int], str] = field(default_factory=dict)
    wanted: list[tuple[str, date, date]] = field(default_factory=list)


def _listed_as(symbol: str, ex: date, chains: Chains, parent: str) -> list[str]:
    """The ISINs that first traded under `symbol` from CHILD_SLACK before the ex-date on, outside
    the parent's chain: a resulting company is a new listing, never an older one or the parent."""
    if not symbol:
        return []
    since = ex - timedelta(days=CHILD_SLACK)
    return sorted(
        i
        for i, first, last in chains.by_symbol.get(symbol, ())
        if first >= since and last >= ex and chains.head.get(i, i) != parent
    )


def _children(p: Proposal, chain: str, chains: Chains) -> dict[tuple[str, int], str] | None:
    """(file, line) -> the resulting company each research row names ("" when unknown); None
    when a row's symbol names more than one new listing."""
    found = {}
    for r in p.research:
        named = (
            [r.resulting_isin]
            if r.resulting_isin
            else _listed_as(r.resulting_symbol, p.ex_date, chains, chain)
        )
        if len(named) > 1:
            return None
        found[(r.file, r.line)] = named[0] if named else ""
    return found


def _resolve(props: list[Proposal], chains: Chains, out: Outcome) -> Resolved:
    """Place each proposal on its chain; skip or review the ones that cannot be placed."""
    got = Resolved()
    for p in props:
        if p.reason.startswith(SKIP):
            out.skipped.append(_row(p, p.reason.removeprefix(SKIP)))
            continue
        if p.reason == UNMATCHED:  # reviewed as such, whether or not the bars know the company
            out.review.append(_row(p, p.reason))
            continue
        chain = chains.resolve(p.symbol, p.isin, p.ex_date)
        if chain is None:
            _skip_sme(out, p, "no_instrument")
            continue
        children = _children(p, chain, chains)
        if children is None:
            out.review.append(_row(p, "resulting_company_ambiguous"))
            continue
        got.pairs.append((p, chain))
        lo, hi = p.ex_date - timedelta(days=WINDOW_DAYS), p.ex_date + timedelta(days=WINDOW_DAYS)
        got.wanted += [(m, lo, hi) for m in chains.members[chain]]
        got.children |= children
        since = p.ex_date - timedelta(days=CHILD_SLACK)
        until = p.ex_date + timedelta(days=CHILD_DAYS)
        got.wanted += [(child, since, until) for child in children.values() if child]
    return got


def _group(
    pairs: list[tuple[Proposal, str]], series: dict[str, list[Bar]], out: Outcome
) -> tuple[dict[tuple[str, date, date], list[Proposal]], list[tuple[Proposal, str]]]:
    """Splits, bonuses and rights grouped by (chain, previous session, ex session); the
    demergers and cash events for later; rows decided by their text alone to review."""
    groups: dict[tuple[str, date, date], list[Proposal]] = defaultdict(list)
    later: list[tuple[Proposal, str]] = []
    for p, chain in pairs:
        if p.reason and not p.research:
            out.review.append(_row(p, p.reason))
            continue
        if p.table not in ("splits_bonus", "rights_issues"):
            later.append((p, chain))
            continue
        pair = around(series[chain], p.ex_date)
        if isinstance(pair, str):
            _skip_sme(out, p, pair)
            continue
        groups[(chain, pair[0].day, pair[1].day)].append(p)
    return groups, later


def _event_key(p: Proposal, chain: str, series: list[Bar]) -> tuple:
    """One event per chain and ex-date; a demerger's per session pair, so two feed rows dated
    either side of a holiday are one event."""
    pair = around(series, p.ex_date) if p.table == "demergers" else "cash"
    if isinstance(pair, str):
        return (chain, p.table, p.ex_date)
    return (chain, p.table, pair[0].day, pair[1].day)


def _same_day(accepted: list[dict], chain: str, series: list[Bar], ex: date) -> Fraction:
    """The product of the split, bonus and rights factors already accepted on the chain for a
    demerger's session pair."""
    pair = around(series, ex)
    if isinstance(pair, str):
        return Fraction(1)
    return math.prod(
        (
            fr(a["value"])
            for a in accepted
            if a["chain_isin"] == chain
            and a["status"] == ACCEPTED
            and a["action_type"] in PRICE_TYPES
            and pair[0].day < a["ex_date"] <= pair[1].day
        ),
        start=Fraction(1),
    )


def _add_cash(cash: dict[tuple[str, date], dict], key: tuple[str, date], got: dict) -> None:
    held = cash.get(key)
    if held is None:
        cash[key] = got
        return
    # a dividend and a bonus distribution on one day: one cash row
    held["cash"] += got["cash"]
    held["method"] += "+" + got["method"]
    held["basis"] += "+" + got["basis"]
    held["source_published_at"] = latest(held["source_published_at"], got["source_published_at"])
    if held["available_at"] is None or got["available_at"] is None:
        held["available_at"] = None
        held["available_basis"] = "research_undated"
    elif got["available_at"] > held["available_at"]:
        held["available_at"] = got["available_at"]
        held["available_basis"] = got["available_basis"]


def _decide_events(
    later: list[tuple[Proposal, str]],
    series: dict[str, list[Bar]],
    bars_by_isin: dict[str, list[Bar]],
    children: dict[tuple[str, int], str],
    index: dict[date, tuple[float, float]],
    out: Outcome,
) -> None:
    """Demergers, then cash (one row per chain and ex-date), onto `out`."""
    done: set[tuple] = set()
    cash: dict[tuple[str, date], dict] = {}
    for p, chain in sorted(later, key=lambda x: (x[0].table, x[0].ex_date)):
        key = _event_key(p, chain, series[chain])
        if key in done:
            out.skipped.append(_row(p, "duplicate_event"))
            continue
        done.add(key)
        if p.table == "demergers":
            same_day = _same_day(out.accepted, chain, series[chain], p.ex_date)
            out.accepted += decide_demerger(
                chain, p, series[chain], same_day, bars_by_isin, children, index, out
            )
            continue
        got = decide_cash(chain, p, series[chain], out)
        if got:
            _add_cash(cash, (chain, p.ex_date), got)
    out.accepted += list(cash.values())


def _write_results(
    con: duckdb.DuckDBPyConnection,
    layout: Layout,
    out: Outcome,
    log: list[dict],
    now: datetime | None,
) -> tuple[list[dict], int, pl.DataFrame]:
    """Number, stamp and write every version, the hindsight `actions` and the lists; returns
    (versions, withdrawn from the ledger, hindsight)."""
    found, withdrawn = versions.Ledger(layout.ledger).stamp(
        versions.number(out.accepted), now or versions.now_ist()
    )
    table = versions.frame(found).sort(*versions.EVENT, "version")
    hindsight = versions.select(table, None)
    _store(con, "factor_versions", table)
    _store(con, "actions", hindsight)
    _write(con, "review", out.review, REVIEW_SCHEMA)
    _write(con, "skipped", out.skipped, REVIEW_SCHEMA)
    _write(con, "corrections_log", log, CORRECTIONS_LOG_SCHEMA)
    return found, withdrawn, hindsight


def _count(rows: list[dict], key: str) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for r in rows:
        c[r[key]] += 1
    return dict(sorted(c.items()))


def build(layout: Layout, curation: Path, now: datetime | None = None) -> dict[str, Any]:
    all_spans = spans(layout)
    index = indices.levels(layout, factors.NEUTRAL_INDEX)
    sessions = bar_sessions(layout)
    runs = symbol_runs(layout)
    research = read_research(curation / "research")
    corrections = read_corrections(curation / "corrections.tsv")
    overrides_path = curation / "lineage_overrides.tsv"
    overrides = (
        lineage.read_overrides(overrides_path.read_text(encoding="utf-8"))
        if overrides_path.exists()
        else {}
    )

    with duckdb.connect(str(layout.db)) as con:
        # 1. lineage
        decisions, chains = _build_lineage(con, layout, all_spans, sessions, runs, overrides)

        # 2.-4. proposals against the bars
        out = Outcome()
        got = _resolve(proposals(con, research), chains, out)
        bars_by_isin = window(layout, got.wanted)
        series = {c: series_of(bars_by_isin, chains.members[c]) for c in {c for _, c in got.pairs}}
        groups, later = _group(got.pairs, series, out)
        for (chain, _, _), group in sorted(groups.items()):
            before, on = around(series[chain], group[0].ex_date)  # type: ignore[misc]
            out.accepted += decide_price_group(chain, before, on, group, out)
        _decide_events(later, series, bars_by_isin, got.children, index, out)

        # 5. corrections, then every version numbered, stamped and written
        log = apply_corrections(out.accepted, corrections, chains, chains.spans)
        found, withdrawn, hindsight = _write_results(con, layout, out, log, now)

    return {
        "lineage": _count([{"k": f"{d.status}:{d.reason}"} for d in decisions], "k"),
        "events": _count(hindsight.to_dicts(), "action_type"),
        "versions": _count(found, "available_basis"),
        "disputed": sum(v["status"] == DISPUTED for v in found),
        "available_at_unknown": sum(v["available_at"] is None for v in found),
        "withdrawn_from_ledger": withdrawn,
        "review": _count(out.review, "reason"),
        "skipped": _count(out.skipped, "reason"),
        "corrections": _count(log, "status"),
    }
