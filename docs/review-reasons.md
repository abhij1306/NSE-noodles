# Review reasons

Whatever the pipeline cannot confirm goes to a review list with a reason, never into the
series. `out/review.csv` combines the corporate-action review (`list = actions`) and the ISIN
successions waiting for a verdict (`list = lineage`); its columns are in
[outputs](outputs.md#review-columns). Rows that are set aside on purpose (not errors) are in the
`skipped` table only. `build actions` prints the counts per reason.

Fixes go in the [curation folder](curation.md). The agent skills `research-corporate-action`,
`triage-jumps` and `review-lineage` in `.claude/skills/` walk through them.

## Corporate actions (`list = actions`)

The `table` column says which kind of row it is.

### Splits, bonuses, rights (`splits_bonus`, `rights_issues`)

| Reason | Meaning | What to do |
|---|---|---|
| `off_factor` | The ex-date gap (open / previous close) is outside the factor's band: more than +20% or −16.7% from it (log distance over ln 1.2) | Wrong ratio, wrong date, or not an equity action. Research the filing; fix with a correction |
| `ambiguous` | The gap is within the band but not at least twice as close (in log terms) to the factor as to 1: a normal session move could explain it. A rights-only gap that is merely `ambiguous` is accepted instead (`terp_price_unconfirmed`) | Usually a bonus to public holders only (promoters waived): find the allotment filing and `add` a `bonus` correction with old shares / new shares |
| `conflicting_rows` | Two feed rows for one session give the same action type different values | Find the right one; correct |
| `unparsed` | The purpose text could not be read (split/bonus legs, or a rights ratio and face value). A split or bonus stays here even with research attached | Split/bonus: a correction. Rights: a research row |
| `no_issue_price` | Rights text with a ratio but no single premium (or "at par") | Research the letter of offer: `issue_price` |
| `non_simple` | Rights text naming warrants, convertibles, debentures, preference shares or partly paid | Research the terms. Partly paid alone is priceable (full issue price, `partly_paid = yes`) |
| `non_simple_research` | A research row lists `non_equity_consideration` | Decide by hand; correct if it can be priced |
| `low_confidence` | Every research row for the rights issue is `low` | Better sources |
| `date_disputed` | A rights research row matched within 60 days but names another ex-date, or a record date that is not 0 to 7 days after NSE's ex-date | Check the dates; if NSE's is wrong, `our_data_wrong` and an `add` correction at the right date |
| `research_unmatched` | A `split_bonus` research row matches no feed row on its `ex_date` (`hint` = the research `file:line`) | Fix the row's ISIN, symbol or `ex_date`, or remove it |
| `no_bars` | No bar within 14 days on one side of the ex-date | Check suspension, symbol change; SME rows are skipped instead |
| `no_instrument` | No ISIN or symbol in the bars matches the feed row | Check the symbol history; SME rows are skipped instead |
| `research_says_row_wrong` | A research row has `our_data_wrong = yes` | A correction (`delete`, `move` or `add`) |

### Demergers (`demergers`)

The ex-date version is the index-neutralised gap ([method rule 16](method.md#factors)). It is
accepted when a cross-check agrees, or, with none, when the drop is material and held. Otherwise
the reason says why. The prefix `needs_research:` means there is no research row yet.

The code also names `*_date_unconfirmed` variants, but every demerger ex-date is currently
treated as confirmed, so they do not occur.

| Reason | Meaning | What to do |
|---|---|---|
| `needs_research:no_cross_check_gap_not_material` | No research, and the neutralised gap is above 0.9 (a drop under 10%) | Research the scheme: ratio, resulting company, CoA |
| `needs_research:no_cross_check_gap_not_held` | No research, and the neutralised close on the fifth session (counting the ex-date) does not agree with the gap, or five sessions have not passed yet | Research; or wait for more sessions |
| `needs_research:no_drop`, `no_drop` | The gap is not below 1 | Check the ex-date and the ISIN; possibly not a demerger of this company |
| `gap_disagrees_gap_not_material`, `gap_disagrees_gap_not_held` | Research gave a cross-check, it disagrees with the gap, and the gap alone is not enough | Check the ratio, the resulting company, the dates |
| `no_cross_check_gap_not_material`, `no_cross_check_gap_not_held` | Research exists but gives no usable cross-check (no ratio priced, no CoA) | Add the ratio or CoA |
| `demerger_versions_disagree` | The ratio-priced factor is more than +10% / −9.1% (log distance over ln 1.1) from the ex-date version. The ex-date version stays; the ratio version is `disputed` and never used | Check the ratio, the resulting company and its first session |
| `low_confidence` | A research leg has `low` confidence | Better sources |
| `date_disputed` | A research row matched within 60 days of NSE's ex-date but names another ex-date, or a `record_date` (within 60 days) that is not 0 to 7 days after NSE's ex-date | Check the dates; if NSE's is wrong, `our_data_wrong` and an `add` correction at the right date |
| `resulting_company_ambiguous` | A research row gives a `resulting_symbol` (no ISIN) under which more than one new ISIN started trading from 5 days before the ex-date | Add `resulting_isin` |
| `no_bars`, `no_instrument`, `research_says_row_wrong` | As above | As above |

### Cash (`dividends`, `bonus_instruments`)

| Reason | Meaning | What to do |
|---|---|---|
| `several_amounts` | The purpose names several amounts that cannot be paired with their kinds | Read the filing; a `dividend` correction |
| `percent_of_face_value` | The amount is given as a percent of face value | Convert from the filing; a `dividend` correction |
| `no_amount` | No readable rupee amount (also any amount in paise) | Check the filing |
| `breakdown_disagrees` | A total and its bracketed breakdown do not add up | Check the filing |
| `rows_disagree` | NSE's rows for one ex-date give different amounts and none is marked revised; rows add up across kinds only when every row names its kind | Check the filing |
| `amount_vs_price` | Cash above half the previous close, for the day as a whole (a dividend and a bonus instrument together) | Check; a real one is a correction |
| `needs_value` | A bonus of debentures or preference shares with no researched value | Research `value_per_share_rs` |
| `low_confidence` | That research is `low` | Better sources |
| `some_holders_only` | The research notes say only some holders received it | Decide by hand; correct if it should count |
| `research_unmatched` | A `bonus_instrument` research row matches no feed row from 7 days before to 3 days after its `ex_date` (`hint` = the research `file:line`) | Fix the row's ISIN, symbol or `ex_date`, or remove it |
| `no_bars`, `no_instrument` | As above | As above |

### Skipped (in the `skipped` table, not in review)

| Reason | Meaning |
|---|---|
| `non_equity_bonus` | A bonus of non-equity instruments named in the text |
| `not_equity_share`, `not_equity_series`, `preference_dividend`, `no_dividend` | A dividend row that is not a cash dividend on an equity share |
| `duplicate_event` | A second feed row for an event already decided: a demerger on the same session pair, a cash event on the same ex-date, or a split/bonus/rights row repeating factors already stated |
| `rights_at_or_above_price` | Issue price at or above the previous close: no value passes |
| `amalgamation_only` | Demerger research names only amalgamation legs |
| `before_bars` | The ex-date is before the chain's first bar |
| `below_one_percent` | A bonus instrument worth under 1% of the price |
| `no_instrument`, `no_bars` | SME rows (SME series are not in the bars) |

## ISIN successions (`list = lineage`)

A succession is linked only when every rule holds ([method rules 9-12](method.md#isin-lineage)).
`actions.lineage` counts both outcomes as `status:reason`. Linked reasons are
`face_value_change`, `symbol_change`, `name_change`, `isin_reissue` and `owner_link`.

| Reason | Meaning |
|---|---|
| `ambiguous` | The old or the new ISIN has more than one candidate |
| `long_gap` | More than 5 sessions between the old ISIN's last day and the new one's first |
| `restructuring` | A corporate action within 30 days names a demerger, amalgamation, merger, arrangement or capital reduction |
| `factor_conflict` | Two different factors of one type on one day for the pair |
| `no_prev_close` | The new ISIN's first bar has no previous close |
| `prev_close_mismatch` | The new first previous close is more than 1% from the old last close, raw and factor-adjusted |
| `price_jump` | The new first open is more than 10% from the adjusted old close, or a session move over 25% at the edges |
| `owner_unlink` | Your `unlink` verdict (kept on the list as a record) |
| `owner_displaced` | A rules link that shares an ISIN with your `link` |
| `override_unused` | Your `link` names a pair the rules never found as a candidate, so it cannot apply |

What to do: link or unlink in `lineage_overrides.tsv` after checking
([curation](curation.md#lineage_overridestsv), skill `review-lineage`).

## The jump report

`out/jumps.csv` is the final check: every adjusted session move beyond 25% is either a factor
still to fix or a genuine move to note (a huge special dividend, a tick-size artefact, an
insolvency relisting). Rights entitlements (`security = rights_entitlement`) and fund units
(`fund_unit`) jump by design. Columns: [outputs](outputs.md#jump-columns). Procedure: skill
`triage-jumps`.

## The demerger exit calendar

Its `reason` column (why a demerger is not exitable) is explained in
[backtesting](backtesting.md#demergers-and-the-exit-calendar).
