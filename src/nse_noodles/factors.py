"""Pure factor maths and the price gate. No I/O.

A price factor f multiplies every price before the ex-date. The witness is the ex-date gap:
the ex-date OPEN over the previous session's CLOSE. (The bhavcopy's PREVCLOSE on an ex-date is
already adjusted by NSE, so it cannot witness anything; the open is the first price that
knows about the action.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction

# NSE's widest daily price band is 20%: a real action's ex-date open is within 20% of the
# previous close times its factor. On ~700 confirmed split and bonus factors the median miss
# was 2.6%, 90% within 10%, 95% within 16%.
GATE_TOL = 0.20
# A demerger priced from the gap alone needs a clear drop: opens 10% or more below the previous
# close are rare (well under 1% of sessions), so on a date a source names, such a drop is the event.
DEMERGER_MIN_DROP = Fraction(1, 10)
SETTLE_SESSIONS = 5  # an uncross-checked gap must still hold at this session's close
# A demerger's ex-date gap is divided by this index's move over the same night (its open over
# its previous close), so the market's move that morning is not read as part of the action. NIFTY
# 500 is the broadest NSE index in the daily archive for the whole index history (from 2012); a
# sector index would need a point-in-time stock-to-sector map that NSE's free archives do not
# give. The stock's own beta and sector, and its news that morning, stay in the factor.
NEUTRAL_INDEX = "NIFTY 500"
# Two estimates of one demerger's factor (the ex-date gap and the ratio priced at the resulting
# company's first close) further apart than this, in log terms, are a review case: the later
# one is recorded as `disputed` and never used.
DEMERGER_VERSION_TOL = 0.10
MAX_YIELD = Decimal("0.5")  # cash above half the previous close is a review case, never stored
BONUS_INSTRUMENT_MIN = Decimal("0.01")  # a bonus debenture worth >= 1% of the price is cash
JUMP_TOL = 0.25  # an adjusted session move beyond 25% needs a factor (beyond NSE's widest band)


def rights_factor(new: Fraction, held: Fraction, price: Fraction, cum: Fraction) -> Fraction:
    """TERP over the cum price for M=`new` shares per N=`held` at issue price S=`price`, with
    P=`cum` the previous close: (N*P + M*S) / ((N+M)*P). 1 when S >= P (no value passes to the
    holder). A partly-paid issue uses the full issue price (the TERP convention)."""
    if cum <= 0 or new <= 0 or held <= 0 or price < 0:
        raise ValueError("rights_factor needs positive terms and price")
    if price >= cum:
        return Fraction(1)
    return (held * cum + new * price) / ((held + new) * cum)


def terp(new: Fraction, held: Fraction, price: Fraction, cum: Fraction) -> Fraction:
    """The theoretical ex-rights price itself."""
    return (held * cum + new * price) / (held + new)


def bonus_factor(new: int | Fraction, held: int | Fraction) -> Fraction:
    """x new for y held: y / (x + y)."""
    return Fraction(held) / (Fraction(new) + Fraction(held))


def public_only_bonus_factor(shares_before: Fraction, shares_after: Fraction) -> Fraction:
    """A bonus to public holders only (promoters waived) changes the count by less than the
    stated ratio: the factor is old shares / new shares, from the allotment filing."""
    if shares_before <= 0 or shares_after < shares_before:
        raise ValueError("share counts")
    return shares_before / shares_after


def ratio_factor(
    priced: list[tuple[Fraction, Fraction, Fraction]], parent_close: Fraction
) -> Fraction | None:
    """A demerger cross-check: 1 - sum(a_i/b_i * C_i) / P over the resulting companies (a_i
    shares for b_i parent shares, C_i the resulting company's first close). None outside (0, 1)."""
    if not priced or parent_close <= 0 or any(c <= 0 or b <= 0 for _, b, c in priced):
        return None
    f = 1 - sum(a / b * c for a, b, c in priced) / parent_close
    return f if 0 < f < 1 else None


def coa_factor(
    parent_pct: Fraction | None, resulting_pcts: list[Fraction | None]
) -> Fraction | str | None:
    """A demerger cross-check from the company's cost-of-acquisition apportionment (a
    book-value tax split, NOT a market price): parent % / 100. None when not given; a reason
    when out of range or the percentages do not add up to 100 (+-0.5)."""
    if parent_pct is None:
        return None
    if not 0 < parent_pct < 100:
        return "coa_out_of_range"
    complete = bool(resulting_pcts) and all(p is not None for p in resulting_pcts)
    if complete and abs(parent_pct + sum(resulting_pcts) - 100) > Fraction(1, 2):  # type: ignore[arg-type]
        return "coa_does_not_sum"
    return parent_pct / 100


@dataclass(frozen=True)
class Verdict:
    """`reason` is "" when the bars confirm the factor, else `off_factor` or `ambiguous`."""

    reason: str
    gap: Fraction

    @property
    def confirmed(self) -> bool:
        return not self.reason


def gate(expected: Fraction, previous_close: Fraction, ex_open: Fraction) -> Verdict:
    """Do the bars confirm `expected` (times any other factor on the same ex-date)? The gap
    must be within GATE_TOL of it, and at least twice as close to it as to 1 in log terms;
    otherwise a plain session move could explain the gap as well (`ambiguous`)."""
    if expected <= 0 or previous_close <= 0 or ex_open <= 0:
        raise ValueError("gate needs positive prices and factor")
    gap = ex_open / previous_close
    off = abs(math.log(gap / expected))
    move = abs(math.log(gap))
    if off > math.log1p(GATE_TOL):
        return Verdict("off_factor", gap)
    if 2 * off > move:
        return Verdict("ambiguous", gap)
    return Verdict("", gap)


def agrees(gap: Fraction, check: Fraction) -> bool:
    """Does a cross-check explain a market gap? The gate with the check as the expected value."""
    return gate(check, Fraction(1), gap).confirmed


def market_factor(
    gap: Fraction, checks: dict[str, Fraction], date_confirmed: bool, settled: Fraction | None
) -> tuple[Fraction | None, str]:
    """A demerger's ex-date factor is the MARKET gap (index-neutralised by the caller), never a
    cross-check. Stored when a cross-check (ratio x resulting price, CoA %) agrees;
    otherwise only when the date is confirmed, the drop is at least DEMERGER_MIN_DROP and the
    close SETTLE_SESSIONS on still agrees with the gap (`settled`), so a pre-open discovery the
    market reverses is not stored. Returns (factor or None, reason)."""
    if not 0 < gap < 1:
        return None, "no_drop"
    agreeing = [k for k, v in checks.items() if agrees(gap, v)]
    if agreeing:
        return gap, "agrees_" + "+".join(agreeing)
    why = "gap_disagrees" if checks else "no_cross_check"
    if not date_confirmed:
        return None, f"{why}_date_unconfirmed"
    if gap > 1 - DEMERGER_MIN_DROP:
        return None, f"{why}_gap_not_material"
    if settled is None or not agrees(settled, gap):
        return None, f"{why}_gap_not_held"
    return gap, f"{why}_material_drop_held"


def index_move(
    levels: dict[date, tuple[float, float]], start: date, end: date, at_open: bool
) -> Fraction | None:
    """The index's move from the close of `start` to the open (or close) of `end`, from
    {day: (open, close)}; None when either day is missing."""
    a, b = levels.get(start), levels.get(end)
    if not a or not b or a[1] <= 0 or b[0 if at_open else 1] <= 0:
        return None
    return Fraction(str(b[0 if at_open else 1])) / Fraction(str(a[1]))


def versions_agree(a: Fraction, b: Fraction, tol: float = DEMERGER_VERSION_TOL) -> bool:
    return abs(math.log(a / b)) <= math.log1p(tol)


def band(estimates: list[Fraction]) -> tuple[Fraction, Fraction]:
    """A demerger's uncertainty band: the range of the estimates known for it (raw gap,
    index-neutralised gap, ratio-priced factor). Not a confidence interval: news about the stock
    itself that morning moves every open-based estimate alike and is not in the band."""
    return min(estimates), max(estimates)


def dividend_factor(amount: Decimal, previous_close: Decimal) -> Decimal | None:
    """The total-return factor of a cash distribution: 1 - D / the raw close before the
    ex-date. None unless it lies in (0, 1)."""
    if previous_close <= 0 or amount <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 30
        f = 1 - amount / previous_close
    return f if 0 < f < 1 else None


def over_yield(amount: Decimal, previous_close: Decimal) -> bool:
    return previous_close <= 0 or amount > MAX_YIELD * previous_close


def bonus_instrument_counts(value: Decimal, previous_close: Decimal) -> bool:
    """A bonus of debentures / preference shares worth at least 1% of the previous close is a
    cash distribution of its value (a total-return factor, with P&L credited as cash); below
    1% it is treated as price-neutral."""
    return previous_close > 0 and value >= BONUS_INSTRUMENT_MIN * previous_close


def is_jump(previous_close: float, open_: float, tol: float = JUMP_TOL) -> bool:
    return previous_close > 0 and abs(open_ - previous_close) > tol * previous_close
