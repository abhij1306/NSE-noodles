from datetime import date
from decimal import Decimal
from fractions import Fraction

from nse_noodles import parse
from nse_noodles.parse import Leg, Payout


def test_split_bonus_legs():
    assert parse.split_bonus_legs(
        "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"
    ) == (Leg("split", Fraction(1, 10)),)
    assert parse.split_bonus_legs("Bonus 1:2") == (Leg("bonus", Fraction(2, 3)),)
    assert parse.split_bonus_legs(
        "Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
    ) == (Leg("bonus", Fraction(1, 2)), Leg("split", Fraction(1, 5)))
    # a dividend in the same purpose is not read as the split's numbers
    assert parse.split_bonus_legs(
        "Dividend - Rs 1.50 Per Share / Face Value Split - From Rs 10/- To Rs 5/- Per Share"
    ) == (Leg("split", Fraction(1, 2)),)


def test_non_equity_bonus_is_never_a_factor():
    for text in (
        "Bonus Debentures 1:1",
        "Scheme Of Arrangement - Bonus Ncrps 4:1",
        "Bonus - 1 Debenture For 1 Equity Share Held",
    ):
        assert parse.non_equity_bonus(text)
        assert parse.split_bonus_legs(text) is None
    assert not parse.non_equity_bonus("Bonus 1:1")


def test_ratio_parse():
    assert parse.parse_ratio("1:48.18") == (Fraction(1), Fraction("48.18"))
    assert parse.parse_ratio("0:1") is None
    assert parse.parse_ratio("abc") is None
    assert parse.bonus_ratio("Bonus 3 : 2") == (3, 2)


def test_rights_terms():
    t = parse.rights_terms("Rights 1:9 @ Premium Rs 149/-", "1")
    assert t == parse.RightsTerms(Fraction(1), Fraction(9), Fraction(150))
    assert parse.rights_terms("Rights 2:5 At Par", "10") == parse.RightsTerms(
        Fraction(2), Fraction(5), Fraction(10)
    )
    assert parse.rights_terms("Rights 613:399", "10") == "no_issue_price"
    assert parse.rights_terms("Rights 1:5 @ Premium Rs 20/- (Partly Paid)", "10") == "non_simple"


def test_dividend_payouts():
    assert parse.payouts(
        "Interim Dividend - Rs 5 Per Share / Special Dividend - Rs 10 Per Share"
    ) == (
        Payout("interim", Decimal(5)),
        Payout("special", Decimal(10)),
    )
    assert parse.payouts("Dividend - Final Rs 4/- And Special Re 1/- Per Share") == (
        Payout("final", Decimal(4)),
        Payout("special", Decimal(1)),
    )
    assert parse.payouts("Dividend - 25%") == "percent_of_face_value"
    assert parse.payouts("Annual General Meeting") == "no_dividend"
    assert parse.combine(["Dividend - Rs 2 Per Share", "Dividend - Rs 2 Per Share"]) == (
        Payout("final", Decimal(2)),
    )
    assert (
        parse.combine(["Dividend - Rs 2 Per Share", "Dividend - Rs 3 Per Share"]) == "rows_disagree"
    )


def test_dates_and_isins():
    assert parse.nse_date("04-Sep-2014") == date(2014, 9, 4)
    assert parse.nse_date("2014-09-04") == date(2014, 9, 4)
    assert parse.nse_date("-") is None
    assert parse.ordinary_equity("INE155A01022")
    assert not parse.ordinary_equity("INF204KB17I5")  # a fund unit
    assert not parse.ordinary_equity("INE155A20022")  # a rights entitlement


def test_review_fixes():
    assert parse.split_bonus_legs(
        "Face Value Subdivision - From Rs 10/- Per Share To Re 1/- Per Share"
    ) == (Leg("split", Fraction(1, 10)),)
    assert parse.rights_terms("Rights 1:5 @ Premium Rs 1,000/-", "10") == parse.RightsTerms(
        Fraction(1), Fraction(5), Fraction(1010)
    )
    assert parse.payouts("Rs 50/- Per Share (Special Dividend)") == (
        Payout("special", Decimal(50)),
    )
    # a row that only names a dividend is ignored; one with an unreadable figure fails the group
    assert parse.combine(["Dividend - Rs 2 Per Share", "Annual General Meeting/Dividend"]) == (
        Payout("final", Decimal(2)),
    )
    assert parse.combine(["Dividend - Rs 2 Per Share", "Dividend - Rs Two Per Share"]) == (
        "no_amount"
    )


def test_dividend_breakdowns_and_reasons():
    assert parse.payouts(
        "Final Dividend - Rs 10 Per Share (Including Special Dividend - Rs 4 Per Share)"
    ) == (Payout("final", Decimal(6)), Payout("special", Decimal(4)))
    assert parse.payouts(
        "Dividend Rs 10 Per Share (Final Dividend Rs 6 + Special Dividend Rs 4)"
    ) == (Payout("final", Decimal(6)), Payout("special", Decimal(4)))
    assert (
        parse.payouts("Dividend - Rs 10 Per Share (Interim Dividend Rs 4 Per Share)")
        == "breakdown_disagrees"
    )
    assert parse.payouts("Dividend On Preference Shares Rs 2") == "preference_dividend"
    assert parse.payouts("Dividend On Preference Shares Rs 2 / Final Dividend Rs 3 Per Share") == (
        Payout("final", Decimal(3)),
    )
    assert parse.payouts("Interim Dividend Rs 2 Rs 3 Per Share") == "several_amounts"
    assert parse.payouts("Dividend - Rs 0 Per Share") == "no_amount"


def test_combine_feed_rows():
    assert parse.combine(
        ["Interim Dividend - Rs 2 Per Share", "Special Dividend - Rs 3 Per Share"]
    ) == (Payout("interim", Decimal(2)), Payout("special", Decimal(3)))
    assert parse.combine(
        ["Dividend - Rs 2 Per Share", "Dividend - Rs 3 Per Share (Purpose Revised)"]
    ) == (Payout("final", Decimal(3)),)
    assert parse.combine(["Dividend - Rs 2 Per Share", "Dividend - 25%"]) == "percent_of_face_value"
    assert parse.combine(["Annual General Meeting/Dividend"]) == "no_amount"
    assert parse.combine([]) == "no_dividend"
    assert parse.combine(["Dividend On Preference Shares Rs 2"]) == "preference_dividend"
