import io
import json
import zipfile
from datetime import date

from nse_noodles import config, doctor
from nse_noodles.http import Fetched

TODAY = date(2026, 10, 26)  # a Monday: the walk back starts on Friday the 23rd
UDIFF = (
    "TradDt,ISIN,SctySrs,TckrSymb,OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal"
)


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("x.csv", text)
    return buf.getvalue()


class Answers:
    """Answers by URL (without its query); any other URL is a 404."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get(self, url, params=None, *, cookies=False):
        self.asked.append(url)
        return self.answers.get(url, Fetched("not_found"))


def test_each_source_is_classified_with_what_to_do():
    cfg = config.parse("[sources.namechange]\nmode = 'manual'\n")
    archives = "https://nsearchives.nseindia.com/content/"
    client = Answers(
        {
            # Friday the 23rd was a holiday (404), Thursday has the file
            archives + "cm/BhavCopy_NSE_CM_0_0_0_20261022_F_0000.csv.zip": Fetched(
                "ok", _zip(f"{UDIFF}\n2026-10-22,INE000A01011,EQ,ABC,10,12,9,11,10,100,1100\n")
            ),
            archives + "historical/EQUITIES/2024/JUL/cm05JUL2024bhav.csv.zip": Fetched(
                "ok",
                _zip("Symbol,Close\nABC,10\n"),  # columns renamed: not a format we read
            ),
            archives + "indices/ind_close_all_23102026.csv": Fetched(
                "ok", b"<html>Access Denied</html>"
            ),
            "https://www.nseindia.com/api/corporates-corporateActions": Fetched(
                "ok", json.dumps([{"symbol": "ABC", "subject": "Bonus", "exDate": "x"}]).encode()
            ),
            "https://www.nseindia.com/api/corporate-announcements": Fetched("ok", b"[]"),
            "https://www.nseindia.com/api/corporate-board-meetings": Fetched("error"),
            "https://www.nseindia.com/api/holiday-master?type=trading": Fetched(
                "ok", json.dumps({"CM": [{"tradingDate": "02-Oct-2026"}]}).encode()
            ),
        }
    )
    probes = doctor.run(client, cfg, TODAY)
    assert [(p.source, p.status) for p in probes] == [
        ("bhavcopy", "ok"),
        ("bhavcopy (old format)", "format_changed"),
        ("indices", "blocked"),
        ("corporate_actions", "ok"),
        ("announcements", "ok"),
        ("board_meetings", "blocked"),
        ("symbolchange", "not_found"),
        ("holidays", "ok"),
        ("namechange", "manual"),
    ]
    assert probes[0].detail.startswith("2026-10-22 ")
    lines = doctor.report(probes)
    assert "not_found (symbolchange):" in lines
    assert "blocked (indices, board_meetings):" in lines
    assert "format_changed (bhavcopy (old format)):" in lines
    assert not any(p.ok for p in probes if p.status in doctor.FIXES)


def test_a_moved_daily_archive_is_not_found_after_a_week_of_404s():
    others = [s for s in config.SOURCES if s != "bhavcopy"]
    cfg = config.parse("".join(f"[sources.{s}]\nmode = 'manual'\n" for s in others))
    client = Answers({})
    probes = doctor.run(client, cfg, TODAY)
    assert [(p.source, p.status) for p in probes][:2] == [
        ("bhavcopy", "not_found"),
        ("bhavcopy (old format)", "not_found"),
    ]
    assert len(client.asked) == doctor.WALK_BACK + 1
    assert probes[0].detail.startswith("2026-10-23..2026-10-15 ")
