import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import duckdb
import polars as pl

from nse_noodles import actions
from nse_noodles.actions import Chains, Outcome, Proposal, apply_corrections
from nse_noodles.bars import Bar
from nse_noodles.bhavcopy import BAR_SCHEMA
from nse_noodles.curation import RESEARCH_COLUMNS, Correction, ResearchRow
from nse_noodles.indices import INDEX_SCHEMA
from nse_noodles.layout import Layout
from nse_noodles.parse import Leg, RightsTerms

EX = date(2026, 9, 14)
BEFORE = Bar("INE000A01011", "AAA", date(2026, 9, 11), 100.0, 100.0, 100.0)
RESEARCH = ResearchRow(
    "r.csv", 2, "demerger", "BBB", "INE000B01011", date(2026, 9, 21), "demerger", "CCC",
    "INE000C01011", (Fraction(1), Fraction(1)), None, False, None, None, "", None, None, "no", 2,
    "https://example.com/scheme", date(2026, 9, 1), "", "",
)  # fmt: skip


def _on(open_: float) -> Bar:
    return Bar("INE000A01011", "AAA", EX, open_, open_, 100.0)


def _split(**kw) -> Proposal:
    p = Proposal("splits_bonus", "AAA", "INE000A01011", EX, "Face Value Split", **kw)
    p.legs = p.legs or (Leg("split", Fraction(1, 2)),)
    return p


def _rights(price: int) -> Proposal:
    p = Proposal("rights_issues", "AAA", "INE000A01011", EX, "Rights 1:4")
    p.rights = RightsTerms(Fraction(1), Fraction(4), Fraction(price))
    return p


def _decided(rows: list[dict]) -> list[tuple]:
    return [
        (r["action_type"], round(r["value"], 4), r["method"], r["available_basis"]) for r in rows
    ]


def test_a_split_the_open_confirms_is_accepted_from_the_ex_date_open():
    out = Outcome()
    got = actions.decide_price_group("C", BEFORE, _on(50.5), [_split()], out)
    assert _decided(got) == [("split", 0.5, "gate_confirmed", "ex_open")]
    assert got[0]["available_at"] == datetime(2026, 9, 14, 10)
    assert got[0]["basis"] == "stated_ratio"
    assert out.review == [] and out.skipped == []


def test_a_split_the_open_contradicts_goes_to_review():
    out = Outcome()
    assert actions.decide_price_group("C", BEFORE, _on(99.0), [_split()], out) == []
    assert [r["reason"] for r in out.review] == ["off_factor"]


def test_two_rows_with_one_kind_on_one_session_conflict():
    out = Outcome()
    other = _split(legs=(Leg("split", Fraction(1, 5)),))
    assert actions.decide_price_group("C", BEFORE, _on(50.0), [_split(), other], out) == []
    assert [r["reason"] for r in out.review] == ["conflicting_rows", "conflicting_rows"]


def test_rights_lost_in_the_session_move_are_kept_unconfirmed():
    out = Outcome()
    # TERP factor (4*100 + 80) / (5*100) = 0.96; an open of 98 cannot tell it from noise
    got = actions.decide_price_group("C", BEFORE, _on(98.0), [_rights(80)], out)
    assert _decided(got) == [("rights", 0.96, "terp_price_unconfirmed", "ex_open")]
    assert got[0]["basis"] == "terp"


def test_rights_priced_at_or_above_the_market_are_skipped():
    out = Outcome()
    assert actions.decide_price_group("C", BEFORE, _on(100.0), [_rights(120)], out) == []
    assert [r["reason"] for r in out.skipped] == ["rights_at_or_above_price"]


def _bars(isin: str, closes: dict[date, float], first: date, last: date) -> list[Bar]:
    out, day, close = [], first, None
    while day <= last:
        if day.weekday() < 5:
            close = closes.get(day, close)
            out.append(Bar(isin, isin[-6:-3], day, close, close, close))
        day += timedelta(days=1)
    return out


DEMERGER = date(2026, 9, 21)
PARENT = _bars("INE000B01011", {date(2026, 9, 1): 200.0, DEMERGER: 150.0},
               date(2026, 9, 1), date(2026, 10, 30))  # fmt: skip
CHILD = _bars("INE000C01011", {date(2026, 10, 12): 50.0}, date(2026, 10, 12), date(2026, 10, 30))
FLAT = {b.day: (1000.0, 1000.0) for b in PARENT}


def _demerger(research=(RESEARCH,)) -> Proposal:
    p = Proposal("demergers", "BBB", "INE000B01011", DEMERGER, "Demerger")
    p.research = list(research)
    return p


