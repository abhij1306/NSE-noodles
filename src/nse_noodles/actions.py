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
(from `ca_db`) is the first NSE filing found for the event.
"""

from __future__ import annotations

import json
import math
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
RECORD_DAYS = 7  # a record date falls this soon after the ex-date
DISPUTE_DAYS = 60
# feed dividend statuses that are not a cash dividend on an equity share: skipped, not reviewed
SKIP_DIVIDEND = ("not_equity_share", "not_equity_series", "preference_dividend", "no_dividend")
BONUS_BEFORE, BONUS_AFTER = 7, 3  # NSE's ex-date vs a researched bonus-debenture date


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
        return sorted(found)[0] if len(found) == 1 else None

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
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    if not exists or not exists[0]:
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


def proposals(con: duckdb.DuckDBPyConnection, research: Sequence[ResearchRow]) -> list[Proposal]:
    out: list[Proposal] = []
    for r in _rows(con, "splits_bonus"):
        if r["ex_date"] is None:
            continue
        p = Proposal(
            "splits_bonus", r["symbol"], r["isin"], r["ex_date"], r["subject"], r["market"]
        )
        p.hint = f"class {r['instrument_class']} ({r['classification_source'] or 'no evidence'})"
        if r["parse_status"] != "ok":
            p.reason = (
                "skip:non_equity_bonus" if parse.non_equity_bonus(r["subject"]) else "unparsed"
            )
        else:
            p.legs = _legs(r["legs"])
        out.append(_noticed(p, r))
    for r in _rows(con, "rights_issues"):
        if r["ex_date"] is None:
            continue
        p = Proposal(
            "rights_issues", r["symbol"], r["isin"], r["ex_date"], r["subject"], r["market"]
        )
        p.hint = f"FV {r['face_value']}"
        if r["parse_status"] == "ok":
            p.rights = parse.RightsTerms(
                fr(r["ratio_new"]), fr(r["ratio_held"]), fr(r["issue_price"])
            )
        else:
            p.reason = r["parse_status"]
        out.append(_noticed(p, r))
    for r in _rows(con, "demergers"):
        if r["ex_date"] is not None:
            out.append(
                _noticed(
                    Proposal(
                        "demergers", r["symbol"], r["isin"], r["ex_date"], r["subject"], r["market"]
                    ),
                    r,
                )
            )
    for r in _rows(con, "bonus_instruments"):
        if r["ex_date"] is not None:
            out.append(
                _noticed(
                    Proposal(
                        "bonus_instruments",
                        r["symbol"],
                        r["isin"],
                        r["ex_date"],
                        r["subject"],
                        r["market"],
                    ),
                    r,
                )
            )
    for r in _rows(con, "dividends"):
        p = Proposal("dividends", r["symbol"], r["isin"], r["ex_date"], r["subjects"], r["market"])
        if r["status"] == "ok":
            p.cash = Decimal(str(r["amount"]))
            p.hint = r["kinds"]
        else:
            skip = r["status"] in SKIP_DIVIDEND
            p.reason = ("skip:" if skip else "") + r["status"]
        out.append(_noticed(p, r))

    table_of = {
        "demerger": "demergers",
        "rights": "rights_issues",
        "bonus_instrument": "bonus_instruments",
        "split_bonus": "splits_bonus",
    }
    used: set[int] = set()
    for p in out:
        for i, r in enumerate(research):
            if table_of.get(r.table) == p.table and _matches(r, p):
                p.research.append(r)
                used.add(i)
        # research saying the row itself is wrong (another ISIN, date or event) is never used
        # for its terms: the fix is a correction line, and the row waits in review until then
        if any(r.wrong.startswith("yes") for r in p.research):
            p.reason, p.research = "research_says_row_wrong", []
    # research events NSE's feed does not carry become proposals of their own
    extra: dict[tuple[str, str, date], Proposal] = {}
    for i, r in enumerate(research):
        if i in used or r.ex_date is None or r.table not in ("demerger", "rights"):
            continue
        key = (r.table, r.symbol or r.isin, r.ex_date)
        if key not in extra:
            extra[key] = Proposal(
                table_of[r.table],
                r.symbol,
                r.isin,
                r.ex_date,
                f"research {r.table}",
                source="research",
            )
        extra[key].research.append(r)
    return out + list(extra.values())


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


def _accept(
    chain: str,
    before: Bar,
    p: Proposal,
    kind: str,
    factor: Fraction | None,
    method: str,
    evidence: dict,
    known: Known,
    cash: Decimal | None = None,
    *,
    basis: str,
    terms: Sequence[ResearchRow] = (),
    status: str = ACCEPTED,
    raw_gap: Fraction | None = None,
    band: tuple[Fraction, Fraction] | None = None,
) -> dict:
    """One version of an event. `terms`: the research rows its value reads (their latest
    `source_date` is its `source_published_at`); without them, the first NSE filing found."""
    sources = [
        {"file": r.file, "line": r.line, "url": r.source_url, "confidence": r.confidence}
        for r in p.research
    ]
    if sources:
        evidence = {**evidence, "research": sources}
    published = latest(*(end_of(r.source_date) for r in terms)) if terms else end_of(p.announced_on)
    return {
        "chain_isin": chain,
        "isin": before.isin,
        "symbol": before.symbol,
        "ex_date": p.ex_date,
        "action_type": kind,
        "status": status,
        "value": float(factor) if factor is not None else 1.0,
        "cash": float(cash) if cash is not None else None,
        "basis": basis,
        "raw_gap": float(raw_gap) if raw_gap is not None else None,
        "band_low": float(band[0]) if band else None,
        "band_high": float(band[1]) if band else None,
        "method": method,
        "source": "research"
        if (p.research and kind in ("rights", "demerger")) or p.source == "research"
        else p.source,
        "text": p.text[:500],
        "evidence": json.dumps(evidence, default=str, sort_keys=True),
        "announced_on": p.announced_on,
        "notice_url": p.notice_url,
        "source_published_at": published,
        "available_at": known[0],
        "available_basis": known[1],
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


def _rights_terms(p: Proposal) -> parse.RightsTerms | str:
    """The terms to price: the research's when it is confident and complete, else NSE's."""
    best = [r for r in p.research if r.confidence >= 1 and r.ratio and r.issue_price]
    if [r for r in p.research if r.non_equity]:
        return "non_simple_research"
    if best:
        r = best[0]
        return parse.RightsTerms(r.ratio[0], r.ratio[1], r.issue_price)  # type: ignore[index, arg-type]
    if p.research and all(r.confidence == 0 for r in p.research):
        return "low_confidence"
    return p.rights if p.rights is not None else (p.reason or "unparsed")


def decide_price_group(
    chain: str, before: Bar, on: Bar, group: list[Proposal], out: Outcome
) -> list[dict]:
    """Gate the product of every split/bonus/rights factor of one chain and session pair."""
    prev, ex_open = fr(before.close), fr(on.open)
    legs: list[tuple[Proposal, str, Fraction]] = []
    seen: set[tuple[str, Fraction]] = set()
    for p in group:
        if p.table == "rights_issues":
            terms = _rights_terms(p)
            if isinstance(terms, str):
                out.review.append(_row(p, terms, _bar_evidence(before, on)))
                continue
            f = factors.rights_factor(terms.new, terms.held, terms.price, prev)
            if f == 1:
                out.skipped.append(_row(p, "rights_at_or_above_price"))
                continue
            items = [("rights", f)]
        else:
            items = [(leg.action_type, leg.factor) for leg in p.legs]
        for kind, f in items:
            if (kind, f) not in seen:
                seen.add((kind, f))
                legs.append((p, kind, f))
    if not legs:
        return []
    kinds = [k for _, k, _ in legs]
    if len(set(kinds)) != len(kinds):
        for p in {id(p): p for p, _, _ in legs}.values():
            out.review.append(_row(p, "conflicting_rows", _bar_evidence(before, on)))
        return []
    expected = math.prod((f for _, _, f in legs), start=Fraction(1))
    verdict = factors.gate(expected, prev, ex_open)
    evidence = {
        **_bar_evidence(before, on),
        "expected": f4(expected),
        "verdict": verdict.reason or "confirmed",
    }
    rights_only = all(k == "rights" for k in kinds)
    research_terms = [
        r
        for p, k, _ in legs
        if k == "rights"
        for r in p.research
        if r.confidence >= 1 and r.ratio and r.issue_price
    ]
    known: Known = (
        research_known(research_terms, at_open(on.day))
        if research_terms
        else (at_open(on.day), "ex_open")
    )
    if not verdict.confirmed and not (rights_only and verdict.reason == "ambiguous"):
        for p in {id(p): p for p, _, _ in legs}.values():
            out.review.append(_row(p, verdict.reason, evidence))
        return []
    accepted = []
    for p, kind, f in legs:
        method = "gate_confirmed" if verdict.confirmed else "terp_price_unconfirmed"
        if kind == "rights":
            method = ("terp; " + method) if verdict.confirmed else method
        terms = [r for r in research_terms if r in p.research] if kind == "rights" else []
        accepted.append(
            _accept(
                chain,
                before,
                p,
                kind,
                f,
                method,
                {**evidence, "hint": p.hint},
                known,
                basis="terp" if kind == "rights" else "stated_ratio",
                terms=terms,
            )
        )
    return accepted


def _dates_disputed(p: Proposal) -> bool:
    for r in p.research:
        for kind, day in (("ex", r.ex_date), ("record", r.record_date)):
            if day is None or abs((day - p.ex_date).days) > DISPUTE_DAYS:
                continue
            ok = day == p.ex_date if kind == "ex" else 0 <= (day - p.ex_date).days <= RECORD_DAYS
            if not ok:
                return True
    return False


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
    before, on = pair
    legs = [r for r in p.research if r.leg_type in ("demerger", "")]
    if p.research and not legs:
        out.skipped.append(_row(p, "amalgamation_only"))
        return []
    if legs and min(r.confidence for r in legs) < 1:
        out.review.append(_row(p, "low_confidence"))
        return []
    if _dates_disputed(p):
        out.review.append(_row(p, "date_disputed"))
        return []
    prev = fr(before.close)
    raw = fr(on.open) / prev / same_day
    overnight = factors.index_move(index, before.day, on.day, at_open=True)
    gap = raw / overnight if overnight else raw

    def neutral(price: Fraction, day: date) -> Fraction:
        """A close of `day` brought back to the previous close's market level."""
        drift = factors.index_move(index, before.day, day, at_open=False)
        return price / drift if drift else price

    checks: dict[str, Fraction] = {}
    check_known: dict[str, Known] = {}
    priced = []
    first_closes = []
    for r in legs:
        child = resulting.get(children.get((r.file, r.line), ""), [])
        first = next((b for b in child if b.day >= p.ex_date), None)
        if r.ratio and first is not None and first.close > 0:
            priced.append((r.ratio[0], r.ratio[1], neutral(fr(first.close), first.day)))
            first_closes.append(first.day)
    if legs and len(priced) == len(legs) and same_day == 1:
        rf = factors.ratio_factor(priced, prev)
        if rf is not None:
            checks["ratio"] = rf
            ratio_known, why = research_known(legs, at_close(max(first_closes)))
            check_known["ratio"] = (ratio_known, "resulting_first_close" if ratio_known else why)
    coa = factors.coa_factor(
        next((r.coa_parent for r in legs if r.coa_parent is not None), None),
        [r.coa_resulting for r in legs],
    )
    note = coa if isinstance(coa, str) else ""
    if isinstance(coa, Fraction):
        checks["coa"] = coa
        check_known["coa"] = research_known(legs, at_open(on.day))
    later = [b for b in series if b.day >= on.day]
    settled = None
    if len(later) >= factors.SETTLE_SESSIONS:
        last = later[factors.SETTLE_SESSIONS - 1]
        settled = neutral(fr(last.close), last.day) / prev / same_day
    factor, why = factors.market_factor(gap, checks, True, settled)
    evidence = {
        **_bar_evidence(before, on),
        "raw_gap": f4(raw),
        "index": factors.NEUTRAL_INDEX if overnight else "missing",
        "index_overnight": f4(overnight) if overnight else None,
        "market_gap": f4(gap),
        "same_day_factor": f4(same_day),
        "settled": f4(settled) if settled is not None else None,
        "checks": {k: f4(v) for k, v in checks.items()},
        "note": note,
    }
    if factor is None:
        out.review.append(_row(p, why if p.research else f"needs_research:{why}", evidence))
        return []
    method = "market_gap; " + "; ".join(
        f"{k} {f4(v)}" + ("" if factors.agrees(gap, v) else " disagrees") for k, v in checks.items()
    )
    # usable as soon as the first route that accepts it was knowable: an agreeing check, or the
    # gap holding SETTLE_SESSIONS on
    routes = [check_known[k] for k, v in checks.items() if factors.agrees(gap, v)]
    if factors.market_factor(gap, {}, True, settled)[0] is not None:
        routes.append((at_close(later[factors.SETTLE_SESSIONS - 1].day), "settled_close"))
    dated = [r for r in routes if r[0] is not None]
    known = min(dated) if dated else routes[0]  # type: ignore[type-var]
    found = [
        _accept(
            chain,
            before,
            p,
            "demerger",
            factor,
            method.rstrip("; "),
            evidence,
            known,
            basis="index_neutral_gap" if overnight else "raw_gap",
            terms=legs if known[1] in ("research_source_date", "resulting_first_close") else (),
            raw_gap=raw,
            band=factors.band([raw, gap]),
        )
    ]
    ratio = checks.get("ratio")
    if ratio is not None:
        agree = factors.versions_agree(gap, ratio)
        found.append(
            _accept(
                chain,
                before,
                p,
                "demerger",
                ratio,
                f"resulting_ratio; ex-date version {f4(gap)}"
                + ("" if agree else " disagrees: review"),
                evidence,
                check_known["ratio"],
                basis="resulting_ratio",
                terms=legs,
                status=ACCEPTED if agree else DISPUTED,
                raw_gap=raw,
                band=factors.band([raw, gap, ratio]),
            )
        )
        if not agree:
            out.review.append(_row(p, "demerger_versions_disagree", evidence))
    return found


