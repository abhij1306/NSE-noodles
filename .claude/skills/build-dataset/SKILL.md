---
name: build-dataset
description: Run the NSE-noodles pipeline end to end (pull NSE bhavcopies, indices, corporate actions and lists, then build bars, the corporate-action DB, lineage and factor versions, the hindsight series, the reports and point-in-time replays), read the printed counts, and route what is left to research, triage or lineage review. Use when asked to build, refresh, extend, resume or check the NSE dataset, or when a noodles command failed.
---

# Build the dataset

The CLI is `noodles`, run as `uv run noodles` from the repository root. Every step is resumable
and idempotent. Reference: `docs/cli.md` (commands, printed keys, exit codes) and
`docs/outputs.md` (files).

## Rules

- `--data D` must be outside any git repository. Never commit or paste data (prices, CA rows,
  research rows) into the repo, an issue or a PR.
- `--data`, `--interval` and `-v` go **before** the subcommand: `uv run noodles --data D pull
  bhavcopy ...`.
- One pull at a time, at the default pace. Never pass `--interval` below 2. NSE's terms apply.
- Long pulls: run them in the background and wait for them to finish; do not poll in a loop.
- Never read `.env` or credentials; none are needed.
- Report the counts verbatim. Never claim a step passed without its printed output and exit
  code (1 = a pull or `build bars` failed, or a step raised).

## Step 0: check the setup

```bash
uv sync                                  # once per clone
uv run noodles --help                    # must print the usage
git -C D rev-parse --show-toplevel       # must FAIL ("not a git repository")
```

If `D/curation` is not the curation folder, pass `--curation DIR` to `build` and `run`.

## Step 1: pull

```bash
uv run noodles --data D pull bhavcopy --from 2011-06-22 [--to YYYY-MM-DD]
uv run noodles --data D build bars          # pull indices needs the bar days
uv run noodles --data D pull indices --from 2011-06-22
uv run noodles --data D pull nse --dataset corporate_actions --index equities sme --from 2011-06-22
uv run noodles --data D pull lists
# optional, slow: dates each event's notice (announced_on); the demerger exit calendar needs it
uv run noodles --data D pull nse --dataset announcements board_meetings --index equities sme --from 2011-06-22
```

Or everything in order: `uv run noodles --data D run --from 2011-06-22 [--with-announcements]`.
Weekend sessions: `pull bhavcopy --extra-days YYYY-MM-DD ...`.

## Step 2: build

```bash
for s in bars ca-db actions hindsight reports; do uv run noodles --data D build $s; done
```

PowerShell: `foreach ($s in "bars","ca-db","actions","hindsight","reports") { uv run noodles --data D build $s }`.

For a backtest, also `uv run noodles --data D build replay --as-of YYYY-MM-DD` for each cutoff.
Keep `D/ledger/` with `D/raw/`: it is append-only and no rebuild recreates it.

A step that stops with `CurationError: <file>:<line>` points to a bad curation line: fix that
line, then rerun from `build actions`.

## Step 3: read the counts

| Count | Action |
|---|---|
| `bhavcopy.errors`, `indices.errors`, `<dataset>/<index>.failed` | Transient: rerun the same pull |
| `lists` not all `ok` | Rerun `pull lists` |
| `bars.failed` | A zip with no valid rows. Report it. Do not delete raw files without the user's go-ahead |
| `bars.invalid_rows`, `bars.wrong_date_rows` | Informational; report large numbers |
| `reports.coverage.unaccounted` | Days never fetched: rerun `pull bhavcopy` for that range |
| `reports.coverage.gap` | Weekdays with no file and no holiday listed. Ask the user for true past holidays for `curation/holidays.txt` (from NSE circulars), or retry with `pull bhavcopy --recheck` |
| `reports.coverage.index_missing` | Rerun `pull indices` |
| `actions.review` | Route by reason (below) |
| `actions.lineage` entries starting `review:` | The `review-lineage` skill |
| `actions.corrections` other than `applied` | `exists_use_replace`: use `replace`; `not_found`: wrong ISIN/date/type or the event was never accepted (use `add`); `destination_exists`: fix the target date first |
| `reports.jumps_by_security.equity` (and `other`) | The `triage-jumps` skill. `rights_entitlement` and `fund_unit` jumps are expected |
| `actions.available_at_unknown`, `reports.adjustment_log.available_at_unknown` | Versions that never enter a replay: research or corrections without a source date. Add the date the source shows, never an earlier one |
| `reports.adjustment_log.available_after_ex_date` | Events whose version became usable only after 10:00 on the ex-date. Informational |
| `reports.demerger_exits` `not_exitable:announced_on_unknown` | No pulled filing writes the record date: pull announcements and board meetings for the range, or accept that those positions hold through |
| `actions.disputed` | A demerger's ratio version is more than 10% (log) from its gap version: see `demerger_versions_disagree` |
| `actions.withdrawn_from_ledger` | Versions an earlier build produced and this one did not (a research row edited or removed). Report it; fixes go through `corrections.tsv` |

Routing `actions.review` reasons (full list: `docs/review-reasons.md`):

| Reasons | Go to |
|---|---|
| `needs_research:*`, `gap_disagrees_*`, `no_cross_check_*`, `no_drop`, `demerger_versions_disagree`, `date_disputed`, `low_confidence` | `research-corporate-action` (demerger) |
| `no_issue_price`, `non_simple`, `unparsed` on `rights_issues`, `low_confidence` | `research-corporate-action` (rights) |
| `needs_value`, `some_holders_only` | `research-corporate-action` (bonus instrument) |
| `ambiguous` (mostly bonuses to public holders only) | `research-corporate-action` (public-only bonus: a correction) |
| `off_factor`, `conflicting_rows`, `unparsed` on `splits_bonus`, `research_says_row_wrong` | Find the filing; a correction line (`docs/curation.md`) |
| `several_amounts`, `percent_of_face_value`, `no_amount`, `breakdown_disagrees`, `rows_disagree`, `amount_vs_price` | Read the dividend filing; a `dividend` correction if it should count |
| `no_bars`, `no_instrument` | Check suspension or symbol change; report |

## Step 4: after curation changes

After research CSVs, corrections or lineage overrides change, rerun `build actions`,
`build hindsight`, `build reports`, and `build replay --as-of D` for every replay in use (a
replay file keeps the versions of the build that wrote it). After `holidays.txt` changes, rerun
`build reports` (and the replays).

## Done when

- No pull errors; `bars.failed` empty; `coverage.gap` and `coverage.unaccounted` empty or
  explained.
- Every `review` row is researched, corrected or knowingly left, with a note.
- Every equity jump in `out/jumps.csv` is triaged.