def test_a_demerger_gap_the_resulting_company_confirms_has_two_versions():
    out = Outcome()
    got = actions.decide_demerger(
        "INE000B01011", _demerger(), PARENT, Fraction(1),
        {"INE000C01011": CHILD}, {("r.csv", 2): "INE000C01011"}, FLAT, out,
    )  # fmt: skip
    assert [(r["basis"], r["value"], r["status"], r["available_basis"]) for r in got] == [
        ("index_neutral_gap", 0.75, "accepted", "settled_close"),
        ("resulting_ratio", 0.75, "accepted", "resulting_first_close"),
    ]
    assert got[0]["available_at"] == datetime(2026, 9, 25, 16)
    assert got[1]["available_at"] == datetime(2026, 10, 12, 16)
    assert got[0]["method"] == "market_gap; ratio 0.7500"
    assert out.review == []


def test_a_demerger_without_research_needs_it_unless_the_drop_held():
    out = Outcome()
    flat = _bars("INE000B01011", {date(2026, 9, 1): 200.0}, date(2026, 9, 1), date(2026, 10, 30))
    assert actions.decide_demerger("B", _demerger(()), flat, Fraction(1), {}, {}, FLAT, out) == []
    assert [r["reason"] for r in out.review] == ["needs_research:no_drop"]
    got = actions.decide_demerger("B", _demerger(()), PARENT, Fraction(1), {}, {}, FLAT, out)
    assert _decided(got) == [("demerger", 0.75, "market_gap", "settled_close")]


def test_a_demerger_research_calls_an_amalgamation_or_doubts_is_not_priced():
    out = Outcome()
    merger = replace(RESEARCH, leg_type="amalgamation")
    doubt = replace(RESEARCH, confidence=0)
    for research in ((merger,), (doubt,)):
        got = actions.decide_demerger(
            "B", _demerger(research), PARENT, Fraction(1), {}, {}, FLAT, out
        )
        assert got == []
    assert [r["reason"] for r in out.skipped] == ["amalgamation_only"]
    assert [r["reason"] for r in out.review] == ["low_confidence"]


def _dividend(cash: str, table: str = "dividends") -> Proposal:
    p = Proposal(table, "AAA", "INE000A01011", EX, "Dividend", cash=Decimal(cash), hint="final")
    return p


SERIES = [BEFORE, _on(50.0)]


def test_a_dividend_is_cash_against_the_previous_close():
    out = Outcome()
    got = actions.decide_cash("C", _dividend("2"), SERIES, out)
    assert (got["cash"], got["value"], got["method"], got["available_basis"]) == (
        2.0, 1.0, "dividend", "ex_open",
    )  # fmt: skip
    assert json.loads(got["evidence"])["tr_factor"] == "0.98"


def test_cash_over_half_the_price_or_before_the_bars_is_not_stored():
    out = Outcome()
    assert actions.decide_cash("C", _dividend("60"), SERIES, out) is None
    assert actions.decide_cash("C", _dividend("2"), [_on(50.0)], out) is None
    assert actions.decide_cash("C", _dividend("2"), [], out) is None
    assert [r["reason"] for r in out.review] == ["amount_vs_price", "no_bars"]
    assert [r["reason"] for r in out.skipped] == ["before_bars"]


def test_a_bonus_debenture_is_cash_only_when_research_values_it():
    instrument = replace(RESEARCH, table="bonus_instrument", value_per_share=Fraction(5))
    reasons = []
    for research in ((), (replace(instrument, confidence=0),),
                     (replace(instrument, notes="public only"),),
                     (replace(instrument, value_per_share=Fraction(1, 2)),)):  # fmt: skip
        out = Outcome()
        p = _dividend("0", "bonus_instruments")
        p.cash, p.research = None, list(research)
        assert actions.decide_cash("C", p, SERIES, out) is None
        reasons += [r["reason"] for r in out.review + out.skipped]
    assert reasons == ["needs_value", "low_confidence", "some_holders_only", "below_one_percent"]
    p = _dividend("0", "bonus_instruments")
    p.cash, p.research = None, [instrument]
    got = actions.decide_cash("C", p, SERIES, Outcome())
    assert (got["cash"], got["method"], got["basis"], got["available_basis"]) == (
        5.0, "bonus_instrument_as_cash", "research_value", "research_source_date",
    )  # fmt: skip


def _held(chain: str, ex: date, value: float = 0.5) -> dict:
    return {
        "chain_isin": chain, "isin": chain, "symbol": "X", "ex_date": ex, "action_type": "split",
        "status": "accepted", "value": value, "cash": None, "available_at": None,
    }  # fmt: skip