def decide_cash(chain: str, p: Proposal, series: list[Bar], out: Outcome) -> dict | None:
    before = [b for b in series if b.day < p.ex_date and (p.ex_date - b.day).days <= BAR_DAYS]
    if not before:
        reason = "before_bars" if series and p.ex_date <= series[0].day else "no_bars"
        (out.skipped if reason == "before_bars" else out.review).append(_row(p, reason))
        return None
    prev = before[-1]
    p_close = Decimal(str(prev.close))
    cash = p.cash
    method = "dividend"
    basis = "feed_amount"
    terms: list[ResearchRow] = []
    known: Known = (at_open(p.ex_date), "ex_open")
    if p.table == "bonus_instruments":
        rows = [r for r in p.research if r.value_per_share]
        if not rows:
            out.review.append(_row(p, "needs_value"))
            return None
        r = rows[0]
        if r.confidence < 1:
            out.review.append(_row(p, "low_confidence"))
            return None
        if any(
            w in f"{r.notes} {r.non_equity}".lower()
            for w in ("non-promoter", "non promoter", "public only", "promoters excluded")
        ):
            out.review.append(_row(p, "some_holders_only"))
            return None
        cash = Decimal(r.value_per_share.numerator) / Decimal(r.value_per_share.denominator)  # type: ignore[union-attr]
        if not factors.bonus_instrument_counts(cash, p_close):
            out.skipped.append(_row(p, "below_one_percent"))
            return None
        method, basis, terms = "bonus_instrument_as_cash", "research_value", [r]
        known = research_known([r], at_open(p.ex_date))
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
    return _accept(
        chain, prev, p, "dividend", None, method, evidence, known, cash, basis=basis, terms=terms
    )


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
    log = []
    live: dict[tuple, dict | None] = {}  # the event's current version; None once deleted
    for v in found:
        if v["status"] == ACCEPTED:
            live[(v["chain_isin"], v["ex_date"], v["action_type"])] = v

    def version(
        c: Correction, held: dict, ex: date, value: float, how: str, basis: str, when: Known
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
        found.append(v)
        return v

    for c in corrections:
        chain = chains.head.get(c.isin, c.isin)
        key = (chain, c.ex_date, c.action_type)
        held = live.get(key)
        status = "applied"
        value = float(c.value) if c.value is not None else 1.0
        when: Known = (end_of(c.known_on), "correction" if c.known_on else "correction_undated")
        if c.op == "add":
            if held is not None:
                status = "exists_use_replace"
            else:
                base = {
                    "chain_isin": chain,
                    "isin": c.isin,
                    "symbol": spans_by[c.isin].last_symbol if c.isin in spans_by else "",
                    "action_type": c.action_type,
                    "announced_on": None,
                    "notice_url": "",
                }
                live[key] = version(c, base, c.ex_date, value, "correction", "correction", when)
        elif held is None:
            status = "not_found"
        elif c.op == "delete":
            version(c, held, c.ex_date, 0.0 if c.action_type == "dividend" else 1.0,
                    "correction: deleted", "correction_delete", when)  # fmt: skip
            live[key] = None
        elif c.op == "replace":
            was = held["cash"] if c.action_type == "dividend" else held["value"]
            live[key] = version(
                c, held, c.ex_date, value, f"correction (was {was})", "correction", when
            )
        elif c.op == "move" and live.get((chain, c.new_ex_date, c.action_type)) is not None:
            status = "destination_exists"  # never overwrite: replace or delete that one first
        elif c.op == "move":
            kept = held["cash"] if c.action_type == "dividend" else held["value"]
            version(c, held, c.ex_date, 0.0 if c.action_type == "dividend" else 1.0,
                    f"correction: moved to {c.new_ex_date}", "correction_delete", when)  # fmt: skip
            moved = (latest(held["available_at"], when[0]), when[1])
            live[key] = None
            live[(chain, c.new_ex_date, c.action_type)] = version(
                c,
                held,
                c.new_ex_date,  # type: ignore[arg-type]
                kept,
                f"correction (moved from {c.ex_date})",
                "correction_move",
                moved,
            )
        log.append(
            {
                "line": c.line,
                "op": c.op,
                "isin": c.isin,
                "ex_date": c.ex_date,
                "type": c.action_type,
                "status": status,
                "note": c.note[:300],
            }
        )
    return log


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


def _write(
    con: duckdb.DuckDBPyConnection, name: str, rows: list[dict], schema: dict[str, Any]
) -> None:
    frame = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    con.register("frame", frame)
    con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM frame')
    con.unregister("frame")


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
        seed = Chains(all_spans, [], runs)
        changes = names = []
        sc, nc = layout.lists / "symbolchange.csv", layout.lists / "namechange.csv"
        if sc.exists():
            changes = lineage.parse_symbol_changes(sc.read_text(encoding="utf-8", errors="replace"))
        if nc.exists():
            names = lineage.parse_name_changes(nc.read_text(encoding="utf-8", errors="replace"))
        found = lineage.candidates(all_spans, sessions, changes)
        decisions = lineage.apply_overrides(
            lineage.decide_all(found, _lineage_actions(con, seed), names), overrides
        )
        links = [(d.old_isin, d.new_isin) for d in decisions if d.status == lineage.LINKED]
        chains = Chains(all_spans, links, runs)
        _write(
            con,
            "lineage",
            [
                {
                    "old_isin": d.old_isin,
                    "new_isin": d.new_isin,
                    "effective": d.effective,
                    "status": d.status,
                    "reason": d.reason,
                    "evidence": lineage.evidence_json(d),
                    # the link reads the new ISIN's bars up to EDGE_DAYS after its first day:
                    # known once that whole window has passed, whatever bars the data holds
                    # yet (a build stopped inside the window must not date the link earlier)
                    "known_on": d.effective + timedelta(days=EDGE_DAYS),
                }
                for d in decisions
            ],
            LINEAGE_SCHEMA,
        )
        _write(
            con,
            "chain_map",
            [
                {"isin": i, "chain_isin": h, "depth": chains.depth[i]}
                for i, h in chains.head.items()
            ],
            {"isin": pl.Utf8, "chain_isin": pl.Utf8, "depth": pl.Int64},
        )

        # 2.-4. proposals against the bars
        props = proposals(con, research)
        out = Outcome()
        resolved: list[tuple[Proposal, str]] = []
        children: dict[tuple[str, int], str] = {}
        wanted: list[tuple[str, date, date]] = []
        for p in props:
            if p.reason.startswith("skip:"):
                out.skipped.append(_row(p, p.reason.removeprefix("skip:")))
                continue
            chain = chains.resolve(p.symbol, p.isin, p.ex_date)
            if chain is None:
                (out.skipped if p.market == "sme" else out.review).append(_row(p, "no_instrument"))
                continue
            resolved.append((p, chain))
            lo, hi = (
                p.ex_date - timedelta(days=WINDOW_DAYS),
                p.ex_date + timedelta(days=WINDOW_DAYS),
            )
            wanted += [(m, lo, hi) for m in chains.members[chain]]
            for r in p.research:
                child = r.resulting_isin or ""
                if not child and r.resulting_symbol:
                    child = next(
                        (
                            i
                            for i, f, la in chains.by_symbol.get(r.resulting_symbol, ())
                            if la >= p.ex_date
                        ),
                        "",
                    )
                children[(r.file, r.line)] = child
                if child:
                    wanted.append(
                        (
                            child,
                            p.ex_date - timedelta(days=5),
                            p.ex_date + timedelta(days=CHILD_DAYS),
                        )
                    )
        bars_by_isin = window(layout, wanted)
        series = {c: series_of(bars_by_isin, chains.members[c]) for c in {c for _, c in resolved}}

        groups: dict[tuple[str, date, date], list[Proposal]] = defaultdict(list)
        later: list[tuple[Proposal, str]] = []
        for p, chain in resolved:
            if p.reason and not p.research:
                out.review.append(_row(p, p.reason))
                continue
            if p.table in ("splits_bonus", "rights_issues"):
                pair = around(series[chain], p.ex_date)
                if isinstance(pair, str):
                    (out.skipped if p.market == "sme" else out.review).append(_row(p, pair))
                    continue
                groups[(chain, pair[0].day, pair[1].day)].append(p)
            else:
                later.append((p, chain))
        for (chain, _, _), group in sorted(groups.items()):
            before, on = around(series[chain], group[0].ex_date)  # type: ignore[misc]
            out.accepted += decide_price_group(chain, before, on, group, out)

        done: set[tuple[str, date, str]] = set()
        cash: dict[tuple[str, date], dict] = {}
        for p, chain in sorted(later, key=lambda x: (x[0].table, x[0].ex_date)):
            if (chain, p.ex_date, p.table) in done:
                continue
            done.add((chain, p.ex_date, p.table))
            if p.table == "demergers":
                pair = around(series[chain], p.ex_date)
                same_day = Fraction(1)
                if not isinstance(pair, str):
                    same_day = math.prod(
                        (
                            fr(a["factor"])
                            for a in out.accepted
                            if a["chain_isin"] == chain
                            and pair[0].day < a["ex_date"] <= pair[1].day
                        ),
                        start=Fraction(1),
                    )
                out.accepted += decide_demerger(
                    chain, p, series[chain], same_day, bars_by_isin, children, index, out
                )
            else:
                got = decide_cash(chain, p, series[chain], out)
                if got:
                    held = cash.get((chain, p.ex_date))
                    if held is None:
                        cash[(chain, p.ex_date)] = got
                    else:  # a dividend and a bonus distribution on one day: one cash row
                        held["cash"] += got["cash"]
                        held["method"] += "+" + got["method"]
                        held["basis"] += "+" + got["basis"]
                        held["source_published_at"] = latest(
                            held["source_published_at"], got["source_published_at"]
                        )
                        if held["available_at"] is None or got["available_at"] is None:
                            held["available_at"] = None
                            held["available_basis"] = "research_undated"
                        elif got["available_at"] > held["available_at"]:
                            held["available_at"] = got["available_at"]
                            held["available_basis"] = got["available_basis"]
        out.accepted += list(cash.values())

        # 5. corrections, then every version numbered, stamped and written
        log = apply_corrections(out.accepted, corrections, chains, chains.spans)
        found, withdrawn = versions.Ledger(layout.ledger).stamp(
            versions.number(out.accepted), now or versions.now_ist()
        )
        table = versions.frame(found).sort(*versions.EVENT, "version")
        hindsight = versions.select(table, None)
        for name, frame in (("factor_versions", table), ("actions", hindsight)):
            con.register("frame", frame)
            con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM frame')
            con.unregister("frame")
        _write(con, "review", out.review, REVIEW_SCHEMA)
        _write(con, "skipped", out.skipped, REVIEW_SCHEMA)
        _write(
            con,
            "corrections_log",
            log,
            {
                "line": pl.Int64,
                "op": pl.Utf8,
                "isin": pl.Utf8,
                "ex_date": pl.Date,
                "type": pl.Utf8,
                "status": pl.Utf8,
                "note": pl.Utf8,
            },
        )

    def count(rows: list[dict], key: str) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for r in rows:
            c[r[key]] += 1
        return dict(sorted(c.items()))

    return {
        "lineage": count([{"k": f"{d.status}:{d.reason}"} for d in decisions], "k"),
        "events": count(hindsight.to_dicts(), "action_type"),
        "versions": count(found, "available_basis"),
        "disputed": sum(v["status"] == DISPUTED for v in found),
        "available_at_unknown": sum(v["available_at"] is None for v in found),
        "withdrawn_from_ledger": withdrawn,
        "review": count(out.review, "reason"),
        "skipped": count(out.skipped, "reason"),
        "corrections": count(log, "status"),
    }
