from datetime import date

import pytest

from nse_noodles import ca_db, exits, parse

# Thu 3, Fri 4, Mon 7 (Tue 8 a holiday), Wed 9, Thu 10, Fri 11 Sep 2026
SESSIONS = [date(2026, 9, d) for d in (3, 4, 7, 9, 10, 11)]
HOLIDAYS = {date(2026, 9, 8), date(2026, 9, 15)}


@pytest.mark.parametrize(
    ("ex_date", "expected"),
    [
        (date(2026, 9, 7), date(2026, 9, 4)),  # a Monday ex-date exits on Friday
        (date(2026, 9, 9), date(2026, 9, 7)),  # the holiday before it is no session
        (date(2026, 9, 10), date(2026, 9, 9)),
        (date(2026, 9, 3), None),  # no session before it in the data
        # past the last bar: weekdays that are not listed holidays
        (date(2026, 9, 14), date(2026, 9, 11)),  # Monday after the last bar: that Friday
        (date(2026, 9, 16), date(2026, 9, 14)),  # Tuesday the 15th is a holiday
        (date(2026, 9, 12), date(2026, 9, 11)),  # a Saturday ex-date
    ],
)
def test_exit_session_skips_weekends_and_holidays(ex_date, expected):
    assert exits.exit_session(SESSIONS, HOLIDAYS, ex_date) == expected


def test_exit_session_without_bars_reads_the_calendar_only():
    assert exits.exit_session([], HOLIDAYS, date(2026, 9, 9)) == date(2026, 9, 7)


@pytest.mark.parametrize(
    ("announced", "expected"),
    [
        (date(2026, 9, 3), (True, "")),
        (date(2026, 9, 4), (False, "announced_on_exit_session")),  # same day is too late
        (date(2026, 9, 5), (False, "announced_after_exit_session")),
        (None, (False, "announced_on_unknown")),  # never guessed
    ],
)
def test_exitable_only_when_announced_strictly_before_the_exit_session(announced, expected):
    assert exits.exit_status(date(2026, 9, 4), announced, date(2026, 9, 7)) == expected


def test_unknown_record_date_or_no_session_is_not_exitable():
    assert exits.exit_status(date(2026, 9, 4), None, None) == (False, "record_date_unknown")
    assert exits.exit_status(None, date(2026, 8, 1)) == (False, "no_session_before_ex_date")


def _row(isin, ex, record, announced, symbol="ABC"):
    return {
        "isin": isin,
        "symbol": symbol,
        "ex_date": ex,
        "record_date": record,
        "announced_on": announced,
        "notice_source": "announcement" if announced else "",
        "notice_url": "",
    }


def test_calendar_as_of_keeps_only_what_was_announced_by_then():
    ex = date(2026, 9, 7)
    rows = [
        _row("INE000A01011", ex, ex, date(2026, 8, 24)),
        _row("INE000A01011", ex, ex, None),  # the same event under another purpose text
        _row("INE000B01011", ex, ex, date(2026, 9, 2), "LATE"),
        _row("INE000C01011", ex, None, None, "UNDATED"),
    ]
    full = exits.calendar(rows, SESSIONS, HOLIDAYS, {"INE000A01011": "INE000Z01011"})
    assert full.height == 3
    a = full.filter(full["isin"] == "INE000A01011").row(0, named=True)
    assert (a["chain_isin"], a["announced_on"], a["exit_session"], a["exitable"]) == (
        "INE000Z01011",
        date(2026, 8, 24),
        date(2026, 9, 4),
        True,
    )
    early = exits.calendar(rows, SESSIONS, HOLIDAYS, {}, as_of=date(2026, 8, 31))
    assert early["isin"].to_list() == ["INE000A01011"]
    assert exits.calendar(rows, SESSIONS, HOLIDAYS, {}, as_of=date(2026, 9, 2)).height == 2


def test_dates_named_reads_the_formats_filings_use():
    text = (
        "fixed Wednesday, July 22, 2026 as the Record Date; as 07-Sep-2026; on 5th October, 2026;"
        " book closure 06/11/2026; ISO 2026-12-01; not a date 31-Feb-2026"
    )
    assert parse.dates_named(text) == {
        date(2026, 7, 22),
        date(2026, 9, 7),
        date(2026, 10, 5),
        date(2026, 11, 6),
        date(2026, 12, 1),
    }


def test_record_notice_needs_the_record_date_that_held():
    pattern = ca_db.NOTICE["demergers"]
    notices = [
        (date(2026, 6, 22), "Record Date for the purpose of Dividend is 07-Sep-2026", "d", "a"),
        (date(2026, 7, 1), "Record date for the Scheme is 31-Aug-2026", "changed", "a"),
        (date(2026, 8, 24), "Record date fixed as 07-Sep-2026 pursuant to the Scheme", "ok", "a"),
        (date(2026, 8, 25), "Demerger record date September 7, 2026", "later", "a"),
    ]
    ex = date(2026, 9, 7)
    assert ca_db.record_notice(notices, ex, ex, pattern) == (date(2026, 8, 24), "ok", "a")
    assert ca_db.record_notice(notices, ex, None, pattern) == (None, "", "")
    assert ca_db.record_notice(notices[:2], ex, ex, pattern) == (None, "", "")
