"""A data folder's own settings, `DATA/noodles.toml`: where each source is fetched from, which
sources are downloaded at all, how politely, and the defaults of `run`.

Every key is optional; a missing one keeps its default (`noodles config init` writes the file
with every default, `noodles config show` prints what is in effect). A flag on the command line
wins over the file. The file travels with the data, so a folder kept up to date by a scheduled
`noodles run` keeps its settings.

When NSE moves a file, point its source's `url` at the new place. When NSE blocks or drops a
source, set its `mode = "manual"`: `pull` and `run` then leave it alone and its files come in
with `noodles import`. A `url` is a template with these fields (the day's for a daily file):

    {name}      the file's name in raw/ (NSE's own name: cm05JUL2024bhav.csv.zip, ...)
    {yyyy} {mm} {dd} {MON}     2024, 07, 05, JUL
    {yyyymmdd} {ddmmyyyy}      20240705, 05072024

The JSON sources take no fields: the date range is sent as query parameters, as NSE's site does.

When NSE renames bhavcopy columns but keeps the file's shape, `[bhavcopy_columns]` maps each
field noodles reads to its new header (`open = "OpenPrice"`); the fields not named keep their
UDiFF headers. A file in a new shape still needs a code change.
"""

from __future__ import annotations

import contextlib
import json
import tomllib
from dataclasses import dataclass, field, fields, replace
from datetime import date
from pathlib import Path
from typing import Any

from . import http

FILE_NAME = "noodles.toml"
MODES = ("download", "manual")
FIRST_ISIN_DAY = date(2011, 6, 22)  # the first bhavcopy with ISINs: the history starts there
_ARCHIVES = "https://nsearchives.nseindia.com/content/"
SOURCES: dict[str, dict[str, str]] = {
    # the old CM bhavcopy (to 5 Jul 2024) and UDiFF (from 8 Jul 2024)
    "bhavcopy": {
        "url": _ARCHIVES + "cm/{name}",
        "url_old": _ARCHIVES + "historical/EQUITIES/{yyyy}/{MON}/{name}",
    },
    "indices": {"url": _ARCHIVES + "indices/{name}"},
    "corporate_actions": {"url": "https://www.nseindia.com/api/corporates-corporateActions"},
    "announcements": {"url": "https://www.nseindia.com/api/corporate-announcements"},
    "board_meetings": {"url": "https://www.nseindia.com/api/corporate-board-meetings"},
    "symbolchange": {"url": _ARCHIVES + "equities/symbolchange.csv"},
    "namechange": {"url": _ARCHIVES + "equities/namechange.csv"},
    "holidays": {"url": "https://www.nseindia.com/api/holiday-master?type=trading"},
}
BHAVCOPY_FIELDS = ("date", "isin", "series", "symbol", "open", "high", "low", "close",
                   "prev_close", "volume", "traded_value")  # fmt: skip
_MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
_SAMPLE_DAY = date(2024, 7, 5)


class ConfigError(ValueError):
    """`noodles.toml` names a key, value or template field that does not exist."""


@dataclass(frozen=True)
class Network:
    interval: float = http.DEFAULT_INTERVAL  # seconds between two requests
    tries: int = http.DEFAULT_TRIES
    timeout: float = http.DEFAULT_TIMEOUT
    user_agent: str = http.USER_AGENT
    home: str = http.HOME  # loaded once for NSE's cookies before a JSON request

    def client(self, interval: float | None = None, tries: int | None = None) -> http.NseClient:
        return http.NseClient(
            interval=self.interval if interval is None else interval,
            tries=tries or self.tries,
            timeout=self.timeout,
            user_agent=self.user_agent,
            home=self.home,
        )


@dataclass(frozen=True)
class Run:
    start: date = FIRST_ISIN_DAY  # `from` in the file
    with_announcements: bool = False
    curation: str = ""  # default DATA/curation; relative to the data folder
    holidays: str = ""  # an extra holiday list, one ISO date per line


@dataclass(frozen=True)
class Source:
    url: str
    mode: str = "download"
    url_old: str = ""  # bhavcopy only: the old format's archive


def fill(template: str, day: date | None = None, name: str = "") -> str:
    """A `url` template with the day's fields and the file's name."""
    values = {"name": name}
    if day is not None:
        values |= {
            "yyyy": f"{day:%Y}",
            "mm": f"{day:%m}",
            "dd": f"{day:%d}",
            "MON": _MONTHS[day.month - 1],
            "yyyymmdd": f"{day:%Y%m%d}",
            "ddmmyyyy": f"{day:%d%m%Y}",
        }
    return template.format(**values)


@dataclass(frozen=True)
class Config:
    network: Network = field(default_factory=Network)
    run: Run = field(default_factory=Run)
    sources: dict[str, Source] = field(
        default_factory=lambda: {k: Source(**v) for k, v in SOURCES.items()}
    )
    bhavcopy_columns: dict[str, str] = field(default_factory=dict)  # field -> NSE's new header
    path: Path | None = None  # the file read, None for the defaults

    def url(self, source: str, day: date | None = None, name: str = "", old: bool = False) -> str:
        s = self.sources[source]
        return fill(s.url_old if old else s.url, day, name)

    def manual(self, source: str) -> bool:
        return self.sources[source].mode == "manual"


