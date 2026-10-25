# Command line

The command is `noodles`. Run it as `uv run noodles ...` from the repository, or as plain
`noodles` with the project's virtual environment activated ([getting started](getting-started.md)).

```text
noodles [--data DIR] [--interval SECONDS] [-v] {pull,build,run} ...
noodles                          (no arguments: the guided menu)
```

## Global options

These go **before** the subcommand: `noodles --data D pull bhavcopy`, not
`noodles pull bhavcopy --data D`.

| Option | Meaning |
|---|---|
| `--data DIR` | Required. The data directory. Keep it outside any git repository. `~` is expanded and the directory is created if needed |
| `--interval SECONDS` | Seconds between two requests to NSE. Default 2. Do not go lower |
| `-v`, `--verbose` | Log each retry and warning |

## The guided menu

`noodles` with no arguments asks for a data directory, shows what it already holds (zips, bar
days, index days, NSE month files, lists, database, outputs, review rows), and offers:

1. Run everything for a date range (`run`)
2. Pull one source (`pull ...`)
3. Build one step (`build ...`)
4. Point-in-time replay as of a day (`build replay --as-of`)
5. Show what the data directory holds
6. Quit

It asks only for what the command needs, each answer with a default in brackets (Enter takes
it). It then prints the equivalent command line and asks before running it. Type `q` at any
prompt to go back. The default data directory is `$NOODLES_DATA`, else `~/nse-data`. The menu
refuses a directory inside a git repository. When bars already exist, it offers to start from
the last bar day, which resumes. The menu never passes `--curation` or `--holidays`, so with a
curation folder outside `DATA/curation` use the flags directly.

## pull

Downloads only, from NSE, at the paced interval. Each pull resumes: what is already on disk is
skipped. Never run two pulls at once.

| Command | Options | What it fetches |
|---|---|---|
| `pull bhavcopy` | `--from`, `--to`, `--extra-days D [D ...]`, `--recheck` | One bhavcopy zip per weekday in the range, plus `--extra-days` (weekend sessions; days outside the range are ignored). A 404 is remembered in `raw/not_found.json` and not asked again unless `--recheck`. A reply that is not a readable zip is an error and is asked again on the next run |
| `pull indices` | `--from`, `--to`, `--recheck` | The index archive for each day **that already has a bar file** in the range. Run `build bars` first |
| `pull nse` | `--from`, `--to`, `--dataset NAME [NAME ...]`, `--index equities\|sme [...]`, `--refresh-current` | NSE JSON feeds, month by month. `--dataset`: `corporate_actions` (default), `announcements` (fetched a week at a time, slow), `board_meetings`. `--index`: `equities` (default), `sme` |
| `pull lists` | none | `symbolchange.csv`, `namechange.csv` and the current year's `holidays.json`. A reply that is not a CSV or JSON (a block page) never replaces a good file |

`--from` defaults to 2011-06-22 (the first bhavcopy with ISINs) and `--to` to today. Dates are
`YYYY-MM-DD`.

`pull nse` skips a month only when its file exists and the month had ended when it was fetched.
A month still in progress, or a range that starts or ends inside a month, is saved as
`YYYY-MM_DD-DD.json` and fetched again on the next pull. A whole-month file wins over a partial
one when the feed is read. `--refresh-current` is accepted but changes nothing: the month in
progress is fetched again on every pull anyway.

Requests: every request sends a browser User-Agent and Referer, and before the website JSON
endpoints (`pull nse`, the holiday list) the client loads NSE's home page for cookies. It waits `--interval` seconds between requests and tries each one 5 times, backing off
(2 s, 4 s, ...) and starting a fresh session after the second failure.

## build

`noodles --data D build STEP [--curation DIR] [--rebuild] [--holidays FILE] [--as-of D]`.
Builds read only local files. Run the steps in this order:

