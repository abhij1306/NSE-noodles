import json
from datetime import date
from pathlib import Path

import duckdb

from nse_noodles import ca_db
from nse_noodles.layout import Layout

ABC, DEF, GHI, JKL, MNO = (f"INE{n}A01011" for n in ("001", "002", "003", "004", "005"))


def _feed(symbol, isin, subject, ex, rec="", series="EQ", face="10"):
    return {
        "symbol": symbol,
        "comp": f"{symbol} Ltd",
        "series": series,
        "isin": isin,
        "subject": subject,
        "exDate": ex,
        "recDate": rec,
        "faceVal": face,
    }


def _ann(symbol, at, text, file, desc="General"):
    return {
        "symbol": symbol,
        "sm_name": f"{symbol} Ltd",
        "sm_isin": "",
        "an_dt": at,
        "desc": desc,
        "attchmntText": text,
        "attchmntFile": file,
    }


def _write(layout: Layout, dataset: str, index: str, rows: list[dict]) -> None:
    folder = layout.nse(dataset, index)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2026-06.json").write_text(json.dumps(rows), encoding="utf-8")


def _fixture(root: Path) -> Layout:
    layout = Layout(root)
    _write(
        layout,
        "corporate_actions",
        "equities",
        [
            _feed(
                "ABC",
                ABC,
                "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
                "10-Jun-2026",
            ),
            _feed("ABC", ABC, "Bonus 1:1", "20-Jun-2026"),
            _feed("DEF", DEF, "Bonus 1:1", "20-Jun-2026"),
            _feed("GHI", GHI, "Bonus NCRPS 4:1", "22-Jun-2026"),
            _feed("JKL", JKL, "Rights 1:5 @ Premium Rs 90/-", "24-Jun-2026"),
            _feed("JKL", JKL, "Rights Issue of Warrants", "25-Jun-2026"),
            _feed("MNO", MNO, "Demerger", "01-Jul-2026", rec="01-Jul-2026"),
            _feed("ABC", ABC, "Interim Dividend - Rs 5 Per Share", "15-Jun-2026"),
            _feed("ABC", ABC, "Special Dividend - Rs 2 Per Share", "15-Jun-2026"),
            _feed("PQR", "INE006A01011", "Dividend - Rs 3 Per Share", "16-Jun-2026", series="N1"),
            _feed("STU", "INF007A01011", "Dividend - Rs 1 Per Share", "17-Jun-2026"),
            _feed("VWX", "INE008A01011", "Dividend - Rs 4 Per Share", ""),
            _feed("YZA", "INE009A01011", "Dividend Rs Nil", "18-Jun-2026"),
        ],
    )
    _write(
        layout,
        "corporate_actions",
        "sme",
        [_feed("SME", "INE010A01011", "Final Dividend - Rs 1.50 Per Share", "19-Jun-2026")],
    )
    _write(
        layout,
        "announcements",
        "equities",
        [
            _ann("ABC", "01-May-2026 10:00:00", "Board approves stock split", "abc-split.pdf"),
            _ann(
                "ABC",
                "02-May-2026 10:00:00",
                "Bonus issue of equity shares in the ratio 1:1",
                "abc-bonus.pdf",
            ),
            _ann("DEF", "03-May-2026", "Bonus issue of debentures to shareholders", "def.pdf"),
            _ann("DEF", "01-Jan-2026 10:00:00", "Bonus of warrants long ago", "def-old.pdf"),
            _ann(
                "MNO",
                "05-Jun-2026 09:00:00",
                "Record date for the Scheme of Arrangement is 01-Jul-2026",
                "mno.pdf",
            ),
        ],
    )
    _write(
        layout,
        "board_meetings",
        "equities",
        [
            {
                "bm_symbol": "JKL",
                "sm_name": "JKL Ltd",
                "sm_isin": JKL,
                "bm_date": "02-Jun-2026",
                "bm_purpose": "Rights Issue",
                "bm_desc": "To consider a rights issue",
                "bm_timestamp": "28-May-2026 18:00:00",
            }
        ],
    )
    return layout


def _rows(layout: Layout, sql: str) -> list[tuple]:
    with duckdb.connect(str(layout.db), read_only=True) as con:
        return con.execute(sql).fetchall()


