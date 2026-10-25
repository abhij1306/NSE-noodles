import json

from nse_noodles import reports
from nse_noodles.layout import Layout


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
