"""Pure parsers for NSE corporate-action purpose texts. No I/O.

Convention: a price factor multiplies every price BEFORE the ex-date and lies in (0, 1). A
1:2 face-value split (Rs 10 to Rs 5) is 1/2; a bonus of x new for y held is y / (x + y). A text
that does not parse cleanly gives None (or a reason string), never a guessed factor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction

SPLIT, BONUS, RIGHTS, DEMERGER, OTHER, DIVIDEND = (
    "split",
    "bonus",
    "rights",
    "demerger",
    "other",
    "dividend",
)
PRICE_TYPES = (SPLIT, BONUS, RIGHTS, DEMERGER, OTHER)

_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_NSE_DATE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_NUMBER = r"(\d+(?:\.\d+)?)"


def nse_date(raw: str | None) -> date | None:
    """`04-Sep-2014` (NSE's feed format) or ISO `2014-09-04`; anything else is None."""
    raw = (raw or "").strip()
    found = _NSE_DATE.fullmatch(raw)
    if found and found.group(2).lower() in _MONTHS:
        try:
            return date(int(found[3]), _MONTHS.index(found[2].lower()) + 1, int(found[1]))
        except ValueError:
            return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ORDINAL = r"(\d{1,2})(?:st|nd|rd|th)?"
_NAMED_DATES = (  # (pattern, order of day / month / year groups)
    (
        re.compile(rf"\b{_ORDINAL}[\s\-]*(?:of\s+|day\s+of\s+)?{_MONTH}[\s,\-]*(\d{{4}})\b", re.I),
        "dmy",
    ),
    (re.compile(rf"\b{_MONTH}\s+{_ORDINAL},?\s*(\d{{4}})\b", re.I), "mdy"),
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b"), "dmy"),  # Indian order: day first
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
)


def dates_named(text: str) -> set[date]:
    """Every calendar date written in a filing's text: `5th September, 2026`, `September 05,
    2026`, `05-Sep-2026`, `05/09/2026` (day first, as Indian filings write it), `2026-09-05`.
    A string that is no valid date is ignored."""
    out = set()
    for pattern, order in _NAMED_DATES:
        for m in pattern.finditer(text):
            parts = dict(zip(order, m.groups(), strict=True))
            month = parts["m"].lower()
            month_no = _MONTHS.index(month) + 1 if month in _MONTHS else int(month)
            try:
                out.add(date(int(parts["y"]), month_no, int(parts["d"])))
            except ValueError:
                continue
    return out


def money(raw: str | None) -> Decimal | None:
    try:
        value = Decimal((raw or "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


# --- ISINs ----------------------------------------------------------------------------------------

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def ordinary_equity(isin: str) -> bool:
    """An ordinary (or DVR) equity share's ISIN: `IN` + `E`/`9` + 4-char issuer + security type
    `01` + serial + check digit. ETF/fund units (`INF`), rights entitlements (type `20`) and
    debt are not. NSE's `instrument_class`-style labels are unreliable; the ISIN is not."""
    return len(isin) == 12 and isin.startswith("IN") and isin[2] in "E9" and isin[7:9] == "01"


def issuer(isin: str) -> str:
    """The issuer part of an ISIN: a reissue of the same company's shares keeps it."""
    return isin[3:7]


# --- ratios ---------------------------------------------------------------------------------------

_RATIO = re.compile(rf"^\s*{_NUMBER}\s*:\s*{_NUMBER}\s*$")
_RATIO_IN = re.compile(rf"{_NUMBER}\s*:\s*{_NUMBER}")


def parse_ratio(raw: str) -> tuple[Fraction, Fraction] | None:
    """`a:b` (decimals allowed: `1:48.18`) as two positive numbers, else None."""
    found = _RATIO.match(raw or "")
    if found is None:
        return None
    a, b = Fraction(found[1]), Fraction(found[2])
    return (a, b) if a > 0 and b > 0 else None


def bonus_ratio(text: str) -> tuple[int, int] | None:
    """The first `a:b` after the word "bonus"."""
    lowered = text.lower()
    at = lowered.find("bonus")
    if at < 0:
        return None
    found = re.search(r"(?<!\d)(\d++)\s*+:\s*+(\d++)", lowered[at:])
    return (int(found[1]), int(found[2])) if found else None


_FACE = re.compile(r"\d+(?:\.\d+)?")


def split_pair(text: str) -> tuple[Fraction, Fraction] | None:
    """Old and new face value of a split text ("From Rs 10/- Per Share To Rs 2/- Per Share");
    None unless exactly two positive numbers are named."""
    nums = [Fraction(n) for n in _FACE.findall(text)]
    if len(nums) == 2 and all(n > 0 for n in nums):
        return nums[0], nums[1]
    return None


# A bonus of preference shares or debentures ("Bonus NCRPS 4:1", "Bonus Debentures 1:1",
# "Bonus - 1 Debenture For 1 Equity Share Held") issues no equity: the share count stays and the
# price drops by about the instrument's value, as on a dividend. Read as an equity bonus it fakes
# a several-fold jump in the adjusted history.
_NON_EQUITY_BONUS = re.compile(
    r"bonus\W*(?:n?c?rps|ncds?\b|deb(?:entures?)?(?![a-z])|pref)"
    r"|bonus\b.*\b(?:debentures?|ncrps|preference)\b"
)


def non_equity_bonus(text: str) -> bool:
    return _NON_EQUITY_BONUS.search(text.lower()) is not None


@dataclass(frozen=True)
class Leg:
    action_type: str
    factor: Fraction


_SPLIT_START = re.compile(r"face\s*val\w*\s*split|split|sub-?\s*division")


def _one_leg(text: str) -> Leg | None:
    lowered = text.lower()
    if _SPLIT_START.search(lowered):
        pair = split_pair(lowered)
        if pair is not None and 0 < pair[1] / pair[0] < 1:
            return Leg(SPLIT, pair[1] / pair[0])
        return None
    if non_equity_bonus(lowered):
        return None
    ratio = bonus_ratio(lowered)
    if ratio and ratio[0] > 0 and ratio[1] > 0:
        return Leg(BONUS, Fraction(ratio[1], ratio[0] + ratio[1]))
    return None


def split_bonus_legs(text: str) -> tuple[Leg, ...] | None:
    """The legs of a split or bonus purpose: one, or a bonus and a split when the text names
    both ("Bonus 1:1/Face Value Split From Rs 10/- To Rs 2/-"). Only the split's own words are
    read for its numbers, so a dividend in the same purpose is ignored. None when a leg does
    not parse."""
    lowered = re.sub(r"\bsplt\b", "split", text.lower())
    lowered = re.sub(r"\bfrm\b", "from", lowered)
    cut = _SPLIT_START.search(lowered)
    if cut is None:
        leg = _one_leg(lowered)
        return None if leg is None else (leg,)
    split = _one_leg(lowered[cut.start() :])
    if split is None or split.action_type != SPLIT:
        return None
    head = lowered[: cut.start()]
    if "bonus" not in head or non_equity_bonus(head):
        return (split,)
    bonus = _one_leg(head)
    if bonus is None or bonus.action_type != BONUS:
        return None
    return (bonus, split)


def names_split_or_bonus(subject: str) -> bool:
    return re.search(r"split|splt|sub-?\s*division|bonus", subject.lower()) is not None


def names_rights(subject: str) -> bool:
    s = subject.lower()
    return re.search(r"\brights?\b", s) is not None and "entitlement" not in s


def names_demerger(subject: str) -> bool:
    return "demerg" in subject.lower()


# --- rights ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RightsTerms:
    """`new` shares for every `held`, issued at `price` (face value plus premium)."""

    new: Fraction
    held: Fraction
    price: Fraction


_RIGHTS_RATIO = re.compile(rf"\brights?\b(?:\s*issue)?[\s:@-]*{_NUMBER}\s*:\s*{_NUMBER}")
_GROUPED = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"  # 1,000 and 1,00,000 too
_PREMIUM = re.compile(rf"\b(?:premium|prem|prm)\b\s*(?:of\s*)?(?:(?:rs|re)\b\.?)?\s*{_GROUPED}")
_AT_PAR = re.compile(r"(?:\bat|@)\s*par\b")
_NON_SIMPLE = re.compile(r"warrant|ccps|ccd|convertible|debenture|partly|preference|\bncd")


def rights_terms(text: str, face_value: str) -> RightsTerms | str:
    """Terms of a rights purpose ("Rights 1:10 @ Premium Rs 95/-") with the row's face value,
    or why it cannot be priced: `non_simple` (partly paid, warrants, convertibles, debentures),
    `unparsed` or `no_issue_price`."""
    lowered = text.lower()
    if _NON_SIMPLE.search(lowered):
        return "non_simple"
    ratios = _RIGHTS_RATIO.findall(lowered)
    fv = money(face_value)
    if len(ratios) != 1 or fv is None or fv <= 0:
        return "unparsed"
    new, held = Fraction(ratios[0][0]), Fraction(ratios[0][1])
    if new <= 0 or held <= 0:
        return "unparsed"
    premiums = _PREMIUM.findall(lowered)
    if len(premiums) == 1:
        premium = Fraction(premiums[0].replace(",", ""))
    elif not premiums and _AT_PAR.search(lowered):
        premium = Fraction(0)
    else:
        return "no_issue_price"
    return RightsTerms(new, held, Fraction(fv) + premium)


# --- dividends ------------------------------------------------------------------------------------

KIND_INTERIM, KIND_FINAL, KIND_SPECIAL = "interim", "final", "special"


@dataclass(frozen=True, order=True)
class Payout:
    kind: str
    amount: Decimal


_DIVIDEND = re.compile(r"dividend")
_MONEY = re.compile(r"(?:\b(?:rs|re|inr)\b\.?|₹)\s*(?:[-:]\s*)?(\d+(?:\.\d+)?)")
_PERCENT = re.compile(r"(?<![\d.])\d++(?:\.\d++)?+\s*+%")
_SEP = re.compile(r"/(?!-)|\band\b|&|\+|;|,")
_PREFERENCE = re.compile(r"preference|\bpref\b|\bc?n?c?rps\b|\bcps\b")
_KIND = re.compile(r"\b(final|special|interim)\b")
_KIND_STOP = re.compile(r"[0-9%/]")
_BARE = re.compile(r"(?<![\d.])(\d++(?:\.\d++)?+)\s*+(?:/-\s*+)?per\s*+(?:equity\s*+)?sh")
_OTHER_PURPOSE = re.compile(
    r"split|sub-?\s*division|rights|bonus|consolidat|scheme|buy\s*-?back|face\s*val|interest"
)


def _kind(words: str) -> str:
    if "special" in words:
        return KIND_SPECIAL
    if "interim" in words:
        return KIND_INTERIM
    return KIND_FINAL


def _kinded(body: str) -> list[tuple[str, str]]:
    """(kind, amount) for each kind word followed by an amount with no digit, % or / between."""
    found: list[tuple[str, str]] = []
    end = 0
    for kind in _KIND.finditer(body):
        if kind.start() < end:
            continue
        amount = _MONEY.search(body, kind.end())
        stop = _KIND_STOP.search(body, kind.end())
        if amount is None or (stop is not None and stop.start() < amount.start()):
            continue
        found.append((kind[1], amount[1]))
        end = amount.end()
    return found


def _clause(prefix: str, body: str) -> list[Payout] | str:
    other = _OTHER_PURPOSE.search(body)
    body = body[: other.start()] if other else body
    if _PREFERENCE.search(prefix) or _PREFERENCE.search(body):
        return "preference_dividend"
    amounts = _MONEY.findall(body)
    if len(amounts) > 1:
        kinded = _kinded(body)
        if len(kinded) != len(amounts) or len({k for k, _ in kinded}) != len(kinded):
            return "several_amounts"
        found = [Payout(_kind(k), Decimal(a)) for k, a in kinded]
    elif amounts:
        found = [Payout(_kind(prefix), Decimal(amounts[0]))]
    else:
        bare = _BARE.findall(body)
        if len(bare) == 1:
            found = [Payout(_kind(prefix), Decimal(bare[0]))]
        elif "(" in prefix and ")" not in prefix.split("(")[-1]:
            return []  # "Rs 50/- Per Share (Special Dividend)": labels the amount before it
        else:
            return "percent_of_face_value" if _PERCENT.search(body) else "no_amount"
    if any(p.amount <= 0 for p in found):
        return "no_amount"
    return found


def _labelled(prefix: str) -> list[Payout] | str:
    """The one amount before a bracketed label, of the label's kind."""
    amount, label = prefix.rsplit("(", 1)
    found = _MONEY.findall(amount) or _BARE.findall(amount)
    if len(found) != 1:
        return "several_amounts" if found else "no_amount"
    value = Decimal(found[0])
    return [Payout(_kind(label), value)] if value > 0 else "no_amount"


_FIGURE = re.compile(r"\b(?:rs|re|inr)\b|₹|per\s*(?:equity\s*)?sh|\d\s*/-|\d\s*%")


def total(parts: tuple[Payout, ...] | list[Payout]) -> Decimal:
    return sum((p.amount for p in parts), Decimal(0))


def _dividend_clause(lowered: str, found: list[re.Match[str]], n: int) -> list[Payout] | str:
    """The payouts of the n-th "dividend" in a purpose: its prefix runs back to the last
    separator, its body up to the next "dividend"."""
    match = found[n]
    start = found[n - 1].end() if n else 0
    stop = found[n + 1].start() if n + 1 < len(found) else len(lowered)
    before = lowered[start : match.start()]
    seps = list(_SEP.finditer(before))
    prefix = before[seps[-1].end() :] if seps else before
    parts = _clause(prefix, lowered[match.end() : stop])
    # "Rs 50/- Per Share (Special Dividend)": the amount is in the prefix
    return _labelled(prefix) if parts == [] else parts


def _with_breakdown(out: list[Payout], inside: list[Payout], lowered: str) -> list[Payout] | str:
    """Settle the amounts named outside brackets against the bracketed breakdown of them."""
    rest = total(out) - total(inside)
    if len(out) == 1 and rest > 0 and "includ" in lowered:
        return [Payout(out[0].kind, rest), *inside]
    if out and rest != 0:
        return "breakdown_disagrees"
    return inside


def payouts(text: str) -> tuple[Payout, ...] | str:
    """The cash dividends a purpose names ("Interim Dividend - Rs 5 Per Share / Special
    Dividend - Rs 10 Per Share"), or why none can be trusted: `no_dividend`,
    `preference_dividend`, `percent_of_face_value`, `several_amounts`, `no_amount`,
    `breakdown_disagrees`. One unreadable clause is enough: a partial sum is never returned."""
    lowered = re.sub(r"\s+", " ", text.lower())
    found = list(_DIVIDEND.finditer(lowered))
    if not found:
        return "no_dividend"
    out: list[Payout] = []
    inside: list[Payout] = []  # named in brackets: a breakdown of the amount before them
    preference = False
    for n, match in enumerate(found):
        parts = _dividend_clause(lowered, found, n)
        if parts == "preference_dividend":
            preference = True
        elif isinstance(parts, str):
            return parts
        elif lowered[: match.start()].count("(") > lowered[: match.start()].count(")"):
            inside.extend(parts)
        else:
            out.extend(parts)
    settled = _with_breakdown(out, inside, lowered) if inside else out
    if isinstance(settled, str):
        return settled
    if not settled:
        return "preference_dividend" if preference else "no_amount"
    return tuple(settled)


def _read_rows(subjects: list[str]) -> tuple[dict[str, tuple[Payout, ...]], list[str]] | str:
    """Each feed row's payouts, and the reasons of the rows that gave none."""
    read: dict[str, tuple[Payout, ...]] = {}
    failed: list[str] = []
    for subject in subjects:
        found = payouts(subject)
        if found == "no_amount" and _FIGURE.search(subject.lower()):
            return "no_amount"  # a row with a figure we cannot read: never a partial sum
        if isinstance(found, str):
            failed.append(found)
        else:
            read[subject] = tuple(sorted(found))
    return read, failed


def _settle_rows(read: dict[str, tuple[Payout, ...]]) -> tuple[Payout, ...] | str:
    distinct = set(read.values())
    if len(distinct) == 1:
        return next(iter(distinct))
    kinds = [{p.kind for p in parts} for parts in distinct]
    if all(not (a & b) for i, a in enumerate(kinds) for b in kinds[i + 1 :]):
        return tuple(sorted(p for parts in distinct for p in parts))
    revised = {parts for subject, parts in read.items() if "revised" in subject.lower()}
    if len(revised) == 1:
        return next(iter(revised))
    return "rows_disagree"


def combine(subjects: list[str]) -> tuple[Payout, ...] | str:
    """The payouts of one (ISIN, ex-date)'s feed rows. NSE keeps revisions as separate rows:
    identical rows count once, rows naming different kinds add up, and rows giving one kind
    different amounts are settled by the "(Purpose Revised)" rows when those agree, else
    `rows_disagree`. A row that only names a dividend ("AGM / Dividend") is ignored; a row
    with a figure that does not read fails the group."""
    rows = _read_rows(subjects)
    if isinstance(rows, str):
        return rows
    read, failed = rows
    hard = [r for r in failed if r not in ("no_amount", "preference_dividend", "no_dividend")]
    if hard:
        return hard[0]
    if not read:
        return failed[0] if failed else "no_dividend"
    return _settle_rows(read)