DEFAULT = Config()


def _check_keys(section: str, given: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(given) - allowed)
    if unknown:
        raise ConfigError(f"[{section}]: unknown key {unknown[0]!r}; known: {sorted(allowed)}")


def _typed(section: str, key: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, int | float) and not isinstance(value, bool) and value > 0
        value = float(value) if ok else value
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
    elif isinstance(default, date):
        if isinstance(value, str):  # `from = "2015-01-01"` as well as `from = 2015-01-01`
            with contextlib.suppress(ValueError):
                value = date.fromisoformat(value)
        ok = isinstance(value, date)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ConfigError(f"[{section}] {key} = {value!r}: expected {type(default).__name__}")
    return value


def _section(name: str, raw: Any, base: Any, rename: dict[str, str] | None = None) -> Any:
    """`base` with the keys `raw` sets (`rename` maps file keys to field names)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table")
    rename = rename or {}
    names = {f.name for f in fields(base)} - {"path"}
    allowed = {k for k, v in rename.items() if v in names} | (names - set(rename.values()))
    _check_keys(name, raw, allowed)
    changes = {}
    for key, value in raw.items():
        attr = rename.get(key, key)
        changes[attr] = _typed(name, key, value, getattr(base, attr))
    return replace(base, **changes)


def _source(name: str, raw: Any, base: Source) -> Source:
    keys = {"url", "mode"} | ({"url_old"} if base.url_old else set())
    if not isinstance(raw, dict):
        raise ConfigError(f"[sources.{name}] must be a table")
    _check_keys(f"sources.{name}", raw, keys)
    got = _section(f"sources.{name}", raw, base)
    if got.mode not in MODES:
        raise ConfigError(f"[sources.{name}] mode = {got.mode!r}: one of {list(MODES)}")
    for key in ("url", "url_old") if base.url_old else ("url",):
        _check_url(f"sources.{name}", key, getattr(got, key))
    return got


def _check_url(section: str, key: str, url: str) -> None:
    if not url.startswith(("https://", "http://")):
        raise ConfigError(f"[{section}] {key} = {url!r}: not an http(s) URL")
    try:
        fill(url, _SAMPLE_DAY, "x")
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(
            f"[{section}] {key} = {url!r}: unknown field {exc}; "
            "use {name} {yyyy} {mm} {dd} {MON} {yyyymmdd} {ddmmyyyy}"
        ) from None


def _columns(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError("[bhavcopy_columns] must be a table")
    _check_keys("bhavcopy_columns", raw, set(BHAVCOPY_FIELDS))
    for key, header in raw.items():
        if not isinstance(header, str) or not header.strip():
            raise ConfigError(f"[bhavcopy_columns] {key} = {header!r}: expected a column header")
    return {k: v.strip() for k, v in raw.items()}


def parse(text: str, path: Path | None = None) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}") from None
    _check_keys("top level", raw, {"network", "run", "sources", "bhavcopy_columns"})
    network = _section("network", raw.get("network", {}), Network())
    _check_url("network", "home", network.home)
    run = _section("run", raw.get("run", {}), Run(), {"from": "start"})
    given = raw.get("sources", {})
    if not isinstance(given, dict):
        raise ConfigError("[sources] must be a table")
    _check_keys("sources", given, set(SOURCES))
    sources = {k: _source(k, given[k], v) if k in given else v for k, v in DEFAULT.sources.items()}
    columns = _columns(raw.get("bhavcopy_columns", {}))
    return Config(network, run, sources, bhavcopy_columns=columns, path=path)


def load(data: Path) -> Config:
    """`DATA/noodles.toml`, or the defaults when there is none."""
    path = data / FILE_NAME
    if not path.exists():
        return DEFAULT
    return parse(path.read_text(encoding="utf-8"), path)


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | date):
        return str(value)
    return json.dumps(value)  # a TOML basic string


def render(cfg: Config) -> str:
    """The settings as a `noodles.toml`, every key written out."""
    run = {"from": cfg.run.start} | {
        f.name: getattr(cfg.run, f.name) for f in fields(cfg.run) if f.name != "start"
    }
    lines = [
        "# noodles settings for this data folder. Delete a key to get its default back.",
        "# mode: download (pull and run fetch it) | manual (add its files with `noodles import`)",
        "",
        "[network]",
        *(f"{f.name} = {_toml(getattr(cfg.network, f.name))}" for f in fields(cfg.network)),
        "",
        "[run]",
        *(f"{k} = {_toml(v)}" for k, v in run.items()),
    ]
    for name, s in cfg.sources.items():
        lines += ["", f"[sources.{name}]", f"mode = {_toml(s.mode)}", f"url = {_toml(s.url)}"]
        if s.url_old:
            lines.append(f"url_old = {_toml(s.url_old)}")
    lines += [
        "",
        '# Only when NSE renames UDiFF columns: field = its new header, e.g. open = "OpenPrice".',
        f"# Fields: {', '.join(BHAVCOPY_FIELDS)}",
        "[bhavcopy_columns]",
        *(f"{k} = {_toml(v)}" for k, v in cfg.bhavcopy_columns.items()),
    ]
    return "\n".join(lines) + "\n"
