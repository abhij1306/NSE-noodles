import io
import zipfile
from datetime import date

import pytest

from nse_noodles import bhavcopy

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
