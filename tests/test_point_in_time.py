from dataclasses import replace
from datetime import date

import polars as pl

from nse_noodles import adjusted, ca_db
from nse_noodles.actions import research_known
from nse_noodles.curation import ResearchRow
from nse_noodles.reports import lag_sessions

ROW = ResearchRow(
    "r.csv", 2, "demerger", "ABC", "", date(2026, 9, 7), "demerger", "", "", None, None, False,
    None, None, "", None, None, "", 2, "", None, "", "",
)  # fmt: skip


def test_first_notice_is_the_earliest_matching_filing_before_the_ex_date():
    notices = [
        (date(2026, 9, 1), "Outcome of board meeting: dividend", "b"),
        (date(2026, 6, 1), "Board meeting to consider sub-division", "a"),
        (date(2026, 9, 20), "Split record date", "late"),
        (date(2025, 1, 1), "Split", "too early"),
    ]
    split = ca_db.NOTICE["splits_bonus"]
    assert ca_db.first_notice(notices, date(2026, 9, 10), split) == (date(2026, 6, 1), "a")
    assert ca_db.first_notice(notices, date(2026, 9, 10), ca_db.NOTICE["rights_issues"]) == (
        None,
        "",
    )


def test_research_known_needs_every_source_date():
    dated = replace(ROW, source_date=date(2026, 9, 20))
    assert research_known([dated], date(2026, 9, 7)) == (date(2026, 9, 20), "research_source_date")
    early = replace(ROW, source_date=date(2026, 8, 1))
    assert research_known([early], date(2026, 9, 7))[0] == date(2026, 9, 7)
    assert research_known([dated, ROW], date(2026, 9, 7)) == (None, "research_undated")


def test_as_of_keeps_only_factors_known_by_then():
    actions = pl.DataFrame(
        {
            "chain_isin": ["A", "A", "B"],
            "ex_date": [date(2026, 9, 7), date(2026, 10, 1), date(2026, 9, 1)],
            "known_on": [date(2026, 9, 11), date(2026, 10, 1), None],
        }
    )
    assert adjusted.available(actions, date(2026, 9, 10)).height == 0
    assert adjusted.available(actions, date(2026, 9, 11))["ex_date"].to_list() == [date(2026, 9, 7)]


def test_pending_flags_bars_between_ex_date_and_known_on():
    days = [date(2026, 9, d) for d in (4, 7, 8, 11, 14)]
    bars = pl.DataFrame({"chain_isin": ["A"] * 5, "date": days})
    actions = pl.DataFrame(
        {
            "chain_isin": ["A"],
            "ex_date": [date(2026, 9, 7)],
            "action_type": ["demerger"],
            "known_on": [date(2026, 9, 11)],
        }
    )
    assert adjusted.pending(bars, actions).to_list() == [False, True, True, False, False]


def test_lag_sessions():
    sessions = [date(2026, 9, d) for d in (4, 7, 8, 9, 10, 11)]
    assert lag_sessions(sessions, date(2026, 9, 7), date(2026, 9, 11)) == 4
    assert lag_sessions(sessions, date(2026, 9, 7), date(2026, 9, 7)) == 0
    assert lag_sessions(sessions, date(2026, 9, 7), None) is None
