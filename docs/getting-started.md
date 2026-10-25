# Getting started

From a fresh clone to a built dataset. Each command is shown for bash (macOS, Linux, Git Bash)
and, where PowerShell differs, for PowerShell.

## 1. Install

You need git and [uv](https://docs.astral.sh/uv/). uv installs Python 3.12+ for the project if
you don't have it.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then clone and install:

```bash
git clone https://github.com/abhij1306/NSE-noodles
cd NSE-noodles
uv sync
uv run noodles --help
```

`uv sync` creates `.venv/` with the locked dependencies. Prefix commands with `uv run`, or
activate the environment once per shell and type `noodles` directly:

```bash
source .venv/bin/activate          # macOS / Linux
```

```powershell
.venv\Scripts\Activate.ps1         # Windows PowerShell
```

## 2. Choose a data directory

Pick a folder **outside any git repository**, e.g. `~/nse-data`. NSE's data must never be
committed. `noodles` expands `~` itself, so `--data ~/nse-data` works in PowerShell too. The
examples below use `~/nse-data`.

## 3. A first small run

The guided menu is the easiest start. It asks for each choice and shows the command it runs:

```bash
uv run noodles
```

Or run everything for about seven weeks (a few minutes at the default pace):

```bash
uv run noodles --data ~/nse-data run --from 2026-09-01 --to 2026-10-23
uv run noodles --data ~/nse-data build replay --as-of 2026-10-15
```

`run` pulls the bhavcopies, builds the bars, pulls the index archive, the corporate-action feed
(equities and SME) and NSE's lists, then builds every step. It does not build a replay: the
second command builds one as of 15 Oct 2026. All the commands are in [cli](cli.md); what each
output contains is in [outputs](outputs.md).

To run the steps one by one instead:

```bash
D=~/nse-data
uv run noodles --data $D pull bhavcopy --from 2026-09-01 --to 2026-10-23
uv run noodles --data $D build bars
uv run noodles --data $D pull indices --from 2026-09-01 --to 2026-10-23
uv run noodles --data $D pull nse --dataset corporate_actions --index equities sme --from 2026-09-01 --to 2026-10-23
uv run noodles --data $D pull lists
for s in bars ca-db actions hindsight reports; do uv run noodles --data $D build $s; done
```

```powershell
$D = "~/nse-data"
uv run noodles --data $D pull bhavcopy --from 2026-09-01 --to 2026-10-23
uv run noodles --data $D build bars
uv run noodles --data $D pull indices --from 2026-09-01 --to 2026-10-23
uv run noodles --data $D pull nse --dataset corporate_actions --index equities sme --from 2026-09-01 --to 2026-10-23
uv run noodles --data $D pull lists
foreach ($s in "bars", "ca-db", "actions", "hindsight", "reports") { uv run noodles --data $D build $s }
```

`pull indices` fetches only days that already have bars, so `build bars` comes before it.

## 4. The full history

```bash
uv run noodles --data ~/nse-data run
```

`--from` defaults to 22 Jun 2011 (the first bhavcopy with ISINs) and `--to` to today.

**Time.** At one request every 2 seconds:

| Pull | Requests | Time |
|---|---|---|
| Bhavcopies and index archives | about 3,900 each | 4 to 5 hours |
| Corporate actions, equities and SME | about 185 months × 2 | about 15 minutes |
| Announcements (one request a week) and board meetings (`run --with-announcements`) | about 2,000 | 1 to 2 hours more |

The builds take minutes. Announcements and board meetings are optional for the series. They
add bonus instrument-class evidence and the notice dates (`announced_on`), and the demerger
exit calendar needs them: without them no demerger is exitable
([backtesting](backtesting.md#demergers-and-the-exit-calendar)).

**Disk.** A few gigabytes for the full history (a rough estimate, not a measurement): the raw
zips and CSVs, the bars, and the hindsight series are each in the hundreds of megabytes. Each
replay file is about the size of the hindsight series. Announcements with their attachment
text add more.

**Weekend sessions** (Budget Saturdays, Muhurat Sundays) are not in the weekday range. Fetch
them by date:

```bash
uv run noodles --data ~/nse-data pull bhavcopy --extra-days 2025-02-01 2023-11-12
```

## Check that it worked

Read the printed counts ([what each key means](cli.md#what-each-command-prints)):

| Count | Expect | If not |
|---|---|---|
| `bhavcopy.errors`, `indices.errors`, `<dataset>/<index>.failed` | empty | Transient: rerun the same pull |
| `lists` | every file `ok` | Rerun `pull lists` |
| `bars.failed` | empty | A zip with no valid rows (a stale or empty file from NSE). Check it; if it is bad, delete that zip and pull the day again |
| `reports.coverage.unaccounted` | 0 | Days never fetched: rerun `pull bhavcopy` for the range |
| `reports.coverage.gap` | 0 | Weekdays NSE has no file for and no holiday list names. Add true past holidays to `curation/holidays.txt` ([curation](curation.md#holidaystxt)), or ask again with `pull bhavcopy --recheck` |
| `reports.coverage.index_missing` | 0 | Rerun `pull indices` |
| `reports.jumps_by_security.equity` | small | Each equity jump is a factor to fix or a genuine move to note ([review reasons](review-reasons.md#the-jump-report)) |
| `actions.review` | a list of reasons | Work through `out/review.csv` ([review reasons](review-reasons.md)) |

The exit code is 1 when a pull or `build bars` failed ([cli](cli.md#exit-codes)).

A quick look at the series:

```bash
uv run python -c "import polars as pl; print(pl.read_parquet('DATA/out/hindsight_daily.parquet').filter(pl.col('symbol') == 'RELIANCE').tail())"
```

Replace `DATA` with your data directory (in Python, write `~` paths out in full).

## Rerun, resume, refresh

- Every step is resumable and idempotent. After an interruption, run the same command again:
  finished days and months are skipped.
- To extend the data, run `run --from <last bar day>`. The menu suggests this. NSE posts a
  day's files in the evening; a 404 for a day within the last 5 days is asked again on the next
  pull.
- The corporate-action feed for the month in progress is fetched again on every pull, so
  rerunning `pull nse` picks up new rows.
- After changing the curation folder, rerun `build actions`, `hindsight`, `reports` and your
  replays ([cli](cli.md#build)).
- Back up `raw/` and `ledger/` together. Everything else is rebuilt from them; the ledger
  cannot be ([outputs](outputs.md#the-data-directory)).
