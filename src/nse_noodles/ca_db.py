"""Build the corporate-action database from the pulled NSE JSON shards.

Tables in `noodles.duckdb` (all rebuilt from the raw shards, so a rerun gives the same tables):
- `ca_feed`: every corporate-action row as NSE gave it (symbol, company, series, ISIN, purpose
  text, ex-date, record date, face value, market).
- `splits_bonus`: split and bonus rows with their parsed legs and an instrument-class HINT
  (equity / preference / debentures / warrants / unknown) from the text or a nearby
  announcement. The hint is unreliable: the price gate decides.
- `rights_issues`: rights rows with parsed terms, or why they cannot be priced.
- `demergers`: demerger rows. The feed never carries the ratio; research fills it.
- `bonus_instruments`: bonuses of debentures or preference shares (no equity issued; a cash
  distribution of the instrument's value, which research supplies).
- `dividends`: one row per (ISIN, ex-date): the rupees per share, or why it is not trusted.
- `announcements`, `board_meetings`: when those shards were pulled.

Each event row also carries `announced_on` / `notice_url` (and, for a demerger,
`notice_source`: announcement or board_meeting), from the NSE filings of the symbol
(announcements and board-meeting intimations) in the NOTICE_DAYS before the ex-date, empty when
the filings for that window were not pulled:
- splits, bonuses, rights, bonus instruments, dividends: the first filing whose text names the
  event (`first_notice`), when the market first heard of it;
- demergers: the first filing whose text says record date, names the demerger, scheme or
  hive-off (NSE's template: "Merger/HiveOff"), AND writes the record date that held, the feed's,
  as a date (`record_notice`). That is when a holder could first know the last session to sell
  before the ex-date (see `exits`). A filing naming another record date (one later changed) or
  none does not count; nothing found leaves it empty, never guessed.
NSE's `caBroadcastDate` is not used: it is mostly empty and, where set, often later than the
ex-date.

Every cell is untrusted text: it is parsed by `nse_noodles.parse`, never executed or trusted.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

import duckdb
import polars as pl

from . import parse
from .layout import Layout
from .nse_json import read_shards

TEXT_MAX = 2000
_NEG = re.compile(r"debenture|preference|ncrps|nccps|warrant", re.I)
_EQUITY_EVIDENCE = re.compile(
    r"bonus\s+(?:issue\s+of\s+)?equity|equity\s+shares?\s+pursuant\s+to\s+bonus"
    r"|allotment\s+of\s+[\d,]+\s+(?:bonus\s+)?equity",
    re.I,
)
EVIDENCE_BEFORE, EVIDENCE_AFTER = 150, 90  # announcement window (days) around a bonus ex-date
NOTICE_DAYS = 180  # an event's first filing falls this many days before its ex-date at most
NOTICE = {
    "splits_bonus": re.compile(r"split|sub-?division|bonus", re.I),
    "rights_issues": re.compile(r"\brights?\b", re.I),
    # a demerger's record-date notice (`record_notice`): "Record date ... pursuant to the Scheme",
    # "... for the purpose of Merger/HiveOff" (NSE's template, at times cut to "Merger/HiveOf"),
    # "... as the Demerger Record Date"
    "demergers": re.compile(
        r"record\s+date.*(?:merger|scheme|arrangement|hive[\s-]*of)"
        r"|(?:merger|scheme|arrangement|hive[\s-]*of).*record\s+date",
        re.I | re.S,
    ),
    "bonus_instruments": re.compile(r"bonus", re.I),
    "dividends": parse.DIVIDEND_WORD,
}


_EVENT = {
    "symbol": pl.Utf8,
    "isin": pl.Utf8,
    "series": pl.Utf8,
    "ex_date": pl.Date,
    "record_date": pl.Date,
    "market": pl.Utf8,
    "subject": pl.Utf8,
}
_NOTICE = {"announced_on": pl.Date, "notice_url": pl.Utf8}
# Every table is created with its columns even when a range has no rows for it, so the later
# steps can always query it.
SCHEMAS: dict[str, dict[str, pl.DataType | type[pl.DataType]]] = {
    "ca_feed": {
        "symbol": pl.Utf8,
        "company": pl.Utf8,
        "series": pl.Utf8,
        "isin": pl.Utf8,
        "subject": pl.Utf8,
        "ex_date": pl.Date,
        "record_date": pl.Date,
        "face_value": pl.Utf8,
        "market": pl.Utf8,
    },
    "splits_bonus": {
        **_EVENT,
        "legs": pl.Utf8,
        "parse_status": pl.Utf8,
        "instrument_class": pl.Utf8,
        "classification_source": pl.Utf8,
        "evidence": pl.Utf8,
        "evidence_url": pl.Utf8,
        **_NOTICE,
    },
    "rights_issues": {
        **_EVENT,
        "face_value": pl.Utf8,
        "ratio_new": pl.Float64,
        "ratio_held": pl.Float64,
        "issue_price": pl.Float64,
        "parse_status": pl.Utf8,
        **_NOTICE,
    },
    "demergers": {**_EVENT, "company": pl.Utf8, "notice_source": pl.Utf8, **_NOTICE},
    "bonus_instruments": {**_EVENT, "face_value": pl.Utf8, **_NOTICE},
    "dividends": {
        "isin": pl.Utf8,
        "symbol": pl.Utf8,
        "series": pl.Utf8,
        "ex_date": pl.Date,
        "amount": pl.Float64,
        "kinds": pl.Utf8,
        "status": pl.Utf8,
        "subjects": pl.Utf8,
        "market": pl.Utf8,
        **_NOTICE,
    },
    "announcements": {
        "symbol": pl.Utf8,
        "company": pl.Utf8,
        "isin": pl.Utf8,
        "announced_at": pl.Utf8,
        "disseminated_at": pl.Utf8,
        "category": pl.Utf8,
        "text": pl.Utf8,
        "file": pl.Utf8,
    },
    "board_meetings": {
        "symbol": pl.Utf8,
        "company": pl.Utf8,
        "isin": pl.Utf8,
        "meeting_date": pl.Date,
        "purpose": pl.Utf8,
        "description": pl.Utf8,
        "intimated_at": pl.Utf8,
    },
}


def _cell(value: object) -> str:
    return ("" if value is None else str(value)).strip()[:TEXT_MAX]


def _an_dt(raw: str) -> date | None:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def feed_rows(layout: Layout) -> list[dict]:
    out = []
    for market in ("equities", "sme"):
        for r in read_shards(layout, "corporate_actions", market):
            out.append(
                {
                    "symbol": _cell(r.get("symbol")).upper(),
                    "company": _cell(r.get("comp")),
                    "series": _cell(r.get("series")).upper(),
                    "isin": _cell(r.get("isin")).upper(),
                    "subject": _cell(r.get("subject")),
                    "ex_date": parse.nse_date(_cell(r.get("exDate"))),
                    "record_date": parse.nse_date(_cell(r.get("recDate"))),
                    "face_value": _cell(r.get("faceVal")),
                    "market": market,
                }
            )
    return out


def subject_instrument(subject: str) -> str | None:
    s = subject.lower()
    if re.search(r"ncrps|nccps|preference|\bccps\b", s):
        return "preference"
    if re.search(r"debenture|\bncds?\b|\bdeb\b", s):
        return "debentures"
    if "warrant" in s:
        return "warrants"
    return None


def _negative_kind(word: str) -> str:
    """The instrument a `_NEG` match names."""
    if "debenture" in word:
        return "debentures"
    if "warrant" in word:
        return "warrants"
    return "preference"


def _bonus_evidence(found: list[dict], ex: date | None) -> list[dict]:
    """The symbol's dated announcements naming a bonus within the evidence window of `ex`."""
    if ex is None:
        return []
    return [
        a
        for a in found
        if a["day"] is not None
        and -EVIDENCE_BEFORE <= (a["day"] - ex).days <= EVIDENCE_AFTER
        and "bonus" in a["text"].lower()
    ]


