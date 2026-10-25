# Backtesting with the dataset

Two series come out of a build, for two jobs. Mixing them up is the most common way to get a
backtest wrong.

| Series | File | Use it for |
|---|---|---|
| Hindsight | `out/hindsight_daily.parquet` | Research on what happened. Never a backtest's decisions |
| Point-in-time replay | `out/replay_daily_asof=D.parquet` | A backtest's decisions on day D |

Columns of both: [outputs](outputs.md#series-columns). The rules behind them:
[method, point in time](method.md#point-in-time).

## Hindsight vs replay

A corporate-action factor is a set of **versions**, each with an `available_at`: the earliest
time it could have been known. A demerger's factor, for example, may be known only days after
the ex-date.

- The **hindsight** series applies every event's latest version to all earlier bars, whatever
  its availability. It shows a smooth history that nobody could have seen on the day. Bars from
  an ex-date until the event's first version was available are flagged `factor_pending`.
- The **replay as of D** is the series as it could have been built after the close of D: bars
  up to D, only the versions with `available_at` by the end of D, and only the ISIN links known
  by D. A version with unknown availability never enters a replay.

**Compute day D's signals from the replay as of D.** Build one replay per decision date your
backtest needs (each rebalance day, or every day for a daily strategy):

```bash
uv run noodles --data ~/nse-data build replay --as-of 2026-09-10
```

```powershell
foreach ($d in "2026-09-10", "2026-09-11") { uv run noodles --data ~/nse-data build replay --as-of $d }
```

Each replay file is about as large as the hindsight series, so keep only the dates you use. A
replay file holds the versions of the build that wrote it: rebuild your replays after any
curation change.

### Example: HEG, September 2026

HEG went ex-demerger on 7 Sep 2026: previous close 728.25, ex-date open 260.00, raw gap 0.3570.
The NIFTY 500 moved −0.03% that night, so the neutralised gap is 0.3571. With no
resulting-company price yet, the gap was accepted because it held at the settled close
([method rule 16](method.md#factors)), so `available_at` is 2026-09-11 16:00 and
`available_basis` is `settled_close`.

- The replay as of 10 Sep leaves HEG unadjusted: the raw 64% gap shows.
- The replay as of 11 Sep applies 0.3571 to every earlier bar.
- The hindsight series applies it throughout and flags 7-10 Sep `factor_pending`.

## Prices, dividends and signals

| Need | Read |
|---|---|
| Fills, stops, marks, P&L | The price basis `adj_open`, `adj_high`, `adj_low`, `adj_close` |
| Dividend cash | Credit the `cash` of each dividend event on its ex-date to shares held into it |
| Signals (momentum, breakouts, moving averages) | `tr_close`, which includes dividends, so an ex-dividend drop never looks like weakness |
| Rules on an absolute rupee price (minimum price, tick size) | The raw `close` of that date |

Never use `tr_close` for P&L and also credit the cash: that counts each dividend twice.

Dividend cash is in `out/factor_versions.csv` (`action_type = dividend`, `status = accepted`,
`cash` in rupees per share). For a replay as of D, take per event the highest `version` with
`available_at` on or before the end of D. `out/adjustment_log.csv` has the hindsight choice
only. Cash is credited on the ex-date; there are no pay dates.

## Demergers and the exit calendar

On a demerger's ex-date the parent opens lower by the value of the resulting companies. The
adjusted series shows no drop and holds through. An engine on raw prices sees a crash, stops
out at a false loss, and counts the resulting shares nowhere. On HEG's ex-date the parent opened
at 260 after a 728.25 close: a stop at 650 would fill at 260.

The rule that treats both alike, and needs no factor that the market sets only on the day
([method rules 40-42](method.md#held-positions)):

**A position held into a demerger exits at the close of the last trading session before the
ex-date, if the record date was announced strictly before that session.** Announced on that
session or later, or never dated, it holds through, and the factor adjusts it once available.

In a backtest, read the calendar **as of D**: `out/demerger_exits_asof=D.csv`, written by
`build replay --as-of D`, lists only demergers announced by D. `out/demerger_exits.csv` (from
`build reports`) lists every demerger, for research. Columns:
[outputs](outputs.md#exit-calendar-columns).

- `exit_session`: the last day with a bhavcopy before the ex-date. For an ex-date past the data,
  it is the last weekday that no listed holiday names ([curation](curation.md#holidaystxt)).
- `announced_on`: the first NSE filing that writes the record date that held
  ([method rule 36](method.md#point-in-time)). It needs announcements and board meetings pulled
  (`run --with-announcements`, or `pull nse --dataset announcements board_meetings --index
  equities sme`). Without them no demerger is exitable.

| `reason` (when not exitable) | Meaning |
|---|---|
| `announced_on_exit_session` | The notice came on the exit session itself: too late to decide to sell at its close |
| `announced_after_exit_session` | The notice came after the exit session |
| `announced_on_unknown` | The record date is known but no pulled filing writes it |
| `record_date_unknown` | The feed has no record date and no filing was found |
| `record_dates_disagree` | The feed's rows for one event name different record dates |
| `no_session_before_ex_date` | No session before the ex-date in the data |

Re-entry needs nothing special: once the factor is available the replay applies it. Until then
the replay as of D shows the raw gap of that chain, so a signal read there reads the event, not
the market.

## Splits, bonuses and rights

- **Splits and bonuses**: hold. Rescale the stop and any price level by the factor from the
  ex-date; the replay applies the factor from its `available_at`, which for a gate-confirmed
  split or bonus is the ex-date open. No value leaves the position.
- **Rights**: your choice, not the dataset's. The rights factor (TERP) assumes the
  entitlement's value reaches the holder, which a position gets only by selling the
  entitlements (`-RE`) or subscribing. A strategy that does neither loses that value on the
  ex-date. Either exit before the ex-date as for a demerger, or hold and book the gap. Say which
  in the backtest.

## Universe and survivorship

- Build the universe on day D from the replay as of D (or `out/replay_instruments_asof=D.parquet`),
  never from today's listed symbols: delisted and merged-away companies are in the data.
- `ordinary_equity` separates ordinary shares from ETFs/funds (`INF…`), rights entitlements and
  other instruments.
- Join histories by `chain_isin`, never by symbol.
