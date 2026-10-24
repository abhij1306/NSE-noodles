"""The published series: raw bars with each chain's cumulative factors, read as one history per
company (`out/adjusted_daily.parquet`), and the instrument list (`out/instruments.parquet`).

Two adjusted views, never mixed:
- the PRICE basis (`adj_*`): raw x the product of every price factor (split, bonus, rights,
  demerger) with an ex-date after the bar's date. Fills, marks, stops and P&L read this, plus
  dividend cash credited on ex-dates.
- the TOTAL-RETURN close (`tr_close`): the price basis x the product of every dividend factor
  1 - D / previous raw close with an ex-date after the bar's date. Signals read this, so a
  dividend never looks like a breakdown. Counting a dividend in both would double count it.
Raw prices are kept (`open` .. `close`): a rule on an absolute rupee price (a minimum price, the
tick size) must read the raw price of its date.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import polars as pl

from .layout import Layout, atomic_write

STOPPED_SLACK_SESSIONS = 20  # a chain whose last bar is further back than this stopped trading


def _cumulative(events: pl.DataFrame) -> pl.DataFrame:
    """(chain_isin, ex_date, f) -> (chain_isin, until, cum): cum applies to dates <= until
    (= ex_date - 1 day): the product of the event's factor and every later one's."""
    if events.is_empty():
        return pl.DataFrame(schema={"chain_isin": pl.Utf8, "until": pl.Date, "cum": pl.Float64})
    return (
        events.group_by("chain_isin", "ex_date")
        .agg(pl.col("f").log().sum().alias("lf"))
        .sort("chain_isin", "ex_date", descending=[False, True])
        .with_columns(pl.col("lf").cum_sum().over("chain_isin").exp().alias("cum"))
        .with_columns((pl.col("ex_date") - timedelta(days=1)).alias("until"))
        .select("chain_isin", "until", "cum")
        .sort("chain_isin", "until")
    )


def _attach(bars: pl.DataFrame, cum: pl.DataFrame, name: str) -> pl.DataFrame:
    joined = bars.sort("date").join_asof(
        cum.sort("until"),
        left_on="date",
        right_on="until",
        by="chain_isin",
        strategy="forward",
        check_sortedness=False,
    )
    return joined.with_columns(pl.col("cum").fill_null(1.0).alias(name)).drop("cum", "until")


def build(layout: Layout) -> dict[str, int]:
    with duckdb.connect(str(layout.db), read_only=True) as con:
        con.execute(f"CREATE TEMP VIEW bars AS SELECT * FROM read_parquet('{layout.bars_glob()}')")
        bars = con.sql(
            "SELECT coalesce(c.chain_isin, b.isin) AS chain_isin, b.* FROM bars b "
            "LEFT JOIN chain_map c USING (isin)"
        ).pl()
        actions = con.sql("SELECT chain_isin, ex_date, action_type, factor, cash FROM actions").pl()
    price = actions.filter(pl.col("action_type") != "dividend").select(
        "chain_isin", "ex_date", pl.col("factor").alias("f")
    )
    # a dividend's factor uses the chain's raw close of the session before its ex-date
    cash = actions.filter(pl.col("action_type") == "dividend").select(
        "chain_isin", "ex_date", "cash"
    )
    closes = bars.select("chain_isin", "date", "close").sort("date")
    divs = (
        cash.with_columns((pl.col("ex_date") - timedelta(days=1)).alias("d"))
        .sort("d")
        .join_asof(
            closes,
            left_on="d",
            right_on="date",
            by="chain_isin",
            strategy="backward",
            check_sortedness=False,
        )
        .filter(pl.col("close").is_not_null() & (pl.col("cash") < pl.col("close")))
        .select("chain_isin", "ex_date", (1 - pl.col("cash") / pl.col("close")).alias("f"))
    )
    out = _attach(bars, _cumulative(price), "price_factor")
    out = _attach(out, _cumulative(divs), "div_factor")
    out = out.with_columns(
        *[
            (pl.col(c) * pl.col("price_factor")).alias(f"adj_{c}")
            for c in ("open", "high", "low", "close")
        ],
        (pl.col("volume") / pl.col("price_factor")).alias("adj_volume"),
        (pl.col("close") * pl.col("price_factor") * pl.col("div_factor")).alias("tr_close"),
        (
            pl.col("isin").str.slice(0, 2).eq("IN")
            & pl.col("isin").str.slice(2, 1).is_in(["E", "9"])
            & pl.col("isin").str.slice(7, 2).eq("01")
        ).alias("ordinary_equity"),
    ).sort("chain_isin", "date")
    atomic_write(layout.out / "adjusted_daily.parquet", lambda tmp: out.write_parquet(tmp))

    last_session = out["date"].max() if out.height else date.min
    sessions = out.select("date").unique().sort("date")["date"].to_list()
    cutoff = (
        sessions[-STOPPED_SLACK_SESSIONS - 1]
        if len(sessions) > STOPPED_SLACK_SESSIONS
        else date.min
    )
    instruments = (
        out.sort("date")
        .group_by("chain_isin")
        .agg(
            pl.col("isin").unique().sort().str.join(",").alias("isins"),
            pl.col("symbol").last().alias("last_symbol"),
            pl.col("symbol").unique().sort().str.join(",").alias("symbols"),
            pl.col("date").min().alias("first_day"),
            pl.col("date").max().alias("last_day"),
            pl.col("ordinary_equity").last().alias("ordinary_equity"),
        )
        .with_columns((pl.col("last_day") < cutoff).alias("stopped_trading"))
        .sort("chain_isin")
    )
    atomic_write(layout.out / "instruments.parquet", lambda tmp: instruments.write_parquet(tmp))
    return {
        "rows": out.height,
        "chains": instruments.height,
        "stopped_trading": int(instruments["stopped_trading"].sum()) if instruments.height else 0,
        "last_session": str(last_session),
    }
