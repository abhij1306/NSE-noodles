# Method: invariants and rules

The rules the code keeps, stated once. Code is the source of truth; if this file and the code
disagree, one of them is wrong.

## Sources

1. Only official, free NSE sources: the daily bhavcopy archives (old CM format to 5 Jul 2024,
   UDiFF from 8 Jul 2024), the daily index archive, NSE's website JSON endpoints for corporate
   actions, announcements and board meetings, and NSE's symbol-change and name-change lists.
   Research may read company filings, BSE/NSE filings and reputable news, always quoted.
2. The data belongs to NSE. Nothing pulled is committed or redistributed. Pace requests (one
   every 2 s by default), one pull at a time, and follow NSE's terms.
3. Raw downloads are kept untouched and never rewritten. Everything else is rebuilt from them,
   except the append-only factor ledger (rule 32), which records when each version was first
   observed.

## Identity and survivorship

4. A security is its ISIN, not its symbol. Symbols change; the ISIN of a given share does not
   (until a reissue, see lineage).
5. The history starts on 22 Jun 2011, the first bhavcopy with an ISIN column.
6. A day keeps one row per ISIN: the normal-market series first (EQ, then BE, BZ, then block
   windows such as BL/IL that trade beside it).
7. Nothing is filtered by today's listing. Every security in each day's file is kept, so
   delisted, suspended and merged-away companies stay in the history (survivorship-bias free).
   A chain whose last bar is more than 20 sessions before the data's last day stopped trading.
8. "Ordinary equity" is read from the ISIN (`IN` + `E`/`9` + issuer + security type `01`),
   never from a label. NSE's instrument-class labels are unreliable: treat them as hints.

## ISIN lineage

9. A new ISIN for the same shares (face-value change, reissue, symbol or name change) is linked
   to the old one only when all rules hold: succession within 5 sessions with no overlap, the
   same symbol or an NSE symbol-change record, the new first PREVCLOSE equal to the old last
   close (raw or factor-adjusted) within 1%, the new first open within 10% of it, no session
   move over 25% at the edges unexplained by a known factor, and no restructuring text nearby.
10. Anything else is a review case, never a link. A false link is worse than a missing one.
11. Demergers, mergers, schemes and relistings keep separate histories. A demerged company is
    a new listing; the parent is adjusted by a factor, never linked to it.
12. An owner verdict (link / unlink) on a found succession overrules the rules and survives
    every rebuild.

## Factors

13. A price factor multiplies every price before the ex-date and lies in (0, 1), except a
    consolidation (reverse split, added by correction), whose factor is above 1. Adjusted price
    = raw x the product of the factors with ex-date after the bar's date, applied once per chain.
    Volume is divided by the same factor.
14. Split: new face value / old. Bonus x:y: y / (x + y). Bonus + split in one purpose: both legs.
15. Rights M:N at issue price S (face value + premium), cum price P = previous close:
    TERP / P = (N·P + M·S) / ((N+M)·P). Neutral when S ≥ P. A partly-paid issue uses the full
    issue price. Warrants, convertibles or debentures alongside: review.
16. Demerger: NSE gives no factor; the market prices it. The EX-DATE VERSION is the gap ex-date
    open / previous close (NSE sets the open in a special pre-open for F&O stocks, the regular
    pre-open otherwise), divided by any other factor on the same day and by the NIFTY 500's move
    over the same night (its open / previous close), so the market's move is not read as the
    action; the raw gap is kept beside it, and without index data the raw gap is the version.
    Cross-checks: the research ratio priced at the resulting companies' first closes,
    1 − Σ(rᵢ·Cᵢ)/P with each Cᵢ brought back to the ex-date's market level by the index, and the
    cost-of-acquisition % (a book-value tax allocation, which can differ a lot from the market).
    The gap is accepted when a cross-check agrees with it; with none agreeing or none available,
    only when the ex-date is confirmed, the drop is at least 10% and the neutralised close five
    sessions later still agrees with it. Once every resulting company has traded, the
    ratio-priced value is a second version, available from that first close; more than 10% (log)
    from the ex-date version it is `disputed`, never used, and the event goes to review. Every
    demerger version records its band: the range of the estimates known for it. The stock's own
    news that morning is in every open-based estimate and cannot be removed.