def classify_bonus(row: dict, announcements: dict[str, list[dict]]) -> tuple[str, str, str, str]:
    """(instrument_class, source, evidence, evidence_url) for a split or bonus row. A split is
    equity by definition. A plain "Bonus x:y" is classified only from announcement evidence
    near the ex-date; otherwise `unknown` (never assume unknown means equity)."""
    found = subject_instrument(row["subject"])
    if found:
        return found, "subject", "", ""
    if "split" in row["subject"].lower() or "bonus" not in row["subject"].lower():
        return "equity", "subject", "", ""
    nearby = _bonus_evidence(list(announcements.get(row["symbol"], ())), row["ex_date"])
    # any non-equity instrument named near the ex-date wins over equity evidence
    for a in nearby:
        neg = _NEG.search(a["text"])
        if neg:
            return _negative_kind(neg[0].lower()), "announcement", a["text"][:220], a["file"]
    equity = next((a for a in nearby if _EQUITY_EVIDENCE.search(a["text"])), None)
    if equity:
        return "equity", "announcement", equity["text"][:220], equity["file"]
    return "unknown", "", "", ""


def _announcements(layout: Layout) -> list[dict]:
    out = []
    for r in read_shards(layout, "announcements", "equities"):
        out.append(
            {
                "symbol": _cell(r.get("symbol")).upper(),
                "company": _cell(r.get("sm_name")),
                "isin": _cell(r.get("sm_isin")).upper(),
                "announced_at": _cell(r.get("an_dt")),
                "disseminated_at": _cell(r.get("exchdisstime") or r.get("sort_date")),
                "category": _cell(r.get("desc")),
                "text": _cell(r.get("attchmntText")).replace("\n", " "),
                "file": _cell(r.get("attchmntFile")),
            }
        )
    return out


