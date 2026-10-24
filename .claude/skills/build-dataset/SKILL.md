---
name: build-dataset
description: Run the NSE-noodles pipeline end to end (pull NSE bhavcopies, indices, corporate actions and lists, then build bars, the corporate-action DB, lineage and factor versions, the hindsight series and the reports), read the counts, and decide what to rerun or hand to research. Use when asked to build, refresh, extend or check the NSE dataset.
---

# Build the dataset

The CLI is `noodles` (`pip install -e .` in this repo). Every step is resumable and idempotent.

## Rules

- `--data DIR` must be outside any git repository. Never commit or paste data (prices, CA rows,
  research rows) into the repo, an issue or a PR.
- One pull at a time, default pace (2 s). Never lower `--interval` below 2. NSE's data and
  terms apply.
- Long pulls: run them in the background and wait for completion; do not poll in a loop.
- Never read `.env` or credentials; none are needed.

## Steps

1. Pull (each resumes where it stopped):
   ```bash
   noodles --data D pull bhavcopy --from 2011-06-22 [--to YYYY-MM-DD]
   noodles --data D build bars
   noodles --data D pull indices --from 2011-06-22
   noodles --data D pull nse --dataset corporate_actions --index equities sme --from 2011-01-01
   noodles --data D pull lists
   # optional, slow: announcements give instrument-class evidence for bonuses
   noodles --data D pull nse --dataset announcements --from 2011-01-01
   ```
   Or everything in order: `noodles --data D run --from 2011-06-22`.
2. Build: `for s in bars ca-db actions hindsight reports; do noodles --data D build $s; done`
   (`--curation DIR` if the curation folder is not `D/curation`). For a backtest, also
   `noodles --data D build replay --as-of YYYY-MM-DD` per cutoff (the point-in-time replay).
   Keep `D/ledger/` with `D/raw/`: it is append-only and no rebuild recreates it.
3. Read the printed counts:
   - `bhavcopy.errors` / `indices.errors` / `failed` months: transient, rerun the same pull.
   - `coverage.gap`: weekdays with no file that no holiday list names. Add the true holidays
     to `curation/holidays.txt` (from NSE circulars), or refetch with `--recheck`.
   - `coverage.unaccounted`: never fetched; rerun the bhavcopy pull for that range.
   - `actions.review` reasons: see the README table. Demergers (`needs_research:*`) and rights
     (`no_issue_price`, `non_simple`, `ambiguous`) go to the `research-corporate-action` skill.
   - `reports.jumps` by security: equity jumps go to the `triage-jumps` skill; rights
     entitlements and fund units are expected.
   - `actions.available_at_unknown` / `reports.adjustment_log.available_at_unknown`: versions
     that never enter a replay (research or corrections without a source date: add the date
     the source shows, never an earlier one); `available_after_ex_date` counts events a
     backtest could only use some sessions after the ex-date (README, Point in time).
   - `actions.disputed` / review `demerger_versions_disagree`: a demerger whose ratio-priced
     factor is more than 10% from its ex-date gap; research the ratio and the dates.
   - `actions.withdrawn_from_ledger`: versions an earlier build produced and this one did not
     (a research row edited or removed). Correct through `corrections.tsv` instead, which adds
     versions.
4. After curation changes (research CSVs, corrections, lineage overrides), rerun only
   `build actions`, `build hindsight`, `build reports`, and `build replay --as-of D` for every
   replay a backtest reads (a replay file keeps the versions of the build that wrote it).

## Done when

- No pull errors, `coverage.gap` and `unaccounted` empty (or explained),
- every `review` row is either researched, corrected or knowingly left (with a note),
- equity jumps in `out/jumps.csv` are all triaged.

Report the counts verbatim; never claim a step passed without its printed output.
