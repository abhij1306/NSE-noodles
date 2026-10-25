from datetime import date, timedelta

from nse_noodles import lineage
from nse_noodles.bars import Bar, Span
from nse_noodles.lineage import Action, Candidate, Decision, NameChange, SymbolChange


def _verdicts(decisions):
    return [(d.old_isin, d.new_isin, d.effective, d.status, d.reason) for d in decisions]


def test_unused_owner_link_is_reported_not_silently_ignored():
    """An owner link on a pair the rules never found displaces the rules link sharing its old
    ISIN; the unused override must show up for review, not leave the chain unlinked unseen."""
    rules = [Decision("A", "B", date(2026, 6, 15), "linked", "isin_reissue")]
    out = lineage.apply_overrides(rules, {("A", "C"): "link", ("X", "Y"): "link"})
    assert _verdicts(out) == [
        ("A", "B", date(2026, 6, 15), "review", "owner_displaced"),
        ("A", "C", date(2026, 6, 15), "review", "override_unused"),
        ("X", "Y", date.min, "review", "override_unused"),
    ]
    assert out[1].evidence == {"override": "link"}


def test_used_overrides():
    rules = [
        Decision("A", "B", date(2026, 6, 15), "review", "price_jump"),
        Decision("C", "D", date(2026, 6, 16), "linked", "isin_reissue"),
    ]
    out = lineage.apply_overrides(rules, {("A", "B"): "link", ("C", "D"): "unlink"})
    assert _verdicts(out) == [
        ("A", "B", date(2026, 6, 15), "linked", "owner_link"),
        ("C", "D", date(2026, 6, 16), "review", "owner_unlink"),
    ]
    assert out[0].evidence == {"rules": "review:price_jump"}


OLD, NEW = "INE001A01011", "INE001A01029"
MON = date(2026, 6, 1)
SESSIONS = [MON + timedelta(days=d) for d in range(60) if (MON + timedelta(days=d)).weekday() < 5]


def _span(isin, symbol, first, last, closes=(100.0,), opens=None, prev=None, last_symbol=None):
    """A span whose bars run on consecutive sessions from `first` (head) and up to `last` (tail)."""
    opens = opens or closes
    head = tuple(
        Bar(isin, symbol, SESSIONS[SESSIONS.index(first) + i], o, c, None)
        for i, (o, c) in enumerate(zip(opens, closes, strict=True))
    )
    tail = tuple(
        Bar(isin, symbol, SESSIONS[SESSIONS.index(last) - len(closes) + 1 + i], o, c, None)
        for i, (o, c) in enumerate(zip(opens, closes, strict=True))
    )
    return Span(isin, first, last, symbol, last_symbol or symbol, prev, head, tail)


def _candidate(old_symbol="ABC", new_symbol="ABC", gap=0, **new):
    old = _span(OLD, old_symbol, SESSIONS[0], SESSIONS[9], closes=(100.0, 100.0))
    first = SESSIONS[10 + gap]
    defaults = {"closes": (100.0, 101.0), "prev": 100.0}
    new_span = _span(NEW, new_symbol, first, SESSIONS[30], **{**defaults, **new})
    via = "same_symbol" if old_symbol == new_symbol else "symbol_change"
    return Candidate(old, new_span, gap, via)


def _why(c, actions=(), names=(), ambiguous=False):
    d = lineage.decide(c, list(actions), names, ambiguous=ambiguous)
    return d.status, d.reason


def test_decide_links_and_names_the_kind_of_change():
    c = _candidate()
    assert _why(c) == ("linked", "isin_reissue")
    assert _why(_candidate("ABC", "ABCD")) == ("linked", "symbol_change")
    renamed = [NameChange("ABC", SESSIONS[12])]
    assert _why(c, names=renamed) == ("linked", "name_change")
    far_rename = [NameChange("ABC", SESSIONS[0] - timedelta(days=60))]
    assert _why(c, names=far_rename) == ("linked", "isin_reissue")
    split = Action(OLD, SESSIONS[10], "split", 0.2)
    halved = _candidate(closes=(20.0, 20.5), prev=20.0)
    assert _why(halved, [split]) == ("linked", "face_value_change")


def test_decide_evidence():
    d = lineage.decide(_candidate(), [])
    assert (d.old_isin, d.new_isin, d.effective) == (OLD, NEW, SESSIONS[10])
    assert d.evidence == {
        "old_symbol": "ABC",
        "new_symbol": "ABC",
        "old_last_day": "2026-06-12",
        "new_first_day": "2026-06-15",
        "gap_sessions": 0,
        "via": "same_symbol",
        "old_last_close": 100.0,
        "new_prev_close": 100.0,
        "new_first_open": 100.0,
        "gap_factor": 1.0,
        "open_ratio": 1.0,
    }


