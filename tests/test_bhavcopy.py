import io
import zipfile
from datetime import date

import pytest

from nse_noodles import bars, bhavcopy
from nse_noodles.http import Fetched
from nse_noodles.layout import Layout, NotFoundMemo, write_bytes

DAY = date(2012, 3, 6)
HEADER = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,ISIN"


def _zip(*lines: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("cm06MAR2012bhav.csv", "\n".join([HEADER, *lines]) + "\n")
    return buf.getvalue()


def test_parse_keeps_one_valid_row_per_isin():
    parsed = bhavcopy.parse(
        DAY,
        _zip(
            "ABC,BE,10,12,9,11,11,10,100,1100,06-MAR-2012,INE000A01011",
            'ABC,EQ,10,12,9,11.5,11,10,"1,000",11500,06-MAR-2012,INE000A01011',
            "ABC,BL,10,10,10,10,10,10,50,500,06-MAR-2012,INE000A01011",
            "XYZ,N1,100,100,100,100,100,100,1,100,06-MAR-2012,INE000B01011",
            "OLD,EQ,5,6,4,5,5,5,10,50,05-MAR-2012,INE000C01011",
            "BAD,EQ,10,12,11,11,11,10,100,1100,06-MAR-2012,INE000D01011",
            "NAN,EQ,nan,12,9,11,11,10,100,1100,06-MAR-2012,INE000E01011",
            "FRAC,EQ,10,12,9,11,11,10,10.5,1100,06-MAR-2012,INE000F01011",
        ),
    )
    assert parsed.rows == [
        {
            "date": DAY, "isin": "INE000A01011", "symbol": "ABC", "series": "EQ", "open": 10.0,
            "high": 12.0, "low": 9.0, "close": 11.5, "prev_close": 10.0, "volume": 1000,
            "traded_value": 11500.0,
        }
    ]  # fmt: skip
    assert parsed.skipped_series == 1
    assert parsed.wrong_date == 1
    assert parsed.invalid == 3
    assert parsed.other_series == 2


def test_parse_needs_exactly_one_csv():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("a.csv", HEADER)
        archive.writestr("b.csv", HEADER)
    with pytest.raises(ValueError, match="exactly one CSV"):
        bhavcopy.parse(DAY, buf.getvalue())


class FakeClient:
    """Answers from a {url-suffix: Fetched} map; anything else is a 404."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url, params=None, *, cookies=False):
        self.asked.append(url)
        for suffix, got in self.answers.items():
            if url.endswith(suffix):
                return got
        return Fetched("not_found")


def test_prev_close_zero_keeps_the_bar_without_it():
    parsed = bhavcopy.parse(DAY, _zip("NEW,EQ,10,12,9,11,11,0,100,1100,06-MAR-2012,INE000A01011"))
    assert [(r["isin"], r["prev_close"]) for r in parsed.rows] == [("INE000A01011", None)]


def test_pull_keeps_only_real_archives_and_forgets_recent_404s(tmp_path):
    layout = Layout(tmp_path)
    days = [date(2012, 3, 5), DAY, date(2012, 3, 7)]
    client = FakeClient(
        {
            "cm05MAR2012bhav.csv.zip": Fetched("ok", b"<html>Access Denied</html>"),
            "cm06MAR2012bhav.csv.zip": Fetched("ok", _zip()),
        }
    )
    report = bhavcopy.pull(layout, client, days)
    assert report.errors == [date(2012, 3, 5)]
    assert report.downloaded == 1
    assert sorted(p.name for p in layout.bhavcopy.iterdir()) == ["cm06MAR2012bhav.csv.zip"]
    assert NotFoundMemo(layout.raw / "not_found.json").has("bhavcopy", date(2012, 3, 7))

    recent = NotFoundMemo(tmp_path / "memo.json", today=date(2026, 10, 23))
    recent.add("bhavcopy", date(2026, 10, 20))
    recent.add("bhavcopy", date(2026, 10, 1))
    assert not recent.has("bhavcopy", date(2026, 10, 20))
    assert recent.has("bhavcopy", date(2026, 10, 1))


def test_build_fails_a_file_with_no_rows_for_its_day(tmp_path):
    layout = Layout(tmp_path)
    write_bytes(
        layout.bhavcopy / "cm06MAR2012bhav.csv.zip",
        _zip("ABC,EQ,10,12,9,11,11,10,100,1100,05-MAR-2012,INE000A01011"),
    )
    report = bhavcopy.build(layout)
    assert report.days == 0
    assert report.wrong_date == 1
    assert report.failed == [
        "cm06MAR2012bhav.csv.zip: no valid rows (wrong date 1, invalid 0, other series 0)"
    ]
    assert not list(layout.bars.glob("*.parquet"))


def test_bars_view_survives_a_quote_in_the_data_path(tmp_path):
    layout = Layout(tmp_path / "o'brien")
    write_bytes(
        layout.bhavcopy / "cm06MAR2012bhav.csv.zip",
        _zip("ABC,EQ,10,12,9,11,11,10,100,1100,06-MAR-2012,INE000A01011"),
    )
    assert bhavcopy.build(layout).days == 1
    assert bars.connect(layout).sql("SELECT isin, close FROM bars").fetchall() == [
        ("INE000A01011", 11.0)
    ]
