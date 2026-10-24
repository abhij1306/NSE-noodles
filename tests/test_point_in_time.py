from dataclasses import replace
from datetime import date, datetime
from fractions import Fraction

import polars as pl

from nse_noodles import adjusted, ca_db, versions
from nse_noodles.actions import Chains, apply_corrections, research_known
from nse_noodles.curation import Correction, ResearchRow
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
    floor = versions.at_open(date(2026, 9, 7))
    dated = replace(ROW, source_date=date(2026, 9, 20))
    assert research_known([dated], floor) == (
        datetime(2026, 9, 20, 23, 59, 59),
        "research_source_date",
    )
    # a source published before the ex-date: usable from the price the decision read
    early = replace(ROW, source_date=date(2026, 8, 1))
    assert research_known([early], floor)[0] == floor
    assert research_known([dated, ROW], floor) == (None, "research_undated")


def _v(ex, available, value, chain="A", kind="demerger", status="accepted"):
    return {
        "chain_isin": chain, "isin": chain, "ex_date": ex, "action_type": kind, "status": status,
        "value": value, "cash": None, "basis": "b", "available_at": available,
        "source_published_at": None,
    }  # fmt: skip


EX = date(2026, 9, 7)
CONFIRMED = datetime(2026, 9, 11, 16)  # the gap held five sessions on
RATIO = datetime(2026, 10, 20, 16)  # the resulting company's first close


def test_replay_at_a_frozen_cutoff_ignores_later_versions():
    before = [_v(EX, CONFIRMED, 0.36)]
    frozen = versions.select(
        versions.frame(versions.number(before)), versions.cutoff(EX.replace(day=15))
    )
    # a later confirmation adds a version; the earlier one is not touched
    after = [*before, _v(EX, RATIO, 0.34)]
    table = versions.frame(versions.number(after))
    assert versions.select(table, versions.cutoff(date(2026, 9, 15))).equals(frozen)
    assert versions.select(table, versions.cutoff(date(2026, 10, 20)))["value"].to_list() == [0.34]
    assert versions.select(table, None)["value"].to_list() == [0.34]  # hindsight: the latest
    # before the first version was available the replay has no factor at all
    assert versions.select(table, versions.cutoff(date(2026, 9, 10))).is_empty()


def test_unknown_availability_never_qualifies_for_a_replay():
    table = versions.frame(versions.number([_v(EX, None, 0.5), _v(EX, None, 0.9, chain="B")]))
    assert versions.select(table, versions.cutoff(date(2099, 1, 1))).is_empty()
    assert versions.select(table, None).height == 2  # hindsight still shows them


def test_replay_needs_the_ex_date_and_skips_disputed_versions():
    early = datetime(2026, 9, 1, 23, 59, 59)  # a correction public before its ex-date
    table = versions.frame(
        versions.number([_v(EX, early, 0.5), _v(EX, early, 0.4, status="disputed")])
    )
    assert versions.select(table, versions.cutoff(date(2026, 9, 4))).is_empty()
    assert versions.select(table, versions.cutoff(EX))["value"].to_list() == [0.5]


def test_ledger_keeps_the_first_observation():
    v = versions.number([_v(EX, CONFIRMED, 0.36)])[0]
    first = datetime(2026, 9, 11, 18)
    _, new = versions.stamp([dict(v)], {}, first)
    assert len(new) == 1
    again, new = versions.stamp([dict(v)], {v["version_id"]: first}, datetime(2026, 10, 1))
    assert not new and again[0]["first_observed_at"] == first


def test_corrections_add_versions_and_never_edit_one():
    held = {
        **_v(EX, versions.at_open(EX), 0.5, chain="INE000X01011", kind="split"),
        "symbol": "X", "announced_on": None, "notice_url": "", "raw_gap": None,
        "band_low": None, "band_high": None, "method": "gate", "source": "nse_feed",
        "text": "", "evidence": "{}", "available_basis": "ex_open",
    }  # fmt: skip
    found = [held]
    fix = Correction(1, "replace", "INE000X01011", EX, "split", Fraction(1, 10), None, "circular",
                     date(2026, 9, 20))  # fmt: skip
    gone = Correction(2, "delete", "INE000X01011", EX, "split", None, None, "not a split")
    log = apply_corrections(found, [fix, gone], Chains([], [], []), {})
    assert [r["status"] for r in log] == ["applied", "applied"]
    assert held["value"] == 0.5 and [v["value"] for v in found] == [0.5, 0.1, 1.0]
    table = versions.frame(versions.number(found))
    replay = {
        d: versions.select(table, versions.cutoff(d))["value"].to_list()
        for d in (EX, date(2026, 9, 20), date(2026, 12, 1))
    }
    # the undated deletion never enters a replay; the dated fix does from its known_on
    assert replay == {EX: [0.5], date(2026, 9, 20): [0.1], date(2026, 12, 1): [0.1]}
    assert versions.select(table, None)["value"].to_list() == [1.0]


def test_pending_flags_bars_until_the_first_version_is_available():
    days = [date(2026, 9, d) for d in (4, 7, 8, 11, 14)]
    bars = pl.DataFrame({"chain_isin": ["A"] * 5, "date": days})
    first = pl.DataFrame(
        {
            "chain_isin": ["A"],
            "ex_date": [EX],
            "action_type": ["demerger"],
            "first_available_at": [CONFIRMED],
        }
    )
    assert adjusted.pending(bars, first).to_list() == [False, True, True, False, False]


def test_lag_sessions():
    sessions = [date(2026, 9, d) for d in (4, 7, 8, 9, 10, 11)]
    assert lag_sessions(sessions, EX, CONFIRMED) == 4
    assert lag_sessions(sessions, EX, versions.at_open(EX)) == 0
    assert lag_sessions(sessions, EX, None) is None