def _board_meetings(layout: Layout) -> list[dict]:
    return [
        {
            "symbol": _cell(b.get("bm_symbol")).upper(),
            "company": _cell(b.get("sm_name")),
            "isin": _cell(b.get("sm_isin")).upper(),
            "meeting_date": parse.nse_date(_cell(b.get("bm_date"))),
            "purpose": _cell(b.get("bm_purpose")),
            "description": _cell(b.get("bm_desc")).replace("\n", " "),
            "intimated_at": _cell(b.get("bm_timestamp")),
        }
        for b in read_shards(layout, "board_meetings", "equities")
    ]


Notice = tuple[date, str, str, str]  # (day, text, url, source: announcement | board_meeting)


def first_notice(
    notices: list[Notice], ex: date | None, pattern: re.Pattern[str]
) -> tuple[date | None, str]:
    """(day, url) of the earliest (day, text, url, ...) filing within NOTICE_DAYS before `ex`, up
    to and including the ex-date, whose text matches `pattern`; (None, "") when there is none."""
    if ex is None:
        return None, ""
    hits = [
        (day, url)
        for day, text, url, *_ in notices
        if 0 <= (ex - day).days <= NOTICE_DAYS and pattern.search(text)
    ]
    return min(hits) if hits else (None, "")


def record_notice(
    notices: list[Notice], ex: date | None, record: date | None, pattern: re.Pattern[str]
) -> tuple[date | None, str, str]:
    """(day, url, source) of the earliest filing within NOTICE_DAYS before `ex`, up to and
    including the ex-date, whose text matches `pattern` and names the date `record`
    (`parse.dates_named`); (None, "", "") when there is none or either date is unknown. On the
    same day an announcement (with its attachment URL) wins over a board-meeting intimation."""
    if ex is None or record is None:
        return None, "", ""
    hits = [
        (day, url, source)
        for day, text, url, source in notices
        if 0 <= (ex - day).days <= NOTICE_DAYS
        and pattern.search(text)
        and record in parse.dates_named(text)
    ]
    return min(hits, key=lambda h: (h[0], h[2] != "announcement", h[1])) if hits else (None, "", "")


def _notices(anns: list[dict], meetings: list[dict]) -> dict[str, list[Notice]]:
    out: dict[str, list[Notice]] = defaultdict(list)
    for a in anns:
        day = _an_dt(a["announced_at"])
        if day is not None:
            text = f"{a['category']} {a['text']}"
            out[a["symbol"]].append((day, text, a["file"], "announcement"))
    for m in meetings:
        day = _an_dt(m["intimated_at"])
        if day is not None:
            text = f"{m['purpose']} {m['description']}"
            out[m["symbol"]].append((day, text, "", "board_meeting"))
    return out


def _legs_text(legs: tuple[parse.Leg, ...] | None) -> str:
    return "" if not legs else ";".join(f"{leg.action_type}={leg.factor}" for leg in legs)


def _split_bonus_row(r: dict, base: dict, evidence: dict[str, list[dict]]) -> dict:
    legs = parse.split_bonus_legs(r["subject"])
    cls, src, ev, url = classify_bonus(r, evidence)
    return {
        **base,
        "subject": r["subject"],
        "legs": _legs_text(legs),
        "parse_status": "ok" if legs else "unparsed",
        "instrument_class": cls,
        "classification_source": src,
        "evidence": ev,
        "evidence_url": url,
    }


def _rights_row(r: dict, base: dict) -> dict:
    terms = parse.rights_terms(r["subject"], r["face_value"])
    ok = not isinstance(terms, str)
    return {
        **base,
        "subject": r["subject"],
        "face_value": r["face_value"],
        "ratio_new": float(terms.new) if ok else None,  # type: ignore[union-attr]
        "ratio_held": float(terms.held) if ok else None,  # type: ignore[union-attr]
        "issue_price": float(terms.price) if ok else None,  # type: ignore[union-attr]
        "parse_status": "ok" if ok else terms,
    }