def test_decide_review_reasons_in_order():
    c = _candidate()
    assert _why(c, ambiguous=True) == ("review", "ambiguous")
    assert _why(_candidate(gap=6)) == ("review", "long_gap")
    scheme = Action(OLD, SESSIONS[11], "other", 1.0, "Scheme of Amalgamation")
    d = lineage.decide(c, [scheme])
    assert (d.reason, d.evidence["restructuring"]) == ("restructuring", ["Scheme of Amalgamation"])
    conflict = [Action(OLD, SESSIONS[10], "split", 0.5), Action(NEW, SESSIONS[10], "split", 0.2)]
    assert _why(c, conflict) == ("review", "factor_conflict")
    assert _why(_candidate(prev=None)) == ("review", "no_prev_close")
    assert _why(_candidate(prev=90.0)) == ("review", "prev_close_mismatch")
    d = lineage.decide(_candidate(opens=(120.0, 101.0)), [])
    assert (d.reason, d.evidence["open_ratio"]) == ("price_jump", 1.2)
    edge = lineage.decide(_candidate(opens=(100.0, 140.0), closes=(100.0, 140.0)), [])
    assert (edge.reason, edge.evidence["edge_move"]) == (
        "price_jump",
        "2026-06-15 -> 2026-06-16 x1.4000",
    )


def test_decide_non_positive_prices_are_a_jump():
    c = _candidate()
    zero = Candidate(
        _span(OLD, "ABC", SESSIONS[0], SESSIONS[9], closes=(100.0, 0.0)), c.new, 0, "same_symbol"
    )
    assert _why(zero) == ("review", "price_jump")


def test_candidates():
    old = _span(OLD, "ABC", SESSIONS[0], SESSIONS[9])
    same = _span(NEW, "ABC", SESSIONS[10], SESSIONS[20])
    renamed = _span("INE001A01037", "XYZ", SESSIONS[11], SESSIONS[20])
    overlapping = _span("INE001A01045", "ABC", SESSIONS[9], SESSIONS[20])
    too_late = _span("INE001A01052", "ABC", SESSIONS[41], SESSIONS[42])
    unrelated = _span("INE002A01011", "QQQ", SESSIONS[10], SESSIONS[20])
    change = SymbolChange("ABC", "XYZ", SESSIONS[10])
    stale = SymbolChange("ABC", "QQQ", SESSIONS[0] - timedelta(days=30))
    found = lineage.candidates(
        [old, same, renamed, overlapping, too_late, unrelated], SESSIONS, [change, stale, change]
    )
    assert [(c.old.isin, c.new.isin, c.gap_sessions, c.via, c.change) for c in found] == [
        (OLD, NEW, 0, "same_symbol", None),
        (OLD, "INE001A01037", 1, "symbol_change", change),
        # OLD -> too_late is 31 sessions apart; the later spans reach it within 30
        (NEW, "INE001A01052", 20, "same_symbol", None),
        ("INE001A01045", "INE001A01052", 20, "same_symbol", None),
    ]


def test_candidates_gap_limit():
    old = _span(OLD, "ABC", SESSIONS[0], SESSIONS[0])
    at_limit = _span(NEW, "ABC", SESSIONS[31], SESSIONS[31])
    assert [c.gap_sessions for c in lineage.candidates([old, at_limit], SESSIONS, [])] == [30]
    past = _span(NEW, "ABC", SESSIONS[32], SESSIONS[32])
    assert lineage.candidates([old, past], SESSIONS, []) == []


def test_parse_symbol_changes_formats():
    header = "SM_NAME,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\nAbc Ltd,ABC,ABCD,15-JUN-2026\n"
    expected = [SymbolChange("ABC", "ABCD", date(2026, 6, 15))]
    assert lineage.parse_symbol_changes(header) == expected
    bare = "Abc Ltd,abc,abcd,15-Jun-26\nshort,row\nSame,XYZ,XYZ,15-Jun-2026\n"
    assert lineage.parse_symbol_changes(bare) == expected
    keyed = "SM_KEY_SYMBOL,SM_NEW_SYMBOL,SM_APPLICABLE_FROM\nABC,ABCD,2026-06-15\nABC,ABCD,bad\n"
    assert lineage.parse_symbol_changes(keyed) == expected