| Step | Reads | Writes |
|---|---|---|
| `bars` | `raw/bhavcopy/`, `raw/indices/` | `bars/` (new days only; `--rebuild` re-parses every zip), `index/` (new days only) |
| `ca-db` | `raw/nse/` | The corporate-action tables in `noodles.duckdb` (every table, empty when not pulled) |
| `actions` | bars, index, database, `raw/lists/`, the curation folder | `lineage`, `chain_map`, `factor_versions`, `actions`, `review`, `skipped`, `corrections_log`; appends to `ledger/factor_versions.jsonl` |
| `hindsight` | bars, database | `out/hindsight_daily.parquet`, `out/instruments.parquet` |
| `reports` | the above, holiday lists | `out/jumps.csv`, `review.csv`, `adjustment_log.csv`, `factor_versions.csv`, `demerger_exits.csv`, `coverage.json` |
| `replay --as-of D` | bars, database, holiday lists | `out/replay_daily_asof=D.parquet`, `out/replay_instruments_asof=D.parquet`, `out/demerger_exits_asof=D.csv` |

| Option | Used by | Meaning |
|---|---|---|
| `--curation DIR` | `actions`, `reports`, `replay` | The curation folder ([curation](curation.md)). Default `DATA/curation` |
| `--rebuild` | `bars` | Re-parse every zip, not only new ones |
| `--holidays FILE` | `reports`, `replay` | An extra holiday list, one ISO date per line, added to `holidays.txt` and NSE's current list |
| `--as-of D` | `replay` | Required for `replay`: the series as it could have been built after the close of D |

After a curation change, rerun `actions`, `hindsight`, `reports`, and `replay --as-of D` for
every replay you use (a replay file keeps the versions of the build that wrote it).

## run

`noodles --data D run [--from] [--to] [--curation DIR] [--holidays FILE]
[--with-announcements]`, in this order:

1. `pull bhavcopy` for the range
2. `build bars`
3. `pull indices`
4. `pull nse --dataset corporate_actions --index equities sme` (plus `announcements` and
   `board_meetings` with `--with-announcements`)
5. `pull lists`
6. `build bars`, `ca-db`, `actions`, `hindsight`, `reports`

It never builds a replay: run `build replay --as-of D` afterwards. The builds run even when a
pull failed, so you get the counts. The exit code still reports the failure.

## Exit codes

| Code | When |
|---|---|
| 0 | Everything finished |
| 1 | A pull reported `errors` or `failed` months, a list was not `ok`, or `build bars` reported `failed` files (for `run`, any of these). The printed counts say which |
| 1 | A step stopped with an error (a Python traceback), e.g. a bad curation line, which names its file and line. Also `build replay` without `--as-of` |
| 2 | The command line could not be parsed (unknown option, `--data` missing or placed after the subcommand) |

The menu ends with the exit code of the last command it ran (0 if none, or when input ends).
Ctrl-C in the menu exits with 130.

## What each command prints

Each step prints one line, `title: {json}`.

| Line | Keys |
|---|---|
| `bhavcopy` | `downloaded, already, not_found, skipped_known_404, errors` (dates to retry) |
| `indices` | `downloaded, already, not_found, errors` |
| `<dataset>/<index>` | `months_written, months_skipped, rows, failed` (date ranges to retry) |
| `lists` | each file → `ok`, `not_found` or `error` |
| `bars` | `days, rows, already, invalid_rows, wrong_date_rows, failed` (a zip with no valid row fails) |
| `index` | `days` parsed |
| `ca-db` | rows per table |
| `actions` | `lineage` (`status:reason` counts), `events` (by type), `versions` (by `available_basis`), `disputed`, `available_at_unknown`, `withdrawn_from_ledger`, `review` (by reason), `skipped` (by reason), `corrections` (by status) |
| `hindsight`, `replay as of D` | `rows, chains, stopped_trading, last_session, factor_pending_rows, events_used, price_events_used` |
| `demerger exits as of D` | `events, exitable`, `not_exitable:<reason>` counts |
| `reports` | `jumps`, `jumps_by_security`, `review`, `coverage` (counts per list), `adjustment_log` (`rows, by_available_basis, available_after_ex_date, available_at_unknown, announced_on_found`), `demerger_exits`, `factor_versions` (`rows, events_with_several, disputed`) |

- `available_after_ex_date` counts events whose version became available after 10:00 IST on the
  ex-date: a backtest trading that open could not have had it.
- `withdrawn_from_ledger` counts ledger versions that this build no longer produces (a research
  row edited or removed, for example).

What to look for in these counts: [getting started](getting-started.md#check-that-it-worked).