def _events(
    feed: list[dict], evidence: dict[str, list[dict]]
) -> tuple[dict[str, list[dict]], dict[tuple[str, str, date], list[dict]]]:
    """Each feed row into the event tables its subject names (one row can name several), and
    the dividend rows grouped by (ISIN, symbol, ex-date)."""
    tables: dict[str, list[dict]] = {
        "splits_bonus": [],
        "rights_issues": [],
        "demergers": [],
        "bonus_instruments": [],
    }
    dividend_groups: dict[tuple[str, str, date], list[dict]] = defaultdict(list)
    for r in feed:
        subject = r["subject"]
        base = {k: r[k] for k in ("symbol", "isin", "series", "ex_date", "record_date", "market")}
        if parse.names_split_or_bonus(subject):
            if parse.non_equity_bonus(subject):
                tables["bonus_instruments"].append(
                    {**base, "subject": subject, "face_value": r["face_value"]}
                )
            tables["splits_bonus"].append(_split_bonus_row(r, base, evidence))
        if parse.names_rights(subject):
            tables["rights_issues"].append(_rights_row(r, base))
        if parse.names_demerger(subject):
            tables["demergers"].append({**base, "subject": subject, "company": r["company"]})
        if parse.DIVIDEND_WORD.search(subject) and r["ex_date"] is not None:
            dividend_groups[(r["isin"], r["symbol"], r["ex_date"])].append(r)
    return tables, dividend_groups


def _dividend_status(isin: str, series: str, found: tuple[parse.Payout, ...] | str) -> str:
    if not parse.ordinary_equity(isin) and isin:
        return "not_equity_share"
    if series not in ("EQ", "BE", "BZ", ""):
        return "not_equity_series"
    return found if isinstance(found, str) else "ok"


def _dividend_row(isin: str, symbol: str, ex: date, rows: list[dict]) -> dict:
    found = parse.combine([x["subject"] for x in rows])
    parsed = not isinstance(found, str)
    return {
        "isin": isin,
        "symbol": symbol,
        "series": rows[0]["series"],
        "ex_date": ex,
        "amount": float(parse.total(found)) if parsed else None,  # type: ignore[arg-type]
        "kinds": "+".join(f"{p.kind} {p.amount}" for p in found) if parsed else "",  # type: ignore[union-attr]
        "status": _dividend_status(isin, rows[0]["series"], found),
        "subjects": " | ".join(sorted({x["subject"] for x in rows}))[:TEXT_MAX],
        "market": rows[0]["market"],
    }


def _split_bonus_notice(subject: str) -> re.Pattern[str]:
    """A split is announced by a filing that names a split, a bonus by one that names a bonus: a
    split filing weeks earlier must not date a bonus (it would be known before it was)."""
    lowered = subject.lower()
    words = (
        [r"split|sub-?\s*division"] if re.search(r"split|splt|sub-?\s*division", lowered) else []
    )
    words += ["bonus"] if "bonus" in lowered else []
    return re.compile("|".join(words), re.I) if words else NOTICE["splits_bonus"]


def _attach_notices(tables: dict[str, list[dict]], notices: dict[str, list[Notice]]) -> None:
    """Set `announced_on` / `notice_url` (and a demerger's `notice_source`) on every event row."""
    for name, table_pattern in NOTICE.items():
        for row in tables[name]:
            filed = notices.get(row["symbol"], [])
            pattern = table_pattern
            if name == "splits_bonus":
                pattern = _split_bonus_notice(row["subject"])
            if name == "demergers":
                day, url, source = record_notice(filed, row["ex_date"], row["record_date"], pattern)
                row["notice_source"] = source
            else:
                day, url = first_notice(filed, row["ex_date"], pattern)
            row["announced_on"], row["notice_url"] = day, url


def _write_tables(layout: Layout, tables: dict[str, list[dict]]) -> dict[str, int]:
    counts = {}
    layout.root.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(layout.db)) as con:
        for name, rows in tables.items():
            frame = pl.DataFrame(rows, schema=SCHEMAS[name])
            con.execute(f'DROP TABLE IF EXISTS "{name}"')
            con.register("frame", frame)
            con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM frame')
            con.unregister("frame")
            counts[name] = len(rows)
    return counts


def build(layout: Layout) -> dict[str, int]:
    feed = feed_rows(layout)
    anns = _announcements(layout)
    meetings = _board_meetings(layout)
    evidence: dict[str, list[dict]] = defaultdict(list)
    for a in anns:
        evidence[a["symbol"]].append(
            {"day": _an_dt(a["announced_at"]), "text": a["text"], "file": a["file"]}
        )
    events, dividend_groups = _events(feed, evidence)
    tables = {
        "ca_feed": feed,
        **events,
        "dividends": [_dividend_row(*key, rows) for key, rows in dividend_groups.items()],
        "announcements": anns,
        "board_meetings": meetings,
    }
    _attach_notices(tables, _notices(anns, meetings))
    return _write_tables(layout, tables)