def test_build_tables(tmp_path):
    layout = _fixture(tmp_path)
    assert ca_db.build(layout) == {
        "ca_feed": 14,
        "splits_bonus": 4,
        "rights_issues": 2,
        "demergers": 1,
        "bonus_instruments": 1,
        "dividends": 5,
        "announcements": 5,
        "board_meetings": 1,
    }
    assert _rows(
        layout,
        "SELECT symbol, ex_date, legs, parse_status, instrument_class, classification_source,"
        " evidence_url, announced_on, notice_url FROM splits_bonus ORDER BY ALL",
    ) == [
        ("ABC", date(2026, 6, 10), "split=1/5", "ok", "equity", "subject", "",
         date(2026, 5, 1), "abc-split.pdf"),
        ("ABC", date(2026, 6, 20), "bonus=1/2", "ok", "equity", "announcement", "abc-bonus.pdf",
         date(2026, 5, 1), "abc-split.pdf"),
        ("DEF", date(2026, 6, 20), "bonus=1/2", "ok", "debentures", "announcement", "def.pdf",
         date(2026, 1, 1), "def-old.pdf"),
        ("GHI", date(2026, 6, 22), "", "unparsed", "preference", "subject", "", None, ""),
    ]  # fmt: skip
    assert _rows(
        layout,
        "SELECT ex_date, ratio_new, ratio_held, issue_price, parse_status, announced_on"
        " FROM rights_issues ORDER BY ALL",
    ) == [
        (date(2026, 6, 24), 1.0, 5.0, 100.0, "ok", date(2026, 5, 28)),
        (date(2026, 6, 25), None, None, None, "non_simple", date(2026, 5, 28)),
    ]
    assert _rows(
        layout,
        "SELECT symbol, company, record_date, notice_source, announced_on, notice_url"
        " FROM demergers",
    ) == [("MNO", "MNO Ltd", date(2026, 7, 1), "announcement", date(2026, 6, 5), "mno.pdf")]
    assert _rows(layout, "SELECT symbol, subject, face_value FROM bonus_instruments") == [
        ("GHI", "Bonus NCRPS 4:1", "10")
    ]
    assert _rows(
        layout,
        "SELECT symbol, series, ex_date, amount, kinds, status, subjects, market"
        " FROM dividends ORDER BY symbol",
    ) == [
        ("ABC", "EQ", date(2026, 6, 15), 7.0, "interim 5+special 2", "ok",
         "Interim Dividend - Rs 5 Per Share | Special Dividend - Rs 2 Per Share", "equities"),
        ("PQR", "N1", date(2026, 6, 16), 3.0, "final 3", "not_equity_series",
         "Dividend - Rs 3 Per Share", "equities"),
        ("SME", "EQ", date(2026, 6, 19), 1.5, "final 1.50", "ok",
         "Final Dividend - Rs 1.50 Per Share", "sme"),
        ("STU", "EQ", date(2026, 6, 17), 1.0, "final 1", "not_equity_share",
         "Dividend - Rs 1 Per Share", "equities"),
        ("YZA", "EQ", date(2026, 6, 18), None, "", "no_amount", "Dividend Rs Nil", "equities"),
    ]  # fmt: skip


def test_build_without_announcements_leaves_tables_empty(tmp_path):
    layout = Layout(tmp_path)
    _write(layout, "corporate_actions", "equities", [_feed("ABC", ABC, "Bonus 1:1", "20-Jun-2026")])
    counts = ca_db.build(layout)
    assert counts["announcements"] == 0
    assert counts["board_meetings"] == 0
    assert _rows(
        layout,
        "SELECT instrument_class, classification_source, announced_on FROM splits_bonus",
    ) == [("unknown", "", None)]
    assert _rows(
        layout, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'announcements'"
    ) == [(0,)]


def _bonus(subject="Bonus 1:1", ex=date(2026, 6, 20)):
    return {"symbol": "ABC", "subject": subject, "ex_date": ex}


def _evidence(*items):
    return {"ABC": [{"day": d, "text": t, "file": f} for d, t, f in items]}


def test_classify_bonus_from_subject():
    assert ca_db.classify_bonus(_bonus("Bonus NCRPS 1:1"), {}) == ("preference", "subject", "", "")
    assert ca_db.classify_bonus(_bonus("Bonus Debentures 1:1"), {}) == (
        "debentures",
        "subject",
        "",
        "",
    )
    assert ca_db.classify_bonus(_bonus("Bonus Warrants 1:1"), {}) == ("warrants", "subject", "", "")
    assert ca_db.classify_bonus(_bonus("Split from Rs 10 to Rs 2"), {}) == (
        "equity",
        "subject",
        "",
        "",
    )
    assert ca_db.classify_bonus(_bonus("Rights 1:5"), {}) == ("equity", "subject", "", "")


def test_classify_bonus_from_announcements():
    may = date(2026, 5, 1)
    assert ca_db.classify_bonus(_bonus(), _evidence((may, "Bonus of warrants", "w.pdf"))) == (
        "warrants",
        "announcement",
        "Bonus of warrants",
        "w.pdf",
    )
    assert ca_db.classify_bonus(
        _bonus(), _evidence((may, "Bonus 1:1 of Preference shares", "p.pdf"))
    ) == ("preference", "announcement", "Bonus 1:1 of Preference shares", "p.pdf")
    # a negative hit anywhere in the window beats earlier equity evidence
    equity = (may, "Allotment of 1,000 bonus equity shares", "e.pdf")
    assert ca_db.classify_bonus(
        _bonus(), _evidence(equity, (may, "Bonus debenture issue", "d.pdf"))
    ) == ("debentures", "announcement", "Bonus debenture issue", "d.pdf")
    assert ca_db.classify_bonus(
        _bonus(), _evidence(equity, (may, "Bonus issue of equity", "e2.pdf"))
    ) == ("equity", "announcement", "Allotment of 1,000 bonus equity shares", "e.pdf")


def test_classify_bonus_ignores_announcements_out_of_reach():
    far = (date(2025, 6, 1), "Bonus of warrants", "far.pdf")  # > EVIDENCE_BEFORE days before
    late = (date(2026, 9, 30), "Bonus of warrants", "late.pdf")  # > EVIDENCE_AFTER days after
    undated = (None, "Bonus of warrants", "undated.pdf")
    no_bonus = (date(2026, 6, 1), "Warrants allotted", "nb.pdf")
    plain = (date(2026, 6, 1), "Bonus record date fixed", "plain.pdf")
    found = _evidence(far, late, undated, no_bonus, plain)
    assert ca_db.classify_bonus(_bonus(), found) == ("unknown", "", "", "")
    assert ca_db.classify_bonus(_bonus(ex=None), found) == ("unknown", "", "", "")
