from datetime import date
from fractions import Fraction

import pytest

from nse_noodles.curation import (
    RESEARCH_COLUMNS,
    Correction,
    CurationError,
    read_corrections,
    read_research,
)

ISIN = "INE001A01011"


def _research(tmp_path, **cells):
    folder = tmp_path / "research"
    folder.mkdir()
    row = {k: "" for k in RESEARCH_COLUMNS} | {"table": "rights", "ex_date": "2026-06-10"}
    row |= cells
    lines = [",".join(RESEARCH_COLUMNS), ",".join(row[k] for k in RESEARCH_COLUMNS)]
    (folder / "r.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read_research(folder)


def test_research_numbers(tmp_path):
    (row,) = _research(
        tmp_path,
        table="Demerger",
        leg_type="Amalgamation",
        ratio_a="1",
        ratio_b="2",
        issue_price="1250.50",
        coa_parent_pct="62.5",
    )
    assert (row.table, row.leg_type) == ("demerger", "amalgamation")
    assert row.ratio == (Fraction(1), Fraction(2))
    assert row.issue_price == Fraction("1250.5")
    assert row.coa_parent == Fraction(125, 2)
    assert (row.coa_resulting, row.value_per_share) == (None, None)


@pytest.mark.parametrize(
    ("cells", "message"),
    [
        ({"issue_price": "Rs 12.5"}, "issue_price"),
        ({"ratio_a": "2:1", "ratio_b": "1"}, "ratio_a"),
        ({"ratio_a": "1", "ratio_b": "-5"}, "ratio_b"),
        ({"coa_parent_pct": "0"}, "coa_parent_pct"),
        ({"coa_resulting_pct": "abc"}, "coa_resulting_pct"),
        ({"value_per_share_rs": "-1"}, "value_per_share_rs"),
        ({"ratio_a": "1"}, "ratio_a and ratio_b"),
        ({"ratio_b": "3"}, "ratio_a and ratio_b"),
    ],
)
def test_research_bad_number_is_an_error_not_a_fallback(tmp_path, cells, message):
    with pytest.raises(CurationError, match=rf"r\.csv:2: .*{message}"):
        _research(tmp_path, **cells)


@pytest.mark.parametrize(
    ("cells", "message"),
    [
        ({"table": "demergers"}, "table"),
        ({"table": ""}, "table"),
        ({"leg_type": "right"}, "leg_type"),
    ],
)
def test_research_unknown_table_or_leg_type_is_an_error(tmp_path, cells, message):
    with pytest.raises(CurationError, match=rf"r\.csv:2: .*{message}"):
        _research(tmp_path, **cells)


def _read(tmp_path, *lines):
    path = tmp_path / "corrections.tsv"
    path.write_text("\n".join("\t".join(cells) for cells in lines) + "\n", encoding="utf-8")
    return read_corrections(path)


def test_read_corrections_accepts_each_op(tmp_path):
    rows = _read(
        tmp_path,
        ("op", "isin", "ex_date", "type", "value", "new_ex_date", "note", "known_on"),
        ("# a comment",),
        ("add", ISIN.lower(), "2026-06-10", "bonus", "0.5", "", "bonus 1:1", "2026-05-01"),
        ("replace", ISIN, "10-Jun-2026", "split", "5", "", "consolidation 1:5"),
        ("replace", ISIN, "2026-06-11", "dividend", "12.5", "", "special dividend"),
        ("delete", ISIN, "2026-06-12", "other", "", "", "not an event"),
        ("move", ISIN, "2026-06-13", "rights", "", "2026-06-16", "ex-date moved"),
    )
    assert rows == [
        Correction(3, "add", ISIN, date(2026, 6, 10), "bonus", Fraction(1, 2), None,
                   "bonus 1:1", date(2026, 5, 1)),
        Correction(4, "replace", ISIN, date(2026, 6, 10), "split", Fraction(5), None,
                   "consolidation 1:5"),
        Correction(5, "replace", ISIN, date(2026, 6, 11), "dividend", Fraction(25, 2), None,
                   "special dividend"),
        Correction(6, "delete", ISIN, date(2026, 6, 12), "other", None, None, "not an event"),
        Correction(7, "move", ISIN, date(2026, 6, 13), "rights", None, date(2026, 6, 16),
                   "ex-date moved"),
    ]  # fmt: skip


def test_read_corrections_missing_file(tmp_path):
    assert read_corrections(tmp_path / "absent.tsv") == []


@pytest.mark.parametrize(
    "cells",
    [
        ("drop", ISIN, "2026-06-10", "bonus", "0.5", "", "note"),  # unknown op
        ("add", ISIN, "2026-06-10", "merger", "0.5", "", "note"),  # unknown type
        ("add", ISIN, "someday", "bonus", "0.5", "", "note"),  # bad ex-date
        ("add", "NOTANISIN", "2026-06-10", "bonus", "0.5", "", "note"),  # bad ISIN
        ("add", ISIN, "2026-06-10", "bonus", "0.5", "", ""),  # no note
        ("add", ISIN, "2026-06-10", "bonus", "", "", "note"),  # add without value
        ("replace", ISIN, "2026-06-10", "dividend", "0", "", "note"),  # value not positive
        ("add", ISIN, "2026-06-10", "bonus", "1", "", "note"),  # price factor not below 1
        ("add", ISIN, "2026-06-10", "split", "1", "", "note"),  # a split of 1 is no split
        ("move", ISIN, "2026-06-10", "bonus", "", "", "note"),  # move without new ex-date
        ("delete", ISIN, "2026-06-10", "bonus", "", "", "note", "soon"),  # bad known_on
    ],
)
def test_read_corrections_rejects_bad_lines(tmp_path, cells):
    with pytest.raises(CurationError, match=r"corrections\.tsv:1: bad correction line"):
        _read(tmp_path, cells)
