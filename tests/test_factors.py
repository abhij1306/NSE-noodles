from datetime import date
from decimal import Decimal
from fractions import Fraction

from nse_noodles import factors


def test_rights_factor_is_terp_over_cum_price():
    # 1 new for 9 held at Rs 150, cum price Rs 215.30
    cum = Fraction("215.30")
    f = factors.rights_factor(Fraction(1), Fraction(9), Fraction(150), cum)
    assert f == factors.terp(Fraction(1), Fraction(9), Fraction(150), cum) / cum
    assert f == (9 * cum + 150) / (10 * cum)
    assert round(float(f), 4) == 0.9697


def test_rights_at_or_above_price_is_neutral():
    assert factors.rights_factor(Fraction(1), Fraction(2), Fraction(100), Fraction(90)) == 1
    assert factors.rights_factor(Fraction(1), Fraction(2), Fraction(90), Fraction(90)) == 1


def test_bonus_and_public_only_bonus():
    assert factors.bonus_factor(1, 1) == Fraction(1, 2)
    assert factors.bonus_factor(3, 2) == Fraction(2, 5)
    # a public-only bonus is old shares / new shares, not the stated ratio
    assert factors.public_only_bonus_factor(Fraction(1000), Fraction(1050)) == Fraction(20, 21)


def test_gate_confirms_rejects_and_calls_ambiguous():
    # a 1:1 bonus, previous close 200, ex open 101
    assert factors.gate(Fraction(1, 2), Fraction(200), Fraction(101)).confirmed
    # the gap is 0.8: more than 20% away from 0.5
    assert factors.gate(Fraction(1, 2), Fraction(200), Fraction(160)).reason == "off_factor"
    # a 3% rights factor against a flat open: a session move explains it as well
    assert factors.gate(Fraction(97, 100), Fraction(100), Fraction(99)).reason == "ambiguous"


def test_demerger_market_factor_rules():
    gap = Fraction(6054, 10000)
    # a cross-check that agrees stores the gap, not the check
    assert factors.market_factor(gap, {"coa": Fraction(6885, 10000)}, True, None)[0] == gap
    # every check disagrees: stored only if confirmed, material and held
    held = Fraction(61, 100)
    f, why = factors.market_factor(gap, {"ratio": Fraction(9, 10)}, True, held)
    assert f == gap and why == "gap_disagrees_material_drop_held"
    assert (
        factors.market_factor(gap, {}, True, Fraction(95, 100))[1] == "no_cross_check_gap_not_held"
    )
    assert factors.market_factor(Fraction(95, 100), {}, True, Fraction(95, 100))[1].endswith(
        "gap_not_material"
    )
    assert factors.market_factor(gap, {}, False, held)[1] == "no_cross_check_date_unconfirmed"


def test_ratio_and_coa_cross_checks():
    # 1 resulting share per parent share, resulting first close 45.60, parent close 484.70
    f = factors.ratio_factor([(Fraction(1), Fraction(1), Fraction("45.60"))], Fraction("484.70"))
    assert round(float(f), 4) == 0.9059
    assert factors.coa_factor(Fraction("68.85"), [Fraction("31.15")]) == Fraction("0.6885")
    assert factors.coa_factor(Fraction(70), [Fraction(20)]) == "coa_does_not_sum"
    assert factors.coa_factor(None, []) is None


def test_dividend_factor_and_cash_rules():
    f = factors.dividend_factor(Decimal("7.50"), Decimal("430.35"))
    assert f is not None and round(f, 6) == Decimal("0.982572")
    assert factors.dividend_factor(Decimal(500), Decimal(400)) is None
    assert factors.over_yield(Decimal(60), Decimal(100))
    assert factors.bonus_instrument_counts(Decimal("12.50"), Decimal("154.75"))
    assert not factors.bonus_instrument_counts(Decimal("0.85"), Decimal(100))


def test_demerger_gap_is_neutralised_by_the_index_move_that_night():
    levels = {date(2026, 9, 4): (100.0, 100.0), date(2026, 9, 7): (102.0, 101.0)}
    night = factors.index_move(levels, date(2026, 9, 4), date(2026, 9, 7), at_open=True)
    assert night == Fraction(102, 100)
    raw = Fraction(260) / Fraction("728.25")
    assert raw / night < raw  # a market up 2% that morning hid part of the drop
    assert factors.index_move(levels, date(2026, 9, 3), date(2026, 9, 7), at_open=True) is None
    assert factors.index_move(
        levels, date(2026, 9, 4), date(2026, 9, 7), at_open=False
    ) == Fraction(101, 100)


def test_demerger_versions_agree_within_tolerance_and_band():
    assert factors.versions_agree(Fraction(36, 100), Fraction(38, 100))
    assert not factors.versions_agree(Fraction(36, 100), Fraction(42, 100))
    assert factors.band([Fraction(3, 10), Fraction(36, 100), Fraction(33, 100)]) == (
        Fraction(3, 10),
        Fraction(36, 100),
    )
