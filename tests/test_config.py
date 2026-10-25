from datetime import date

import pytest

from nse_noodles import cli, config
from nse_noodles.config import ConfigError


def test_a_moved_source_is_fetched_from_its_template():
    cfg = config.parse(
        """
[network]
interval = 3
[run]
from = "2020-01-01"
[sources.bhavcopy]
url = "https://mirror.example/{yyyy}/{mm}/{name}"
[sources.announcements]
mode = "manual"
"""
    )
    day = date(2024, 7, 8)
    assert cfg.url("bhavcopy", day, "x.zip") == "https://mirror.example/2024/07/x.zip"
    assert cfg.url("bhavcopy", date(2024, 7, 5), "y.zip", old=True) == (
        "https://nsearchives.nseindia.com/content/historical/EQUITIES/2024/JUL/y.zip"
    )
    assert (cfg.network.interval, cfg.run.start) == (3.0, date(2020, 1, 1))
    assert (cfg.manual("announcements"), cfg.manual("bhavcopy")) == (True, False)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[sources.bhavcopy]\nurl_new = 'https://x'", "unknown key 'url_new'"),
        ("[sources.indices]\nurl_old = 'https://x'", "unknown key 'url_old'"),
        ("[sources.indices]\nmode = 'off'", "one of \\['download', 'manual'\\]"),
        ("[sources.indices]\nurl = 'https://x/{date}'", "unknown field 'date'"),
        ("[sources.indices]\nurl = 'ftp://x'", "not an http\\(s\\) URL"),
        ("[sources.nifty]\nmode = 'manual'", "unknown key 'nifty'"),
        ("[network]\ninterval = 0", "expected float"),
        ("[run]\nfrom = 'soon'", "expected date"),
        ("[run\n", "not valid TOML"),
    ],
)
def test_a_mistake_in_the_file_is_named(text, message):
    with pytest.raises(ConfigError, match=message):
        config.parse(text)


def test_the_written_defaults_read_back_as_the_defaults():
    changed = config.parse("[sources.namechange]\nmode = 'manual'\n[run]\nholidays = 'h.txt'\n")
    again = config.parse(config.render(changed))
    assert (again.manual("namechange"), again.run.holidays) == (True, "h.txt")
    assert again.sources == changed.sources
    assert again.network == changed.network


def test_config_init_then_show_and_a_bad_file_exits_2(tmp_path, capsys):
    assert cli.run(["--data", str(tmp_path), "config", "init"]) == 0
    assert (tmp_path / "noodles.toml").read_text(encoding="utf-8").startswith("# noodles")
    assert cli.run(["--data", str(tmp_path), "config", "init"]) == 1  # never overwritten
    assert cli.run(["--data", str(tmp_path), "config", "show"]) == 0
    assert 'mode = "download"' in capsys.readouterr().out
    (tmp_path / "noodles.toml").write_text("[sources.bhavcopy]\nmode = 'never'\n")
    assert cli.run(["--data", str(tmp_path), "build", "bars"]) == 2
    assert "mode = 'never'" in capsys.readouterr().err


def test_a_manual_source_is_never_pulled(tmp_path, capsys, monkeypatch):
    (tmp_path / "noodles.toml").write_text("[sources.bhavcopy]\nmode = 'manual'\n")
    monkeypatch.setenv("NOODLES_DATA", str(tmp_path))
    assert cli.run(["pull", "bhavcopy", "--from", "2026-10-01", "--to", "2026-10-02"]) == 0
    assert 'bhavcopy: {"mode": "manual: add its files with `noodles import`"}' in (
        capsys.readouterr().out
    )
    assert not (tmp_path / "raw").exists()