17. A bonus of debentures or preference shares issues no equity: never a price factor. When its
    value is at least 1% of the previous close it is a cash distribution of that value (as a
    dividend); below 1% it is treated as neutral.
18. A bonus to public holders only (promoters waived) is old shares / new shares from the
    allotment filing, not the stated ratio.
19. ETF unit splits are not in NSE's corporate-action feed; they appear as jumps and are added
    through the correction list from NSE circulars.

## The price gate

20. Every price factor is checked against the bars before it is accepted: the gap is the
    ex-date OPEN over the previous session's CLOSE (the bhavcopy's PREVCLOSE on an ex-date is
    already adjusted by NSE, so only the open witnesses the action).
21. Accepted when the gap is within 20% of the expected factor (times the other factors of that
    session) and at least twice as close to it as to 1 in log terms. Otherwise `off_factor` or
    `ambiguous`: review. 20% is NSE's widest band; on ~700 confirmed factors the median miss
    was 2.6%.
22. Exception: a rights factor whose gap is only `ambiguous` (a few percent drowns in a normal
    session move) is accepted, tagged `price_unconfirmed`. `off_factor` still goes to review.
23. A source never overwrites the bars: a text that the bars contradict is a review case.

## Dividends and total return

24. Every cash dividend counts, however small (leaving small ones out biases returns down).
25. A purpose text with no single amount, a percentage of face value, amounts that disagree,
    or cash above half the previous close is a review case, never guessed. NSE keeps revisions
    as separate rows: identical rows count once, different kinds add up, a revised row settles a
    disagreement.
26. Two series, never mixed: the PRICE basis (price factors only) for fills, marks and P&L, with
    the cash credited on the ex-date to shares held into it; and the TOTAL-RETURN series (price
    factors × 1 − D / previous raw close per dividend) for signals. Counting a dividend in both
    double counts it.
27. A rule on an absolute rupee price (minimum price, tick size) reads the raw price of its date.

## Review, research and corrections

28. Nothing is guessed. A row that does not parse, has no bars, or is not confirmed goes to the
    review list with a reason.
29. Research rows carry a source URL, a short verbatim quote and a confidence. Only stated values
    are used; low confidence goes to review. A research row overrides NSE's terms only when
    confident; a date it disputes sends the event to review.
30. Wrong or missing factors are fixed by a reviewed correction list (add, replace, delete,
    move), each line with its evidence; it is applied last and re-applied on every rebuild. Each
    line adds a version (a deletion is a version of value 1); none edits one.
31. The jump report (adjusted session moves beyond 25%) is the final check: after review, each
    jump is either a fixed factor or an explained genuine move.

## Point in time

