# Outputs

Everything lives under the data directory you pass with `--data`. Keep that directory outside
any git repository: the data belongs to NSE and must not be committed or redistributed.

## The data directory

| Path | What | Rebuilt? |
|---|---|---|
| `raw/bhavcopy/` | Bhavcopy zips as downloaded | Never rewritten: the source of truth |
| `raw/indices/` | `ind_close_all_DDMMYYYY.csv` index archives | Never rewritten |
| `raw/nse/<dataset>/<index>/` | NSE JSON feeds, one file per month (`YYYY-MM.json`). A month that had not ended when it was fetched is saved as `YYYY-MM_DD-DD.json` and fetched again on the next pull | Never rewritten |
| `raw/lists/` | `symbolchange.csv`, `namechange.csv`, `holidays.json` (NSE's current-year holidays) | Replaced by each `pull lists` |
| `raw/not_found.json` | Days the archive answered 404 for, so a resume does not ask again (`--recheck` asks again). A 404 for a day within 5 days of today is not remembered, because NSE may not have published it yet | Updated by pulls |
| `ledger/factor_versions.jsonl` | Every factor version ever produced, appended and never rewritten. It is the only record of `first_observed_at`. **Back it up with `raw/`: no rebuild can recreate it** | Append-only |
| `bars/date=YYYY-MM-DD.parquet` | Parsed bhavcopy, one row per ISIN and day | `build bars` |
| `index/date=YYYY-MM-DD.parquet` | Parsed index levels | `build bars` |
| `noodles.duckdb` | Corporate actions, lineage, factor versions, review lists ([tables](#tables-in-noodlesduckdb)) | `build ca-db`, `build actions` |
| `out/` | The published series and reports (below) | `build hindsight`, `build replay`, `build reports` |
| `curation/` | Your input: research, corrections, lineage overrides, holidays ([curation](curation.md)). Default location; `--curation DIR` moves it | Never written by the code |

Every file except the ledger is written to a temporary name and then renamed, so an
interrupted run never leaves half a file.

## Files in `out/`

| File | Written by | Columns |
|---|---|---|
| `hindsight_daily.parquet` | `build hindsight` | [Series columns](#series-columns) |
| `instruments.parquet` | `build hindsight` | [Instrument columns](#instrument-columns) |
| `replay_daily_asof=D.parquet` | `build replay --as-of D` | [Series columns](#series-columns), with `factor_pending` always false |
| `replay_instruments_asof=D.parquet` | `build replay --as-of D` | [Instrument columns](#instrument-columns) |
| `demerger_exits.csv` | `build reports` | [Exit calendar columns](#exit-calendar-columns) |
| `demerger_exits_asof=D.csv` | `build replay --as-of D` | [Exit calendar columns](#exit-calendar-columns) |
| `jumps.csv` | `build reports` | [Jump columns](#jump-columns) |
| `review.csv` | `build reports` | [Review columns](#review-columns) |
| `adjustment_log.csv` | `build reports` | [Adjustment log columns](#adjustment-log-columns) |
| `factor_versions.csv` | `build reports` | [Factor version columns](#factor-version-columns) |
| `coverage.json` | `build reports` | [Coverage](#coverage) |

`build reports` reads `hindsight_daily.parquet`, so run `build hindsight` first.

### Series columns

One row per chain and day, sorted by `chain_isin, date`.

| Column | Meaning |
|---|---|
| `chain_isin` | The company's chain: its latest ISIN across linked ISIN changes ([glossary](glossary.md)) |
| `date, isin, symbol, series, open, high, low, close, prev_close, volume, traded_value` | The raw bar, unchanged ([bar columns](#bar-columns)) |
| `price_factor` | Product of the price factors (split, bonus, rights, demerger, consolidation) with an ex-date after `date` |
| `div_factor` | Product of the dividend factors 1 − D / previous raw close with an ex-date after `date` |
| `adj_open, adj_high, adj_low, adj_close` | Raw price × `price_factor`: the **price basis** |
| `adj_volume` | `volume / price_factor` |
| `tr_close` | `close × price_factor × div_factor`: the **total-return** close |
| `ordinary_equity` | True when the ISIN is `INE…`/`IN9…` with security type `01` |
| `factor_pending` | Hindsight only: true from a price event's ex-date until the first of its versions was available (or on every bar from the ex-date on when none has a known time). On those bars the hindsight series hides a gap that a trader then would have seen |

How to use the two bases is in [backtesting](backtesting.md).

### Bar columns

`bars/date=YYYY-MM-DD.parquet`: `date, isin, symbol, series, open, high, low, close, prev_close,
volume, traded_value`. Each day keeps one row per ISIN: series `EQ`, else `BE`, else `BZ`, else
one of the block windows `BL, BT, IL, IQ`. Other series (debt, SME, and so on) are left out. A
row with a non-positive price, or high/low that do not bracket open and close, is counted
(`invalid_rows`) and left out, never repaired. `prev_close` is empty when NSE gave 0.

`index/date=YYYY-MM-DD.parquet`: `date, index_name, open, high, low, close` for four indices
only: NIFTY 50, NIFTY 500, NIFTY SMALLCAP 250, INDIA VIX (older names of the first two, such as
`CNX 500`, are read as today's). `raw/indices/` keeps every index.

### Instrument columns

One row per chain: `chain_isin, isins` (comma-separated), `last_symbol, symbols`
(comma-separated), `first_day, last_day, ordinary_equity` (of the last bar), `stopped_trading`
(the last bar is more than 20 sessions before the data's last session).

### Exit calendar columns

`chain_isin, isin, symbol, ex_date, record_date, announced_on, notice_source, notice_url,
exit_session, exitable, reason`. There is one row per demerger event of NSE's feed (an ISIN and
ex-date), and `reason` is empty when the position is exitable. The `asof=D` file lists only
demergers announced by D, using bars and ISIN links known by D. See
[backtesting](backtesting.md#demergers-and-the-exit-calendar) for the rule and the reasons.

### Jump columns

`chain_isin, isin, symbol, date, adj_open, adj_close, ordinary_equity, previous_close,
previous_day, ratio, security, feed_near, review_near`, newest first.

- A row is a session where `|adj_open / previous_close − 1| > 0.25`, with `previous_close` the
  previous **adjusted** close of the same chain no more than 14 calendar days earlier. A longer
  gap (a relisting after a long suspension) is never reported.
- `ratio` = `adj_open / previous_close`.
- `security`: `fund_unit` (ISIN starts `INF`), `rights_entitlement` (security type `20`),
  `equity` (ordinary equity), else `other`.
- `feed_near`: the purpose texts of NSE feed rows for the same symbol or ISIN with an ex-date
  within 7 days, joined by ` | `.
- `review_near`: the **reasons** (not rows) of review rows for the same symbol or ISIN within 7
  days, comma-separated. Look the rows up in `review.csv` or the `review` table.

### Review columns

`list, table, symbol, isin, day, reason, text, hint, evidence`, newest `day` first.

- `list` = `actions`: a corporate-action row that was not accepted. `table` is the database
  table it came from: `splits_bonus`, `rights_issues`, `demergers`, `bonus_instruments`,
  `dividends`. `day` is NSE's ex-date. `text` is the purpose text, `hint` a parse hint, and
  `evidence` JSON (bars, gap, checks).
- `list` = `lineage`: an ISIN succession waiting for a verdict. `table` is `lineage`, `isin` is
  `OLD>NEW`, `symbol` is the new ISIN's first symbol, and `day` is the new ISIN's first day
  (`0001-01-01` for an `override_unused` row with nothing to date it). `evidence` is JSON.

Skipped rows (not errors, e.g. a dividend on a non-equity ISIN) are only in the `skipped`
table. What each reason means and what to do: [review reasons](review-reasons.md).

### Adjustment log columns

Every event at its hindsight version, ordered by `ex_date`: `chain_isin, isin, symbol,
action_type, version, factor, cash, basis, method, source, announced_on, notice_url, ex_date,
source_published_at, first_observed_at, available_at, available_basis, text, lag_sessions`.
`lag_sessions` is the number of sessions from the ex-date to the session of `available_at` (0 =
available on the ex-date itself, empty = unknown).

### Factor version columns

Every version of every event: `chain_isin, isin, symbol, ex_date, action_type, version, status,
value, cash, basis, raw_gap, band_low, band_high, source_published_at, first_observed_at,
available_at, available_basis, method, version_id`. `status` is `accepted` or `disputed` (a
disputed version is never used). `basis` and `available_basis` are explained in
[method](method.md#point-in-time).

### Coverage

`coverage.json` maps each list to ISO dates, from the first to the last session of the bars:

| Key | Weekdays (or days) that are… |
|---|---|
| `holiday` | 404 from the archive and named by a holiday list: explained |
| `gap` | 404 from the archive and named by no holiday list. Add the holiday to `holidays.txt`, or ask again with `pull bhavcopy --recheck` |
| `unaccounted` | never fetched, or the request failed: rerun `pull bhavcopy` for the range |
| `on_listed_holiday` | a holiday list names it but a bhavcopy exists: the list is wrong |
| `weekend_session` | Saturday or Sunday sessions that have a bhavcopy (Budget, Muhurat) |
| `index_missing` | sessions from the first index day on with no index file: rerun `pull indices` |

Holiday lists: `holidays.txt` in the curation folder, the file given with `--holidays`, and
`raw/lists/holidays.json`.

## Tables in `noodles.duckdb`

| Table | Columns |
|---|---|
| `ca_feed` | `symbol, company, series, isin, subject, ex_date, record_date, face_value, market` (`market` = `equities` or `sme`; `subject` is NSE's purpose text) |
| `splits_bonus` | event columns + `legs, parse_status, instrument_class, classification_source, evidence, evidence_url, announced_on, notice_url`. `legs` reads like `bonus=1/2;split=1/5`; `instrument_class` is only a hint from the text or a nearby announcement |
| `rights_issues` | event columns + `face_value, ratio_new, ratio_held, issue_price, parse_status, announced_on, notice_url` |
| `demergers` | event columns + `company, notice_source, announced_on, notice_url` (the record-date notice; ratios come from research) |
| `bonus_instruments` | event columns + `face_value, announced_on, notice_url` (values come from research) |
| `dividends` | `isin, symbol, series, ex_date, amount, kinds, status, subjects, market, announced_on, notice_url` (one row per ISIN and ex-date) |
| `announcements` | `symbol, company, isin, announced_at, disseminated_at, category, text, file` |
| `board_meetings` | `symbol, company, isin, meeting_date, purpose, description, intimated_at` |
| `lineage` | `old_isin, new_isin, effective, status` (`linked`/`review`)`, reason, evidence, known_on` |
| `chain_map` | `isin, chain_isin, depth` |
| `factor_versions` | `version_id, chain_isin, isin, symbol, ex_date, action_type, version, status, value, cash, basis, raw_gap, band_low, band_high, method, source, text, evidence, announced_on, notice_url, source_published_at, first_observed_at, available_at, available_basis` |
| `actions` | The hindsight choice: per event, the accepted version of highest precedence (same columns) |
| `review`, `skipped` | `table, symbol, isin, ex_date, reason, text, hint, evidence` |
| `corrections_log` | `line, op, isin, ex_date, type, status, note` (`status`: `applied`, `exists_use_replace`, `not_found`, `destination_exists`) |

"Event columns" are `symbol, isin, series, ex_date, record_date, market, subject`. `build
ca-db` always creates every table, empty when its source was not pulled. `announced_on` is the
first NSE filing found for the event ([method rule 36](method.md#point-in-time)).

To query a table (the same line works in bash and PowerShell; run it from the repository):

```bash
uv run python -X utf8 -c "import duckdb; con = duckdb.connect('DATA/noodles.duckdb', read_only=True); con.sql('SELECT * FROM review WHERE symbol = ?', params=['HEG']).show()"
```

Replace `DATA` with your data directory, and the table and filter as needed. `-X utf8` keeps the
table borders printable on a Windows console. Close the query before running a build: a build
needs write access to the file.
