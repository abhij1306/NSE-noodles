# NSE-noodles

**The method, not the data:** scripts and rules to build a clean, survivorship-bias-free daily
dataset of the National Stock Exchange of India (NSE) from NSE's own free sources, with every
corporate-action factor checked against the prices before it is used.

> **Data notice.** All market data belongs to NSE (and, for filings, to the companies). This
> repository contains **no data**. The scripts download from NSE's public archives and website
> endpoints to a folder on your machine; the website endpoints are not a documented public API.
> Respect NSE's terms of use and rate limits: keep the default pace of one request every 2
> seconds, never run two pulls at once, and do not redistribute what you download.
>
> **Disclaimer.** The code is provided as is, without warranty of any kind (see `LICENSE`).
> Nothing here is investment advice. The authors are not responsible for any use or misuse of
> the code or of data obtained with it, for losses arising from it, or for your compliance with
> NSE's terms and applicable law.

## Why

Two silent errors ruin most home-made Indian equity backtests. **Unadjusted corporate
actions** create false signals: a 1:1 bonus halves the price overnight, and in a raw series
that is a 50% crash that fires stops and breakdown signals. Demergers, rights issues, splits and
dividends do the same at other scales. **Survivorship bias**: a universe built from today's
listed symbols drops every company that was delisted, merged away or went bust, which inflates
returns. Joining histories by symbol also splices different companies or cuts one in two when
its ISIN changes.

NSE-noodles keys everything by ISIN and keeps every equity security that ever appeared in a
daily bhavcopy since 22 Jun 2011. It joins ISIN changes of the same shares only on evidence, and
accepts a corporate-action factor only when the ex-date price gap confirms it. Whatever it
cannot confirm goes to a review list with a reason, never into the series.

It also keeps **point in time**: each factor records when it could first have been known, so a
backtest can read the series as it could have been built on any past day, not with hindsight.

## Quick start

Install [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` on
macOS/Linux; `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
on Windows), then:

```bash
git clone https://github.com/abhij1306/NSE-noodles
cd NSE-noodles
uv sync
uv run noodles          # a guided menu: pick a data folder and a date range
```

Or run everything for a short range, then build a point-in-time replay:

```bash
uv run noodles --data ~/nse-data run --from 2026-09-01 --to 2026-10-23
uv run noodles --data ~/nse-data build replay --as-of 2026-10-15
```

To keep it current, rerun `run` (it skips what is on disk) or schedule it, and run `doctor` when
NSE changes something: [maintaining](docs/maintaining.md). Or install the command once:
`uv tool install git+https://github.com/abhij1306/NSE-noodles`, then `noodles ...` anywhere.

These commands are the same in PowerShell. Keep the data folder outside any git repository.
Without `--from`, `run` pulls the full history: 4 to 5 hours at the polite pace, resumable.
Details, time and disk estimates, and how to check the result:
[getting started](docs/getting-started.md).

## What you get

| Output (under the data folder) | What |
|---|---|
| `out/hindsight_daily.parquet` | Raw bars plus adjusted prices (`adj_*`), total-return close (`tr_close`) and `chain_isin`, every factor at its latest version. For research, not for a backtest's decisions |
| `out/replay_daily_asof=D.parquet` | The same series as it could have been built after day D. **Backtests compute day D's signals from it** |
| `out/review.csv` | Everything waiting for a human, with a reason |
| `out/jumps.csv` | Adjusted moves above 25% still to explain |
| `out/demerger_exits*.csv` | When a position held into a demerger should exit |
| `bars/`, `noodles.duckdb`, `ledger/` | Raw daily bars, the corporate-action database, the append-only factor ledger |

Every file and column: [outputs](docs/outputs.md).

## Docs

- [Getting started](docs/getting-started.md): install, first run, full history, checking it
  worked, resuming.
- [Command line](docs/cli.md): every command and flag, the menu, exit codes, printed counts.
- [Maintaining](docs/maintaining.md): scheduled updates, the `noodles.toml` settings, `doctor`
  when NSE changes something, `import` for files you downloaded yourself.
- [Outputs](docs/outputs.md): every file, column and database table.
- [Backtesting](docs/backtesting.md): hindsight vs replay, dividends, the demerger exit
  calendar.
- [Review reasons](docs/review-reasons.md): every reason code and what to do about it.
- [Curation](docs/curation.md): research CSVs, corrections, lineage overrides, holidays.
- [Method](docs/method.md): the rules the code keeps, the sources, and the limits.
- [Glossary](docs/glossary.md): bhavcopy, ex-date, record date, TERP, UDiFF and the rest.

**Agent skills (optional).** Nothing here needs an AI agent. For the review work that needs
judgement and web research, `.claude/skills/` holds skills for Claude Code (or any agent that
reads `SKILL.md`): `research-corporate-action`, `triage-jumps` and `review-lineage`.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff format --check . && uv run ruff check .
```

CI (`.github/workflows/ci.yml`) runs on every push to `main` and every pull request:
`ruff format --check`, `ruff check` (including a complexity limit of 10 per function), and
`pytest` on Python 3.12 and 3.13 on Linux and 3.13 on Windows. The tests use no network and no
NSE data.

## License

Code: MIT (see `LICENSE`). The data you download stays NSE's.
