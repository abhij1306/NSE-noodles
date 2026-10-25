import pytest

from nse_noodles.curation import RESEARCH_COLUMNS, CurationError, read_corrections, read_research


def _research(tmp_path, **cells):
    folder = tmp_path / "research"
    folder.mkdir()
    row = {k: "" for k in RESEARCH_COLUMNS} | {"table": "demerger", "ex_date": "2025-01-06"}
    row |= cells
    lines = [",".join(RESEARCH_COLUMNS), ",".join(row[k] for k in RESEARCH_COLUMNS)]
    (folder / "r.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def test_bad_research_date_is_an_error_not_a_dropped_row(tmp_path):
    folder = _research(tmp_path, ex_date="06/01/2025")
    with pytest.raises(CurationError, match="ex_date"):
        read_research(folder)


def test_consolidation_correction_may_exceed_one(tmp_path):
    path = tmp_path / "corrections.tsv"
    path.write_text("add\tINE000X01011\t2024-03-01\tsplit\t10\t\tRe 1 -> Rs 10\n", encoding="utf-8")
    assert read_corrections(path)[0].value == 10
    path.write_text("add\tINE000X01011\t2024-03-01\tbonus\t2\t\tnot a bonus\n", encoding="utf-8")
    with pytest.raises(CurationError):
        read_corrections(path)
