import json
from datetime import date, datetime

import duckdb
import polars as pl

from nse_noodles import reports
from nse_noodles.layout import Layout

EQUITY, ENTITLEMENT = "INE001A01011", "INE001A20011"


def _day(d, chain, isin, o, c):
    return {
        "chain_isin": chain,
        "isin": isin,
        "symbol": "AAA",
        "date": date(2024, 6, d),
        "adj_open": float(o),
        "adj_close": float(c),
        "ordinary_equity": isin == EQUITY,
    }


def test_jumps(tmp_path):
    layout = Layout(tmp_path)
    layout.out.mkdir(parents=True)
    pl.DataFrame(
        [
            _day(3, EQUITY, EQUITY, 100, 100),
            _day(4, EQUITY, EQUITY, 125, 100),  # exactly 25%: not a jump
            _day(5, EQUITY, EQUITY, 126, 200),  # beyond 25%
            _day(25, EQUITY, EQUITY, 400, 200),  # 20 days on: a relisting, not a jump
            _day(26, EQUITY, EQUITY, 140, 140),  # down 30%
            # another chain: its first day is never compared with EQUITY's last
            _day(3, ENTITLEMENT, ENTITLEMENT, 10, 10),
            _day(4, ENTITLEMENT, ENTITLEMENT, 5, 5),
        ]
    ).write_parquet(layout.out / "hindsight_daily.parquet")
    with duckdb.connect(str(layout.db)) as con:
        con.execute(
            "CREATE TABLE ca_feed (symbol VARCHAR, isin VARCHAR, ex_date DATE, subject VARCHAR)"
        )
        con.execute("INSERT INTO ca_feed VALUES ('AAA', '', DATE '2024-06-06', 'Bonus 1:4')")
    found = reports.jumps(layout).select(
        "date", "chain_isin", "security", "previous_close", "ratio", "feed_near", "review_near"
    )
    assert found.rows() == [
        (date(2024, 6, 26), EQUITY, "equity", 200.0, 0.7, "", ""),
        (date(2024, 6, 5), EQUITY, "equity", 100.0, 1.26, "Bonus 1:4", ""),
        (date(2024, 6, 4), ENTITLEMENT, "rights_entitlement", 10.0, 0.5, "Bonus 1:4", ""),
    ]


def test_available_after_ex_open_counts_a_version_known_later_on_the_ex_date():
    """A factor known at the ex-date's close (lag 0 sessions) still came too late for a backtest
    trading that open, so it counts as available after the ex-date."""
    ex = date(2026, 9, 7)
    log = pl.DataFrame(
        {
            "ex_date": [ex] * 5,
            "available_at": [
                datetime(2026, 9, 4, 23, 59, 59),  # filed the session before
                datetime(2026, 9, 7, 10, 0),  # the ex-date's open itself
                datetime(2026, 9, 7, 16, 0),  # the ex-date's close
                datetime(2026, 9, 9, 16, 0),  # two sessions on
                None,  # never known
            ],
        }
    )
    assert reports.available_after_ex_open(log) == 2


def _touch(folder, days):
    folder.mkdir(parents=True, exist_ok=True)
    for day in days:
        (folder / f"date={day}.parquet").write_bytes(b"")


def test_coverage(tmp_path):
    layout = Layout(tmp_path)
    _touch(layout.bars, ["2024-06-03", "2024-06-04", "2024-06-08", "2024-06-10", "2024-06-14"])
    _touch(layout.index, ["2024-06-04", "2024-06-10"])
    layout.raw.mkdir(parents=True)
    (layout.raw / "not_found.json").write_text(
        json.dumps({"bhavcopy": ["2024-06-05", "2024-06-06"]}), encoding="utf-8"
    )
    (tmp_path / "curation").mkdir()
    (tmp_path / "curation" / "holidays.txt").write_text("2024-06-05\n2024-06-10\n", "utf-8")
    assert reports.coverage(layout) == {
        "holiday": ["2024-06-05"],
        "gap": ["2024-06-06"],
        "unaccounted": ["2024-06-07", "2024-06-11", "2024-06-12", "2024-06-13"],
        "on_listed_holiday": ["2024-06-10"],
        "weekend_session": ["2024-06-08"],
        "index_missing": ["2024-06-08", "2024-06-14"],
    }


def test_coverage_extra_holidays_and_no_index(tmp_path):
    layout = Layout(tmp_path)
    _touch(layout.bars, ["2024-06-03", "2024-06-05"])
    layout.raw.mkdir(parents=True)
    (layout.raw / "not_found.json").write_text(
        json.dumps({"bhavcopy": ["2024-06-04"]}), encoding="utf-8"
    )
    extra = tmp_path / "extra.txt"
    extra.write_text("2024-06-04  # Election results\n", encoding="utf-8")
    assert reports.coverage(layout, str(extra)) == {
        "holiday": ["2024-06-04"],
        "gap": [],
        "unaccounted": [],
        "on_listed_holiday": [],
        "weekend_session": [],
        "index_missing": [],
    }


def test_coverage_without_bars(tmp_path):
    assert reports.coverage(Layout(tmp_path)) == {}
