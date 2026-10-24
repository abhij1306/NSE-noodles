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

Point in time. The default build uses every factor with hindsight: bars before an ex-date are
scaled by a factor first known on (or after) that date. Ratios inside the scaled stretch are
unaffected, but some factors (a demerger held at its market gap, a value from research) were
knowable only some sessions after the ex-date, and whether they were accepted at all depends on
those later sessions. So:
- `factor_pending` flags the bars from an ex-date up to the day before the factor's `known_on`
  (or every bar on and after the ex-date when `known_on` is unknown): there, the hindsight
  series shows no gap that a trader then would have seen. A backtest should not trade a name on
  such a day off the adjusted series.
- `build(as_of=D)` writes the series as it could have been built after the close of D: bars up
  to D, only the factors with `known_on` <= D, and only the ISIN links known by D
  (`out/adjusted_daily_asof=D.parquet`, `out/instruments_asof=D.parquet`).
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import polars as pl

from . import lineage
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


def available(actions: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """The actions a build after the close of `as_of` could have used: ex-date and `known_on`
    both on or before it (an action whose `known_on` is unknown never qualifies)."""
    return actions.filter(
        (pl.col("ex_date") <= as_of)
        & pl.col("known_on").is_not_null()
        & (pl.col("known_on") <= as_of)
    )


def chain_map_as_of(links: pl.DataFrame, isins: list[str], as_of: date) -> pl.DataFrame:
    """isin -> chain_isin from the lineage links (old_isin, new_isin, known_on) known by
    `as_of`; an ISIN with no such link is its own chain."""
    known = links.filter(pl.col("known_on").is_not_null() & (pl.col("known_on") <= as_of))
    heads = lineage.chains(known.select("old_isin", "new_isin").rows())
    return pl.DataFrame(
        {"isin": isins, "chain_isin": [heads.get(i, (i, 0))[0] for i in isins]},
        schema={"isin": pl.Utf8, "chain_isin": pl.Utf8},
    )


def pending(bars: pl.DataFrame, actions: pl.DataFrame) -> pl.Series:
    """Per bar: is a price factor of its chain already in effect (ex-date <= date) but not yet
    known (date < known_on, or known_on unknown)?"""
    late = actions.filter(
        (pl.col("action_type") != "dividend")
        & (pl.col("known_on").is_null() | (pl.col("known_on") > pl.col("ex_date")))
    ).select(
        pl.col("chain_isin").alias("p_chain"),
        pl.col("ex_date").alias("p_ex"),
        pl.col("known_on").fill_null(date.max).alias("p_known"),
    )
    keys = bars.select("chain_isin", "date").with_row_index("row")
    if late.is_empty() or keys.is_empty():
        return pl.Series("factor_pending", [False] * bars.height)
    hit = keys.join_where(
        late,
        pl.col("chain_isin") == pl.col("p_chain"),
        pl.col("date") >= pl.col("p_ex"),
        pl.col("date") < pl.col("p_known"),
    )["row"].unique()
    return keys["row"].is_in(hit.implode()).alias("factor_pending")


def build(layout: Layout, as_of: date | None = None) -> dict[str, int]:
    with duckdb.connect(str(layout.db), read_only=True) as con:
        con.execute(f"CREATE TEMP VIEW bars AS SELECT * FROM read_parquet('{layout.bars_glob()}')")
        actions = con.sql(
            "SELECT chain_isin, isin, ex_date, action_type, factor, cash, known_on FROM actions"
        ).pl()
        if as_of is None:
            bars = con.sql(
                "SELECT coalesce(c.chain_isin, b.isin) AS chain_isin, b.* FROM bars b "
                "LEFT JOIN chain_map c USING (isin)"
            ).pl()
        else:
            raw = con.sql(f"SELECT * FROM bars WHERE date <= DATE '{as_of.isoformat()}'").pl()
            links = con.sql(
                "SELECT old_isin, new_isin, known_on FROM lineage WHERE status = 'linked'"
            ).pl()
            cmap = chain_map_as_of(links, raw["isin"].unique().to_list(), as_of)
            bars = cmap.join(raw, on="isin").select("chain_isin", *raw.columns)
            actions = (
                available(actions, as_of)
                .drop("chain_isin")
                .join(cmap, on="isin", how="left")
                .with_columns(pl.col("chain_isin").fill_null(pl.col("isin")))
            )
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
    )
    out = out.with_columns(pending(out, actions)).sort("chain_isin", "date")
    tag = "" if as_of is None else f"_asof={as_of.isoformat()}"
    atomic_write(layout.out / f"adjusted_daily{tag}.parquet", lambda tmp: out.write_parquet(tmp))

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
    atomic_write(
        layout.out / f"instruments{tag}.parquet", lambda tmp: instruments.write_parquet(tmp)
    )
    return {
        "rows": out.height,
        "chains": instruments.height,
        "stopped_trading": int(instruments["stopped_trading"].sum()) if instruments.height else 0,
        "last_session": str(last_session),
        "factor_pending_rows": int(out["factor_pending"].sum()) if out.height else 0,
        "actions_used": actions.height,
    }