def test_corrections_add_move_and_refuse_what_they_cannot_apply():
    a, b = "INE000A01011", "INE000B01011"
    later = date(2026, 9, 21)
    found = [_held(a, EX), _held(b, EX), _held(b, later)]
    fixes = [
        Correction(1, "add", a, EX, "split", Fraction(1, 2), None, "dup"),
        Correction(2, "add", a, later, "split", Fraction(1, 5), None, "missing", date(2026, 9, 1)),
        Correction(3, "move", a, EX, "split", None, date(2026, 9, 28), "wrong date"),
        Correction(4, "move", b, EX, "split", None, later, "onto another"),
        Correction(5, "delete", "INE000C01011", EX, "split", None, None, "absent"),
    ]
    log = apply_corrections(found, fixes, Chains([], [], []), {})
    assert [r["status"] for r in log] == [
        "exists_use_replace", "applied", "applied", "destination_exists", "not_found",
    ]  # fmt: skip
    added = [(v["ex_date"], v["value"], v["basis"], v["available_basis"]) for v in found[3:]]
    assert added == [
        (later, 0.2, "correction", "correction"),
        (EX, 1.0, "correction_delete", "correction_undated"),
        (date(2026, 9, 28), 0.5, "correction_move", "correction_undated"),
    ]


def _con(tables: dict[str, list[dict]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for name, rows in tables.items():
        frame = pl.DataFrame(rows)  # noqa: F841 - read by the query below
        con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM frame')
    return con


def _feed(table: str, **row) -> dict:
    return {"symbol": "AAA", "isin": "INE000A01011", "ex_date": EX, "market": "equities",
            "announced_on": date(2026, 8, 20), "notice_url": "n", **row}  # fmt: skip


def test_proposals_read_the_feed_and_attach_research():
    split = {"subject": "Split", "instrument_class": "equity", "classification_source": None}
    debentures = {**split, "subject": "Bonus debentures"}
    con = _con({
        "splits_bonus": [
            _feed("s", **split, parse_status="ok", legs="split=1/2"),
            _feed("s", **debentures, parse_status="unparsed", legs=""),
            _feed("s", **split, parse_status="unparsed", legs="", ex_date=None),
        ],
        "dividends": [
            _feed("d", subjects="Dividend", status="ok", amount=2.5, kinds="final"),
            _feed("d", subjects="Pref", status="preference_dividend", amount=None, kinds=""),
            _feed("d", subjects="?", status="unparsed", amount=None, kinds=""),
        ],
        "demergers": [_feed("m", subject="Demerger", isin="INE000B01011", symbol="BBB",
                            ex_date=DEMERGER)],
    })  # fmt: skip
    wrong = replace(RESEARCH, line=3, wrong="yes: other isin")
    extra = replace(RESEARCH, line=4, table="rights", symbol="RRR", isin="", ex_date=EX)
    got = actions.proposals(con, [RESEARCH, extra])
    assert [(p.table, p.reason, p.cash, p.legs, len(p.research), p.source) for p in got] == [
        ("splits_bonus", "", None, (Leg("split", Fraction(1, 2)),), 0, "nse_feed"),
        ("splits_bonus", "skip:non_equity_bonus", None, (), 0, "nse_feed"),
        ("demergers", "", None, (), 1, "nse_feed"),
        ("dividends", "", Decimal("2.5"), (), 0, "nse_feed"),
        ("dividends", "skip:preference_dividend", None, (), 0, "nse_feed"),
        ("dividends", "unparsed", None, (), 0, "nse_feed"),
        ("rights_issues", "", None, (), 1, "research"),
    ]
    assert got[0].hint == "class equity (no evidence)" and got[0].announced_on == date(2026, 8, 20)
    assert actions.proposals(con, [wrong])[2].reason == "research_says_row_wrong"


# --- one build over a small archive --------------------------------------------------------------


def _write_world(root) -> tuple[Layout, object]:
    layout = Layout(root / "data")
    curation = root / "curation"
    (curation / "research").mkdir(parents=True)
    layout.bars.mkdir(parents=True)
    layout.index.mkdir(parents=True)
    a = {date(2026, 8, 24): 100.0, EX: 50.0}
    series = {
        ("INE000A01011", "AAA"): _bars("INE000A01011", a, date(2026, 8, 24), date(2026, 11, 6)),
        ("INE000B01011", "BBB"): _bars(
            "INE000B01011", {date(2026, 8, 24): 200.0, DEMERGER: 150.0},
            date(2026, 8, 24), date(2026, 11, 6),
        ),
        ("INE000C01011", "CCC"): _bars("INE000C01011", {date(2026, 10, 12): 50.0},
                                       date(2026, 10, 12), date(2026, 11, 6)),
    }  # fmt: skip
    by_day: dict[date, list[dict]] = {}
    for (isin, symbol), bars in series.items():
        for b in bars:
            by_day.setdefault(b.day, []).append({
                "date": b.day, "isin": isin, "symbol": symbol, "series": "EQ", "open": b.open,
                "high": b.open, "low": b.open, "close": b.close, "prev_close": b.prev_close,
                "volume": 1, "traded_value": 1.0,
            })  # fmt: skip
    for day, rows in by_day.items():
        pl.DataFrame(rows, schema=BAR_SCHEMA).write_parquet(layout.bars / f"date={day}.parquet")
        index = {"date": day, "index_name": "NIFTY 500", "open": 1000.0, "high": 1000.0,
                 "low": 1000.0, "close": 1000.0}  # fmt: skip
        pl.DataFrame([index], schema=INDEX_SCHEMA).write_parquet(
            layout.index / f"date={day}.parquet"
        )
    research = {k: "" for k in RESEARCH_COLUMNS} | {
        "table": "demerger", "symbol": "BBB", "isin": "INE000B01011", "ex_date": "2026-09-21",
        "leg_type": "demerger", "resulting_symbol": "CCC", "resulting_isin": "INE000C01011",
        "ratio_a": "1", "ratio_b": "1", "our_data_wrong": "no", "confidence": "high",
        "source_url": "https://example.com/scheme", "source_date": "2026-09-01",
    }  # fmt: skip
    pl.DataFrame([research]).write_csv(curation / "research" / "r.csv")
    (curation / "corrections.tsv").write_text(
        "replace\tINE000A01011\t2026-10-05\tdividend\t2.5\t\tboard revised\t2026-10-01\n",
        encoding="utf-8",
    )
    split = {"instrument_class": "equity", "classification_source": "nse"}
    tables = {
        "splits_bonus": [
            _feed("s", subject="Face Value Split", **split, parse_status="ok", legs="split=1/2"),
            _feed("s", subject="Bonus 1:1", **split, parse_status="ok", legs="bonus=1/2",
                  isin="INE000Z01011", symbol="ZZZ"),
            _feed("s", subject="Bonus ?", **split, parse_status="unparsed", legs="",
                  ex_date=date(2026, 10, 19)),
        ],
        "dividends": [
            _feed("d", subjects="Dividend", status="ok", amount=2.0, kinds="final",
                  ex_date=date(2026, 10, 5)),
            _feed("d", subjects="Pref", status="not_equity_share", amount=None, kinds="",
                  isin="INE000B01011", symbol="BBB"),
        ],
        "demergers": [_feed("m", subject="Demerger", isin="INE000B01011", symbol="BBB",
                            ex_date=DEMERGER)],
        "ca_feed": [_feed("f", subject="Dividend", ex_date=date(2026, 10, 5))],
    }  # fmt: skip
    with duckdb.connect(str(layout.db)) as con:
        for name, rows in tables.items():
            frame = pl.DataFrame(rows)  # noqa: F841 - read by the query below
            con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM frame')
    return layout, curation


def test_build_accepts_confirmed_events_and_reviews_the_rest(tmp_path):
    layout, curation = _write_world(tmp_path)
    summary = actions.build(layout, curation, datetime(2026, 11, 10, 18))
    assert summary == {
        "lineage": {},
        "events": {"demerger": 1, "dividend": 1, "split": 1},
        "versions": {
            "correction": 1, "ex_open": 2, "resulting_first_close": 1, "settled_close": 1,
        },
        "disputed": 0,
        "available_at_unknown": 0,
        "withdrawn_from_ledger": 0,
        "review": {"no_instrument": 1, "unparsed": 1},
        "skipped": {"not_equity_share": 1},
        "corrections": {"applied": 1},
    }  # fmt: skip
    with duckdb.connect(str(layout.db), read_only=True) as con:
        rows = con.execute(
            "SELECT action_type, ex_date, value, cash, basis FROM actions ORDER BY ex_date"
        ).fetchall()
    assert rows == [
        ("split", EX, 0.5, None, "stated_ratio"),
        ("demerger", DEMERGER, 0.75, None, "resulting_ratio"),
        ("dividend", date(2026, 10, 5), 1.0, 2.5, "correction"),
    ]
