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
3. Raw downloads are kept untouched and never rewritten. Everything else is rebuilt from them.

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
16. Demerger: the factor is the MARKET gap, ex-date open / previous close (NSE runs a special
    pre-open that discovers it), divided by any other factor on the same day. Two cross-checks
    are written beside it and never used as the factor: the ratio priced at the resulting
    company's first close, 1 − Σ(rᵢ·Cᵢ)/P, and the cost-of-acquisition % (a book-value tax
    allocation, which can differ a lot from the market). The gap is accepted when a cross-check
    agrees with it; with none agreeing or none available, only when the ex-date is confirmed, the
    drop is at least 10% and the close five sessions later still agrees with it.
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
    move), each line with its evidence; it is applied last and re-applied on every rebuild.
31. The jump report (adjusted session moves beyond 25%) is the final check: after review, each
    jump is either a fixed factor or an explained genuine move.

## Point in time

32. Every accepted action records when its factor became knowable (`known_on`): the last date
    whose data the decision reads. Gate-confirmed and TERP factors and dividends: the ex-date
    (the ex open). A demerger held at its market gap: the close of the settle session. A
    demerger confirmed by its ratio: the resulting company's first close. A value only research
    or a correction supplies: its stated source date, never before the ex-date.
33. An action whose `known_on` is unknown is never dated by guess: it is left out of every as-of
    build and flags every bar from its ex-date as `factor_pending`.
34. The as-of build for day D uses only bars up to D, factors with `known_on` <= D and ISIN links
    whose `known_on` (the last bar of the window the link rules read) is <= D. The default build
    is the hindsight view, and flags the bars between an ex-date and its `known_on`.
35. `announced_on` is the first NSE filing naming the event within 180 days before the ex-date.
    It is information only; NSE's `caBroadcastDate` is not used (mostly empty, often after the
    ex-date).

## Coverage

36. Every weekday is accounted for: a bhavcopy, a listed holiday, a gap (no file and no holiday
    list names it) or unaccounted (never fetched). A missing day is never assumed a holiday.
37. Weekend sessions (Budget Saturdays, Muhurat) are sessions when a bhavcopy exists.
38. Index history starts later than the bars; only days after an index's first day count.
