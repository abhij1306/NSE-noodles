# Glossary

Terms used in these docs and in the code, in alphabetical order.

**adjusted price (price basis, `adj_*`).** The raw price multiplied by every price factor whose
ex-date comes after the bar's date. It makes the history before a split, bonus, rights issue or
demerger comparable with prices after it. See [backtesting](backtesting.md).

**`available_at`.** The earliest time (IST) a factor version, and the rule that accepted it,
could have been known. When it is empty, the version never enters a point-in-time replay. See
[method rule 33](method.md#point-in-time).

**bhavcopy.** NSE's end-of-day file for a segment: one row per security and series, with open,
high, low, close, previous close, volume and value. NSE-noodles reads the capital-market (CM)
bhavcopy, published daily as a zip in NSE's archives.

**bonus issue.** Free shares given to existing holders, e.g. 1:1 (one new share for every share
held). The price factor is y / (x + y) for x:y. A **bonus of debentures or preference shares**
issues no equity: it is treated as a cash distribution of the instrument's value.

**chain, `chain_isin`.** One company's price history across ISIN changes of the same shares (a
face-value change, a reissue). `chain_isin` is the chain's latest ISIN. Two different companies
are never joined into one chain.

**circuit, price band.** NSE's daily limit on how far a stock may move (2% to 20%). An adjusted
move beyond 25% is therefore almost always a missing or wrong factor (see **jump**).

**consolidation (reverse split).** Several shares merged into one (face value up). Its factor is
above 1.

**corporate action (CA).** An event that changes the share count or pays holders: split, bonus,
rights issue, demerger, dividend, and so on.

**cost of acquisition (CoA).** The split of the original purchase cost between the parent and
the resulting company after a demerger, published by the company for tax. It is a book-value
allocation, not a market price, so it is used only as a cross-check.

**cum price.** The price of a share with the entitlement still attached. NSE-noodles uses the
previous session's close.

**demerger.** A company spins off a business into a separate listed company (the **resulting
company**). Holders of the parent receive its shares and the parent's price drops by their
value. NSE's feed never gives the ratio, so the market prices it. See
[backtesting](backtesting.md#demergers).

**ex-date.** The first session on which a buyer no longer gets the corporate action's
entitlement. The price gap from the previous close to the ex-date open is what the price gate
checks.

**F&O.** Futures and options. Stocks with derivatives that go through a restructuring open in a
**special pre-open** session (order matching by 09:55, normal market from 10:00). Other stocks
open in the regular pre-open (by 09:15).

**face value (FV).** The nominal value of a share (e.g. Rs 10). A **split** lowers it; the split
factor is new face value / old face value.

**factor.** The number a price is multiplied by to adjust it for an event. A **price factor**
(split, bonus, rights, demerger) lies in (0, 1), except a consolidation. A **dividend factor**
1 − D / previous close is used only in the total-return series.

**factor version.** Each value a factor ever had, kept with its timestamps and never
overwritten. See [method](method.md#point-in-time).

**gap.** The ex-date open divided by the previous session's close.

**hindsight series.** `out/hindsight_daily.parquet`: every factor at its latest version, applied
to all earlier bars. Use it for research on what happened, not for a backtest's decisions.

**hive-off.** The transfer of a business to another entity. NSE's filing template names
demergers "Merger/HiveOff".

**IST.** India Standard Time (UTC+05:30). Every time in the outputs is IST, stored without a
zone.

**ISIN.** The 12-character International Securities Identification Number, e.g.
`INE002A01018`. For Indian shares: `IN`, an issuer type (`E` company, `9` some others, `F`
mutual-fund units), a 4-character issuer code, a 2-digit **security type** (`01` equity shares,
`20` rights entitlements), a 2-character serial and a check digit. NSE-noodles treats an ISIN
as ordinary equity only when it starts `INE` or `IN9` and has security type `01`.

**jump.** A session in the adjusted series whose open is more than 25% away from the previous
adjusted close (with no more than 14 calendar days between the two bars). The jump report is
`out/jumps.csv`.

**Muhurat session, Budget Saturday.** Special weekend sessions: Muhurat trading on Diwali
(often a Sunday) and sessions held on a Saturday when the Union Budget is presented.

**NIFTY 500.** NSE's broad index of 500 companies. Its overnight move is used to remove the
whole market's move from a demerger's ex-date gap.

**PREVCLOSE.** The bhavcopy's previous-close column. On an ex-date NSE has already adjusted it
for the action, so it cannot witness the action. Only the ex-date open can.

**pre-open.** The 15-minute call auction before the market opens, which sets the opening price.

**record date.** The date on which the company checks who its holders are, to decide who gets
the entitlement. Under **T+1** settlement (from 2023) the ex-date is the record date, or the
last session before it when the record date is not a session. Under **T+2** (before 2023) the
ex-date was one session before the record date.

**replay (point-in-time replay).** `out/replay_daily_asof=D.parquet`: the series as it could
have been built after the close of day D. Backtests read this. See
[backtesting](backtesting.md).

**rights issue, rights entitlement (`-RE`).** An offer to existing holders to buy new shares at
an **issue price** (face value + premium), e.g. 1:5 at Rs 100. The entitlement itself trades for
a few sessions under a symbol ending `-RE` with security type `20`. **Partly paid**: the issue
price is paid in calls over time.

**SME.** NSE Emerge, the small and medium enterprise platform. Its corporate actions are pulled
with `--index sme`, but SME series are not in the bars, so SME events are skipped.

**scheme of arrangement, amalgamation.** The court-approved plan behind a demerger or merger.
An amalgamation absorbs one company into another.

**series.** The bhavcopy's market segment code: `EQ` (normal market), `BE`/`BZ`
(trade-for-trade), `BL`, `BT`, `IL`, `IQ` (block and institutional windows). The bars keep
these, one row per ISIN and day in that order of preference. Other series (debt, SME and so on)
are left out.

**split (sub-division).** One share divided into several with a lower face value. Factor = new
face value / old.

**survivorship bias.** The error of building a history only from securities that still exist,
which drops every delisted, merged or failed company and inflates returns.

**TERP.** Theoretical ex-rights price: (N·P + M·S) / (N + M) for M new shares per N held at issue
price S, with cum price P. The rights factor is TERP / P.

**total return (`tr_close`).** The adjusted close also adjusted for dividends, so a dividend
never looks like a price fall. Use it for signals, never for fills or P&L.

**UDiFF.** NSE's Unified Distilled File Formats, the bhavcopy layout used from 8 Jul 2024. The
old CM layout runs to 5 Jul 2024.
