# Keeping the data up to date

Once a data folder is built, three commands keep it current and keep working when NSE changes
something: a scheduled `run`, `doctor` when a pull fails, and `import` when downloading stops
working. Their settings live in the data folder, in `noodles.toml`.

## Update on a schedule

`run` skips everything already on disk, so rerunning it is the update. Write the start of your
history in the settings once, so a plain `run` never reaches further back:

```bash
noodles --data ~/nse-data config init          # writes ~/nse-data/noodles.toml
# edit [run] from = 2018-01-01 (or wherever your history starts)
noodles --data ~/nse-data run                  # from [run] from to today
```

Without `[run] from`, `run` starts at 2011-06-22 and downloads every day not yet on disk. Set
`NOODLES_DATA` and you can leave out `--data`.

Schedule it after NSE posts the day's files (evenings, IST):

```bash
# cron (Linux, macOS): weekdays at 21:00
0 21 * * 1-5  NOODLES_DATA=$HOME/nse-data  $HOME/.local/bin/noodles run >> $HOME/nse-data/run.log 2>&1
```

```powershell
# Windows Task Scheduler: weekdays at 21:00
schtasks /Create /TN noodles /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 21:00 `
  /TR "cmd /c noodles --data %USERPROFILE%\nse-data run >> %USERPROFILE%\nse-data\run.log 2>&1"
