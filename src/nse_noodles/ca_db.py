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


def classify_bonus(row: dict, announcements: dict[str, list[dict]]) -> tuple[str, str, str, str]:
    """(instrument_class, source, evidence, evidence_url) for a split or bonus row. A split is
    equity by definition. A plain "Bonus x:y" is classified only from announcement evidence
    near the ex-date; otherwise `unknown` (never assume unknown means equity)."""
    found = subject_instrument(row["subject"])
    if found:
        return found, "subject", "", ""
    if "split" in row["subject"].lower() or "bonus" not in row["subject"].lower():
        return "equity", "subject", "", ""
    ex = row["ex_date"]
    equity_hit = None
    for a in announcements.get(row["symbol"], ()):
        if ex is None or a["day"] is None:
            continue
        if not -EVIDENCE_BEFORE <= (a["day"] - ex).days <= EVIDENCE_AFTER:
            continue
        text = a["text"]
        if "bonus" not in text.lower():
            continue
        neg = _NEG.search(text)
        if neg:
            word = neg[0].lower()
            kind = (
                "debentures"
                if "debenture" in word
                else "warrants"
                if "warrant" in word
                else "preference"
            )
            return kind, "announcement", text[:220], a["file"]
        if equity_hit is None and _EQUITY_EVIDENCE.search(text):
            equity_hit = (text[:220], a["file"])
    if equity_hit:
        return "equity", "announcement", equity_hit[0], equity_hit[1]
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


def _legs_text(legs: tuple[parse.Leg, ...] | None) -> str:
    return "" if not legs else ";".join(f"{leg.action_type}={leg.factor}" for leg in legs)


def build(layout: Layout) -> dict[str, int]:
    feed = feed_rows(layout)
    anns = _announcements(layout)
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for a in anns:
        by_symbol[a["symbol"]].append(
            {"day": _an_dt(a["announced_at"]), "text": a["text"], "file": a["file"]}
        )

    splits, rights, demergers, instruments = [], [], [], []
    dividend_groups: dict[tuple[str, str, date], list[dict]] = defaultdict(list)
    for r in feed:
        subject = r["subject"]
        base = {k: r[k] for k in ("symbol", "isin", "series", "ex_date", "record_date", "market")}
        if parse.names_split_or_bonus(subject):
            if parse.non_equity_bonus(subject):
                instruments.append({**base, "subject": subject, "face_value": r["face_value"]})
            legs = parse.split_bonus_legs(subject)
            cls, src, ev, url = classify_bonus(r, by_symbol)
            splits.append(
                {
                    **base,
                    "subject": subject,
                    "legs": _legs_text(legs),
                    "parse_status": "ok" if legs else "unparsed",
                    "instrument_class": cls,
                    "classification_source": src,
                    "evidence": ev,
                    "evidence_url": url,
                }
            )
        if parse.names_rights(subject):
            terms = parse.rights_terms(subject, r["face_value"])
            ok = not isinstance(terms, str)
            rights.append(
                {
                    **base,
                    "subject": subject,
                    "face_value": r["face_value"],
                    "ratio_new": float(terms.new) if ok else None,  # type: ignore[union-attr]
                    "ratio_held": float(terms.held) if ok else None,  # type: ignore[union-attr]
                    "issue_price": float(terms.price) if ok else None,  # type: ignore[union-attr]
                    "parse_status": "ok" if ok else terms,
                }
            )
        if parse.names_demerger(subject):
            demergers.append({**base, "subject": subject, "company": r["company"]})
        if "dividend" in subject.lower() and r["ex_date"] is not None:
            dividend_groups[(r["isin"], r["symbol"], r["ex_date"])].append(r)

    dividends = []
    for (isin, symbol, ex), rows in dividend_groups.items():
        found = parse.combine([x["subject"] for x in rows])
        if not parse.ordinary_equity(isin) and isin:
            status = "not_equity_share"
        elif rows[0]["series"] not in ("EQ", "BE", "BZ", ""):
            status = "not_equity_series"
        else:
            status = found if isinstance(found, str) else "ok"
        dividends.append(
            {
                "isin": isin,
                "symbol": symbol,
                "series": rows[0]["series"],
                "ex_date": ex,
                "amount": float(parse.total(found)) if not isinstance(found, str) else None,
                "kinds": "+".join(f"{p.kind} {p.amount}" for p in found)
                if not isinstance(found, str)
                else "",
                "status": status,
                "subjects": " | ".join(sorted({x["subject"] for x in rows}))[:TEXT_MAX],
                "market": rows[0]["market"],
            }
        )

    tables = {
        "ca_feed": feed,
        "splits_bonus": splits,
        "rights_issues": rights,
        "demergers": demergers,
        "bonus_instruments": instruments,
        "dividends": dividends,
        "announcements": anns,
        "board_meetings": _board_meetings(layout),
    }
    counts = {}
    layout.root.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(layout.db)) as con:
        for name, rows in tables.items():
            frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
            con.execute(f'DROP TABLE IF EXISTS "{name}"')
            if frame.width:
                con.register("frame", frame)
                con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM frame')
                con.unregister("frame")
            counts[name] = len(rows)
    return counts
