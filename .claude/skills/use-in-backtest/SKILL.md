---
name: use-in-backtest
description: Read NSE-noodles outputs correctly in a backtest or strategy research - signals from the point-in-time replay as of each decision day, fills and P&L on the price basis plus dividend cash, tr_close only for signals, the demerger exit calendar, and a survivorship-free universe by chain_isin. Use when writing, reviewing or debugging code that reads hindsight_daily.parquet, replay_daily_asof=*.parquet or demerger_exits*.csv, or when backtest results look too good or show impossible moves.
---

# Use the dataset in a backtest

Reference: `docs/backtesting.md` (the rules and the HEG example), `docs/outputs.md` (columns).

## Rules

1. **Decisions on day D read the replay as of D**: `out/replay_daily_asof=D.parquet`, built by
   `uv run noodles --data D_DIR build replay --as-of D`. Never make decisions from
   `out/hindsight_daily.parquet`: it applies factors that were known only later. Hindsight is
   for research on what happened.
2. Build one replay per decision date you need, after the last curation change (a replay file
   keeps the versions of the build that wrote it). Rebuild them after any `build actions`.
3. **Prices**: fills, stops, marks and P&L use the price basis `adj_open, adj_high, adj_low,
   adj_close`. Compare two bars only within one file: a later replay may rescale earlier bars
   once a factor becomes available.
4. **Dividends**: credit the cash on the ex-date to shares held into it. Cash per share comes
   from `out/factor_versions.csv` (`action_type = dividend`, `status = accepted`): per event the
   highest `version` with `available_at` on or before the end of D. No pay dates.
5. **Signals** use `tr_close` (dividends included, so an ex-dividend drop is not weakness).
   Never compute P&L from `tr_close` and also credit cash: that counts dividends twice.
6. **Absolute rupee rules** (minimum price, tick size, circuit levels) read the raw `close` /
   `open` of that date, not `adj_*`.
7. **Universe**: on day D, the chains present in the replay as of D (or
   `out/replay_instruments_asof=D.parquet`), never today's symbol list. Join by `chain_isin`,
   never by symbol. `ordinary_equity` separates shares from ETFs (`INF…`), rights entitlements
   and other instruments.
8. **Demergers**: read `out/demerger_exits_asof=D.csv` (written with the replay). For a position
   held into a demerger with `exitable = true`, exit at the close of `exit_session`. Otherwise
   hold through: until the factor is available the replay shows the raw gap of that chain, so
   do not let a stop or a signal fire on it. Treat a chain with a demerger ex-date on or before
   D and no factor yet as untradeable for signals.
9. **Splits and bonuses**: hold, and rescale stops and price levels by the factor from the
   ex-date.
10. **Rights**: choose and state it: exit before the ex-date, or hold and book the gap. The
    TERP factor assumes the entitlement's value reaches the holder.

## Review checklist (for existing backtest code)

- Does any decision read `hindsight_daily.parquet`? It must read the replay as of that day.
- Is `factor_pending` (hindsight only) ignored in a study that uses hindsight? Flagged bars hide
  a gap a trader would have seen.
- Is the universe built from current symbols or joined by symbol?
- Are `tr_close` and cash credits both used for P&L?
- Are absolute price filters applied to adjusted prices?
- Do demerger ex-dates produce large losses on raw prices, or are held positions exited per the
  calendar?
- Were the replays rebuilt after the last curation change?

Report each problem found with the file and line, and the rule it breaks.