```

`noodles` on the path means an installed tool: `uv tool install git+https://github.com/abhij1306/NSE-noodles`.
From a clone, use `uv run --directory PATH_TO_CLONE noodles` instead. The exit code is 1 when
something failed ([exit codes](cli.md#exit-codes)), so a scheduler can alert on it. A day NSE
has not posted yet is asked again on later runs for 5 days.

## When a pull fails: `doctor`

`errors` in the `bhavcopy` or `indices` counts, or `failed` months in a feed, are usually
transient: the next run asks again. When they keep coming, ask each source once:

```bash
noodles --data ~/nse-data doctor
```

```text
sources (default settings, no noodles.toml):
  bhavcopy               ok              2026-10-23 https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip
  bhavcopy (old format)  ok              2024-07-05 https://nsearchives.nseindia.com/content/historical/EQUITIES/2024/JUL/cm05JUL2024bhav.csv.zip
  indices                not_found       2026-10-23..2026-10-15 https://nsearchives.nseindia.com/content/indices/ind_close_all_23102026.csv
  ...
not_found (indices):
  NSE answered 404: the file has likely moved. ...
```

About a dozen requests, at the usual pace. Each source comes back as one of these statuses:

| Status | Meaning | What to do |
|---|---|---|
| `ok` | Answered in the format noodles reads | Nothing |
| `not_found` | 404 for a week of days that should have files: NSE moved it | Find the new address on nseindia.com and set the source's `url` (below), or switch the source to `manual` and import |
| `blocked` | No answer after retries, an HTTP error, or a web page instead of data | Try later, or raise `[network] interval`; or download in a browser and import |
| `format_changed` | Data, but not in a format noodles reads | The parser needs a code change: open an issue with the URL. `import` takes files in the known formats meanwhile |
| `manual` | `mode = "manual"`: not asked | — |

The exit code is 1 when any source is not `ok` or `manual`.

## Settings: `noodles.toml`

`noodles --data D config init` writes the file with every default. `config show` prints what is
in effect. Delete a key to get its default back. A flag on the command line wins over the file.
A mistake (an unknown key, a bad mode, a template field that does not exist) stops every command
with exit code 2 and names the key.

```toml
[network]
interval = 2.0          # seconds between requests; do not go lower
tries = 5
timeout = 40.0
user_agent = "Mozilla/5.0 ..."
home = "https://www.nseindia.com"   # loaded once for cookies before a JSON request

[run]
from = 2011-06-22       # where `run` starts without --from
with_announcements = false
curation = ""           # default DATA/curation; a relative path is under the data folder
holidays = ""           # an extra holiday list, one ISO date per line

[sources.bhavcopy]
mode = "download"       # or "manual": pull and run leave it alone; files come from import
url = "https://nsearchives.nseindia.com/content/cm/{name}"
url_old = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{yyyy}/{MON}/{name}"

[sources.indices]
mode = "download"
url = "https://nsearchives.nseindia.com/content/indices/{name}"

# also: corporate_actions, announcements, board_meetings, symbolchange, namechange, holidays
```

A daily source's `url` is a template:

| Field | Example (5 Jul 2024) |
|---|---|
| `{name}` | the file's name in `raw/`: `cm05JUL2024bhav.csv.zip`, `BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip`, `ind_close_all_05072024.csv` |
| `{yyyy}` `{mm}` `{dd}` `{MON}` | `2024` `07` `05` `JUL` |
| `{yyyymmdd}` `{ddmmyyyy}` | `20240705` `05072024` |

The JSON feeds take no fields: noodles adds `index`, `from_date` and `to_date` as query
parameters, as NSE's website does. Files are always saved under their name in `raw/`, whatever
the URL, so the build reads them the same.

## Files you download yourself: `import`

When downloading does not work, or you have the files from elsewhere, add them to `raw/` as a
pull would:

```bash
noodles --data ~/nse-data import ~/Downloads/bhavcopies/ ~/Downloads/ca.json
noodles --data ~/nse-data build bars             # or run: the builds read them like downloads
```

Each file is recognised by its content, not its name:

| Kind | Accepts | Saved as |
|---|---|---|
| `bhavcopy` | One day's bhavcopy, zipped or a bare CSV, old format or UDiFF | `raw/bhavcopy/<NSE's zip name>` |
| `indices` | NSE's daily `ind_close_all_DDMMYYYY.csv` | `raw/indices/` |
| `corporate_actions`, `announcements`, `board_meetings` | The JSON NSE's website answers: a list of rows, or `{"data": [...]}` | `raw/nse/<dataset>/<market>/import-<hash>.json` |
| `symbolchange`, `namechange`, `holidays` | NSE's `symbolchange.csv`, `namechange.csv`, holiday JSON | `raw/lists/` |

- Only files under your home folder or the current folder are read, so an agent running
  `import` cannot be steered to system files. For files elsewhere (another drive), run
  `noodles` from their folder: `cd D:\downloads` then `noodles --data ... import .`.
- A folder is read through. A zip that is not itself a bhavcopy is opened and every file in it
  imported, so a month of bhavcopies in one zip works.
- A file whose name gives a day (NSE's names do) must hold that day's rows, else it is refused.
- A day already on disk with different bytes is a conflict and left alone. `--replace` swaps
  it, and deletes that day's parsed file so the next `build bars` reads the new one.
- A list replaces the one on disk (a list is a snapshot). JSON rows only add: a row already on
  disk counts once.
- `--kind K` reads every file as `K` (a header-less `symbolchange.csv` needs it).
  `--market sme` files JSON feeds under SME (default `equities`).
- Every file written is logged in `raw/imports.jsonl` with where it came from and its sha256.

The exit code is 1 when a file was refused or conflicted. The `import` line says which and why.

**Getting NSE's JSON in a browser.** Open `https://www.nseindia.com` first, so the browser holds
NSE's cookies. Then open, for example,
`https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=01-10-2026&to_date=31-10-2026`
and save the page as a `.json` file. The CSV download on NSE's corporate-actions page is not
accepted: it has no ISIN column.

## What settings and import cannot fix

A moved file is a settings change. A blocked source is fixed by importing. A **new file format**
needs code: in July 2024 NSE replaced the old bhavcopy with UDiFF, which needed a new parser.
noodles reads a file's format from its header. A file in neither known format fails `build
bars` (`not a known bhavcopy format`), and `doctor` shows `format_changed`. Nothing is parsed by
guesswork.