32. Factors are versioned and never overwritten. An event (chain, ex-date, type) has one
    version per accepted value: the first, a later pricing (a demerger's ratio version), each
    correction. Precedence is that order. Every version produced is appended to
    `ledger/factor_versions.jsonl`, which is never rewritten and records `first_observed_at`.
33. Each version carries `source_published_at` (its source's publication time, empty if
    unknown), `first_observed_at` (when this pipeline first produced it) and `available_at`: the
    latest of every price and source the value and its acceptance read, all in IST. Open-based
    values: the ex-date at 10:00; close-based: 16:00 on that session; date-only sources: the end
    of that day. A time is never moved earlier than its evidence. A source without a date makes
    `available_at` unknown (empty).
34. HINDSIGHT (`out/hindsight_daily.parquet`): each event at its accepted version of highest
    precedence, whatever its availability, for research only; `factor_pending` flags bars from
    an ex-date until the event's first version was available. POINT-IN-TIME REPLAY as of D
    (`out/replay_daily_asof=D.parquet`): bars up to D, per event the accepted version of highest
    precedence with `available_at` by the end of D and an ex-date on or before D, and ISIN links
    whose `known_on` (the new ISIN's first day + 10 days, the window the link rules read) is on or
    before D. An unknown `available_at` never qualifies. Backtests read the replay.
35. Adding a version available after D never changes the replay as of D (tested on the pure
    selection, `versions.select`).
36. `announced_on` is the date of an NSE filing (announcement or board-meeting intimation) of the
    symbol within 180 days before the ex-date, up to the ex-date. For a split, bonus, rights
    issue or dividend: the first filing naming the event (information only). For a demerger: the
    first filing that says record date, names the demerger, scheme, merger or hive-off (NSE's
    template reads "Merger/HiveOff"), and writes the record date that held (the feed's) as a
    date; a filing naming an earlier, later changed date does not count. Nothing found leaves it empty, never guessed. NSE's `caBroadcastDate` is not
    used (mostly empty, often after the ex-date).

## Coverage

37. Every weekday is accounted for: a bhavcopy, a listed holiday, a gap (no file and no holiday
    list names it) or unaccounted (never fetched). A missing day is never assumed a holiday.
38. Weekend sessions (Budget Saturdays, Muhurat) are sessions when a bhavcopy exists.
39. Index history starts later than the bars; only days after an index's first day count.

## Held positions

40. A position held into a demerger exits at the close of the last trading session before the
    ex-date, when its record date was announced strictly before that session (`announced_on` <
    exit session). Announced on the exit session itself or later, or never dated, the position
    holds through and the factor adjusts it once available. `out/demerger_exits.csv` lists every
    demerger of the feed with its exit session, whether it is exitable and why not;
    `out/demerger_exits_asof=D.csv` lists only those announced by D, with the bars and ISIN
    links known by D. Feed rows of one event that disagree on the record date are never
    exitable (`record_dates_disagree`). Why: on the ex-date the parent opens lower by the resulting companies'
    value (HEG on 7 Sep 2026: previous close 728.25, open 260). On the adjusted series a backtest
    sees no drop and holds; an engine on raw prices sees a crash, stops out at a false loss, and
    the resulting shares it receives are counted nowhere. Exiting before the ex-date treats both
    alike and needs no factor that is unknown until after the event.
41. The trading calendar is the days with a bhavcopy; past the last of them, the weekdays that
    no listed holiday names. A holiday is no session: an ex-date on a Monday exits on Friday's
    close, one after a Monday holiday on the Friday before it. Holiday lists carry no publication
    date, so a replay as of D reads them as they are now; NSE publishes the year's list before
    the year starts, and only an unscheduled closure added after D could move a future exit
    session in that replay.
42. Settlement: under T+1 the ex-date is the record date (or, when the record date is not a
    session, the last session before it). A sale on the last session before the ex-date settles
    by the record date: the buyer is the holder of record and gets the resulting shares, the
    seller does not. That is intended: the sale price includes their value. A sale on the
    ex-date settles after the record date, so the seller still gets them. Under T+2 (before
    2023) the ex-date was a session before the record date; a sale before the ex-date gave up
    the entitlement then too, so the rule is defined on the ex-date, not the record date.
43. Splits and bonuses: hold, and rescale the stop and any price level by the factor from its
    ex-date (the replay applies it from its `available_at`). No value leaves the position.
44. Rights: the user's choice, not the dataset's. The rights factor (TERP) assumes the
    entitlement's value reaches the holder, which a position gets only by selling the
    entitlements (`-RE`) or subscribing. A strategy that does neither loses that value on the
    ex-date; it may instead exit before the ex-date as for a demerger, or hold and book the
    gap. Say which in the backtest.
45. Re-entry after a demerger needs no special rule: once the factor is available the replay
    applies it (rule 34). Until then the replay as of D shows the raw gap of a chain whose
    demerger ex-date is on or before D; a signal read there reads the event, not the market
    (the exit calendar as of D lists the demergers announced by D).
