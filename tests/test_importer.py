import io
import json
import zipfile
from datetime import date

from nse_noodles import bhavcopy, cli, importer, nse_json
from nse_noodles.layout import Layout, write_bytes

UDIFF = (
    "TradDt,ISIN,SctySrs,TckrSymb,OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal"
)
OLD = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,ISIN"
INDEX = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value"
)


def _udiff(day: str, close: str = "11") -> bytes:
    return f"{UDIFF}\n{day},INE000A01011,EQ,ABC,10,12,9,{close},10,100,1100\n".encode()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buf.getvalue()


def test_a_bare_udiff_csv_is_filed_under_nses_name_and_builds(tmp_path):
    layout = Layout(tmp_path / "data")
    (tmp_path / "bhav.csv").write_bytes(_udiff("2026-10-23"))
    report = importer.run(layout, [tmp_path / "bhav.csv"])
    assert (report.imported, report.rejected) == ({"bhavcopy": 1}, [])
    stored = layout.bhavcopy / "BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip"
    assert bhavcopy.trade_day(stored.read_bytes()) == date(2026, 10, 23)
    assert bhavcopy.build(layout).rows == 1
    log = [json.loads(x) for x in (layout.raw / "imports.jsonl").read_text().splitlines()]
    assert [(r["kind"], r["stored_as"]) for r in log] == [
        ("bhavcopy", "raw/bhavcopy/BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip")
    ]


def test_a_file_named_for_one_day_holding_another_is_refused(tmp_path):
    layout = Layout(tmp_path / "data")
    row = "ABC,EQ,10,12,9,11,11,10,100,1100,05-MAR-2012,INE000A01011\n"
    path = tmp_path / "cm06MAR2012bhav.csv.zip"
    path.write_bytes(_zip({"cm06MAR2012bhav.csv": f"{OLD}\n{row}".encode()}))
    report = importer.run(layout, [path])
    assert report.rejected == [f"{path}: named for 2012-03-06 but its rows are dated 2012-03-05"]
    assert not layout.bhavcopy.exists()


def test_another_file_for_a_day_on_disk_needs_replace_and_resets_its_bars(tmp_path):
    layout = Layout(tmp_path / "data")
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    first.write_bytes(_udiff("2026-10-23"))
    second.write_bytes(_udiff("2026-10-23", close="11.5"))
    importer.run(layout, [first])
    bhavcopy.build(layout)
    again = importer.run(layout, [first])
    assert (again.already, again.imported) == (1, {})
    clash = importer.run(layout, [second])
    assert clash.conflicts == [
        f"{second}: BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip already holds another file "
        "(--replace swaps it)"
    ]
    swapped = importer.run(layout, [second], replace=True)
    assert swapped.replaced == ["BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip"]
    assert not (layout.bars / "date=2026-10-23.parquet").exists()
    bhavcopy.build(layout)
    assert [b["close"] for b in bhavcopy.parse(date(2026, 10, 23), (
        layout.bhavcopy / "BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip").read_bytes()).rows
    ] == [11.5]  # fmt: skip


def test_a_folder_with_a_zip_of_mixed_files_is_sorted_by_content(tmp_path):
    layout = Layout(tmp_path / "data")
    folder = tmp_path / "downloads"
    folder.mkdir()
    actions = [{"symbol": "ABC", "subject": "Bonus 1:1", "exDate": "20-Oct-2026", "isin": "X"}]
    (folder / "bundle.zip").write_bytes(
        _zip(
            {
                "idx.csv": f"{INDEX}\nNifty 50,23-10-2026,100,110,90,105\n".encode(),
                "CF-CA-equities.json": json.dumps({"data": actions}).encode(),
                "__MACOSX/._idx.csv": b"junk",
            }
        )
    )
    (folder / "symbols.csv").write_text(
        "SM_KEY_SYMBOL,SM_NEW_SYMBOL,SM_APPLICABLE_FROM\nOLDCO,NEWCO,01-JAN-2020\n"
    )
    (folder / "notes.txt").write_text("hello")
    report = importer.run(layout, [folder], market="sme")
    assert report.imported == {"indices": 1, "corporate_actions": 1, "symbolchange": 1}
    assert report.rejected == [
        f"{folder / 'notes.txt'}: not a file noodles knows (give --kind if it is one)"
    ]
    assert (layout.indices_raw / "ind_close_all_23102026.csv").exists()
    assert nse_json.read_shards(layout, "corporate_actions", "sme") == actions
    assert (layout.lists / "symbolchange.csv").exists()


def test_imported_rows_already_pulled_count_once(tmp_path):
    layout = Layout(tmp_path / "data")
    row = {"symbol": "ABC", "subject": "Split", "exDate": "20-Oct-2026"}
    write_bytes(
        layout.nse("corporate_actions", "equities") / "2026-10.json", json.dumps([row]).encode()
    )
    (tmp_path / "ca.json").write_text(json.dumps([row, {**row, "symbol": "XYZ"}]))
    importer.run(layout, [tmp_path / "ca.json"])
    assert [r["symbol"] for r in nse_json.read_shards(layout, "corporate_actions")] == [
        "ABC",
        "XYZ",
    ]


def test_another_data_folders_raw_imports_whole(tmp_path):
    source = Layout(tmp_path / "old")
    write_bytes(source.bhavcopy / "x.csv", _udiff("2026-10-23"))
    write_bytes(source.raw / "not_found.json", b'{"bhavcopy": ["2026-10-02"]}')
    importer.run(source, [source.bhavcopy / "x.csv"])  # writes source's imports.jsonl
    report = importer.run(Layout(tmp_path / "new"), [source.raw])
    assert (report.imported, report.already, report.rejected) == ({"bhavcopy": 1}, 1, [])


def test_cli_import_exits_1_when_a_file_is_refused(tmp_path, capsys):
    (tmp_path / "x.json").write_text('[{"unexpected": 1}]')
    code = cli.run(["--data", str(tmp_path / "data"), "import", str(tmp_path / "x.json")])
    assert code == 1
    assert "JSON of no known NSE dataset" in capsys.readouterr().out
