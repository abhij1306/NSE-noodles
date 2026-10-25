import io
import json
import zipfile
from datetime import date

import pytest

from nse_noodles import cli, factors, indices, nse_json
from nse_noodles.http import Fetched
from nse_noodles.layout import Layout, write_bytes


class OneAnswer:
    def __init__(self, got):
        self.got = got

    def get(self, url, params=None, *, cookies=False):
        return self.got


def test_a_month_is_whole_only_once_it_has_ended(tmp_path):
    layout = Layout(tmp_path)
    client = OneAnswer(Fetched("ok", json.dumps({"data": [{"symbol": "ABC"}]}).encode()))
    october = (date(2026, 10, 1), date(2026, 10, 31))
    nse_json.pull(layout, client, "corporate_actions", *october, today=date(2026, 10, 31))
    nse_json.pull(layout, client, "corporate_actions", *october, today=date(2026, 11, 2))
    folder = layout.nse("corporate_actions", "equities")
    assert sorted(p.name for p in folder.iterdir()) == ["2026-10.json", "2026-10_01-31.json"]
    assert nse_json.read_shards(layout, "corporate_actions") == [{"symbol": "ABC"}]


def test_a_block_page_never_replaces_a_good_list(tmp_path):
    layout = Layout(tmp_path)
    write_bytes(layout.lists / "symbolchange.csv", b"SYMB,OLD,NEW,DATE\n")
    got = nse_json.pull_lists(layout, OneAnswer(Fetched("ok", b"<html>Access Denied</html>")))
    assert got == {"symbolchange.csv": "error", "namechange.csv": "error", "holidays.json": "error"}
    assert (layout.lists / "symbolchange.csv").read_bytes() == b"SYMB,OLD,NEW,DATE\n"


def test_missing_index_open_stays_missing():
    text = (
        "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        "Closing Index Value\nNIFTY 500,07-09-2026,-,-,-,4000\n"
    )
    rows = indices.parse(text, date(2026, 9, 7))
    assert rows == [
        {
            "date": date(2026, 9, 7), "index_name": "NIFTY 500", "open": None, "high": None,
            "low": None, "close": 4000.0,
        }
    ]  # fmt: skip
    levels = {date(2026, 9, 4): (3900.0, 3950.0), date(2026, 9, 7): (None, 4000.0)}
    assert factors.index_move(levels, date(2026, 9, 4), date(2026, 9, 7), at_open=True) is None


def _stale_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(
            "cm06MAR2012bhav.csv",
            "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,ISIN\n"
            "ABC,EQ,10,12,9,11,11,10,100,1100,05-MAR-2012,INE000A01011\n",
        )
    return buf.getvalue()


def test_cli_exits_nonzero_when_a_step_fails(tmp_path, capsys):
    write_bytes(Layout(tmp_path).bhavcopy / "cm06MAR2012bhav.csv.zip", _stale_zip())
    with pytest.raises(SystemExit) as exited:
        cli.main(["--data", str(tmp_path), "build", "bars"])
    assert exited.value.code == 1
    assert '"failed": ["cm06MAR2012bhav.csv.zip: no valid rows' in capsys.readouterr().out


def test_cli_replay_needs_as_of(tmp_path):
    with pytest.raises(SystemExit, match="needs --as-of"):
        cli.main(["--data", str(tmp_path), "build", "replay"])
