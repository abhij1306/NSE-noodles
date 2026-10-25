"""The published series: raw bars with each chain's cumulative factors, read as one history per
company. Two outputs, clearly named, never to be confused:

- HINDSIGHT (`out/hindsight_daily.parquet`, `out/instruments.parquet`; `build hindsight`): every
  event at its latest accepted version (`versions.select(..., None)`), applied to every bar
  before its ex-date, including versions known only later or never dated. For research on what
  happened, never for a backtest's decisions. `factor_pending` flags the bars from an ex-date
  until the first version of that event was available (all of them when none has a known time):
  there the series shows no gap that a trader then would have seen.
- POINT-IN-TIME REPLAY (`out/replay_daily_asof=D.parquet`, `out/replay_instruments_asof=D.parquet`;
  `build replay --as-of D`): the series as it could have been built after day D: bars up to D,
  for each event the version of highest precedence with `available_at` <= D (an unknown time
  never qualifies), and only the ISIN links known by D. For a backtest, compute day D's signals
  from the replay as of D.

Inside each, two adjusted views, never mixed:
- the PRICE basis (`adj_*`): raw x the product of every price factor (split, bonus, rights,
  demerger) with an ex-date after the bar's date. Fills, marks, stops and P&L read this, plus
  dividend cash credited on ex-dates.
- the TOTAL-RETURN close (`tr_close`): the price basis x the product of every dividend factor
  1 - D / previous raw close with an ex-date after the bar's date. Signals read this, so a
  dividend never looks like a breakdown. Counting a dividend in both would double count it.
Raw prices are kept (`open` .. `close`, and untouched in `bars/`): a rule on an absolute rupee
price (a minimum price, the tick size) must read the raw price of its date.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import polars as pl

from . import lineage, versions
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


def chain_map_as_of(links: pl.DataFrame, isins: list[str], as_of: date) -> pl.DataFrame:
    """isin -> chain_isin from the lineage links (old_isin, new_isin, known_on) known by
    `as_of`; an ISIN with no such link is its own chain."""
    known = links.filter(pl.col("known_on").is_not_null() & (pl.col("known_on") <= as_of))
    heads = lineage.chains(known.select("old_isin", "new_isin").rows())
    return pl.DataFrame(
        {"isin": isins, "chain_isin": [heads.get(i, (i, 0))[0] for i in isins]},
        schema={"isin": pl.Utf8, "chain_isin": pl.Utf8},
    )


def pending(bars: pl.DataFrame, first: pl.DataFrame) -> pl.Series:
    """Per bar: is a price event of its chain already in effect (ex-date <= date) but none of
    its versions available yet (date before `first_available_at`'s day, or no known time)?
    `first`: chain_isin, ex_date, action_type, first_available_at (`versions.first_available`)."""
    late = first.filter(pl.col("action_type") != "dividend").select(
        pl.col("chain_isin").alias("p_chain"),
        pl.col("ex_date").alias("p_ex"),
        pl.col("first_available_at").dt.date().fill_null(date.max).alias("p_known"),
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
    """The hindsight series (`as_of` None) or the point-in-time replay as of the close of
    `as_of`."""
    with duckdb.connect(str(layout.db), read_only=True) as con:
        con.execute(
            f"CREATE TEMP VIEW bars AS SELECT * FROM read_parquet({layout.bars_glob_sql()})"
        )
        all_versions = con.sql("SELECT * FROM factor_versions").pl()
        if as_of is None:
            actions = versions.select(all_versions, None)
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
                versions.select(all_versions, versions.cutoff(as_of))
                .drop("chain_isin")
                .join(cmap, on="isin", how="left")
                .with_columns(pl.col("chain_isin").fill_null(pl.col("isin")))
            )
    price = actions.filter(pl.col("action_type") != "dividend").select(
        "chain_isin", "ex_date", pl.col("value").alias("f")
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
    if as_of is None:
        out = out.with_columns(pending(out, versions.first_available(all_versions)))
        daily, listing = "hindsight_daily.parquet", "instruments.parquet"
    else:  # every factor of a replay was available by its cutoff: nothing is pending
        out = out.with_columns(pl.lit(False).alias("factor_pending"))
        daily = f"replay_daily_asof={as_of.isoformat()}.parquet"
        listing = f"replay_instruments_asof={as_of.isoformat()}.parquet"
    out = out.sort("chain_isin", "date")
    atomic_write(layout.out / daily, lambda tmp: out.write_parquet(tmp))

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
    atomic_write(layout.out / listing, lambda tmp: instruments.write_parquet(tmp))
    return {
        "rows": out.height,
        "chains": instruments.height,
        "stopped_trading": int(instruments["stopped_trading"].sum()) if instruments.height else 0,
        "last_session": str(last_session),
        "factor_pending_rows": int(out["factor_pending"].sum()) if out.height else 0,
        "events_used": actions.height,
        "price_events_used": int((actions["action_type"] != "dividend").sum()),
    }
