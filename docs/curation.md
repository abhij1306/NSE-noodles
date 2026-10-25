# Curation: research, corrections, lineage overrides, holidays

The curation folder holds what people and agents add. It is input, never output, and is
re-applied on every rebuild. Its default location is `DATA/curation/`; `--curation DIR` on
`build` and `run` moves it.

```text
curation/
  research/*.csv           sourced research rows
  corrections.tsv          reviewed fixes to factors
  lineage_overrides.tsv    verdicts on ISIN successions
  holidays.txt             past NSE trading holidays
```

All of it is treated as untrusted text. Every value is parsed, and a line that does not parse
stops the build with an error naming its file and line. Nothing is skipped silently. After any
change, rerun `build actions`, `build hindsight`, `build reports` and your replays.

## research/*.csv

Research supplies what NSE's feed lacks: a demerger's ratio and resulting company, a rights
issue's terms, a bonus debenture's value. Any file name ending in `.csv` works. Files are read
in name order, then line by line.

### Columns

Every file needs all 24 columns in its header, in any order (`RESEARCH_COLUMNS` in
`src/nse_noodles/curation.py`). Write it with a CSV library so quotes and commas are escaped.

| Column | Value |
|---|---|
| `table` | Required: `demerger`, `rights`, `bonus_instrument` or `split_bonus` (see [table names](#from-a-review-row-to-a-research-row)) |
| `symbol`, `isin` | The NSE symbol and ISIN of the event (a match on either is enough) |
| `ex_date` | Required. **NSE's ex-date** of the event, as in the review row (`day` in `review.csv`). Write another date only to dispute NSE's ([rules](#rules)) |
| `leg_type` | Empty or one of `demerger`, `amalgamation`, `rights`, `bonus`, `split`, `bonus_instrument` |
| `resulting_name`, `resulting_symbol`, `resulting_isin` | Demerger: the resulting company. The build finds its prices by `resulting_isin`, else by `resulting_symbol` ([how](#how-a-row-is-matched-and-used)) |
| `ratio_a`, `ratio_b` | `ratio_a` new (or resulting) shares for every `ratio_b` held. Both or neither |
| `issue_price` | Rights: face value + premium, the **full** price even if partly paid |
| `face_value` | Informational only; not read |
| `partly_paid` | `yes` or `no` |
| `coa_parent_pct`, `coa_resulting_pct` | Demerger: the cost-of-acquisition split in percent (e.g. `63.5`). The parent's and every leg's should add up to 100 (±0.5) |
| `non_equity_consideration` | Warrants, convertibles, preference shares, debentures or cash offered alongside; empty if none |
| `value_per_share_rs` | Bonus instrument: rupee value of the debenture or preference share issued per equity share |
| `record_date` | The record date the source states |
| `our_data_wrong` | `no`, `unknown`, or `yes: <what is wrong>` (see below) |
| `confidence` | `high`, `medium` or `low`. Anything else counts as `low` |
| `source_url` | The source |
| `source_date` | The date the source was published. It decides when a backtest could have known the value |
| `quote` | A short verbatim quote containing the number |
| `notes` | Anything else: which kind of source, what was searched, partly-paid calls |

Formats:

- **Numbers** are plain decimals: digits, an optional decimal point, and optional thousands
  commas (`1000`, `1,000`, `0.5`). Write no `Rs`, `%` or `/-`. Every number must be above zero.
  Leave the cell empty when the source does not state the value.
- **Dates** are `YYYY-MM-DD` (NSE's `07-Sep-2026` form is also read).

The build stops on an unknown `table` or `leg_type`, an unreadable or non-positive number, a
lone `ratio_a` or `ratio_b`, a missing `ex_date`, or an unreadable date.

### From a review row to a research row

`review.csv` names the database table; the research CSV uses a shorter name. Copy `symbol` and
`isin`, write the review row's `day` as `ex_date`, and map `table`:

| `review.csv` `table` | research `table` |
|---|---|
| `demergers` | `demerger` |
| `rights_issues` | `rights` |
| `bonus_instruments` | `bonus_instrument` |
| `splits_bonus` | `split_bonus` |

A row with another `table` value stops the build.

### How a row is matched and used

A row applies to an NSE feed event of the same table with the same ISIN or symbol and an ex-date
close to its own:

| research `table` | NSE's ex-date may be | Unmatched row |
|---|---|---|
| `demerger`, `rights` | within 60 days of the row's `ex_date` (`DISPUTE_DAYS`) | Becomes an event of its own at its `ex_date`, for events NSE's feed lacks |
| `bonus_instrument` | from 7 days before to 3 days after the row's `ex_date` | Review: `research_unmatched` (the row's `file:line` is in `hint`) |
| `split_bonus` | the same day | Review: `research_unmatched` |

A matched `demerger` or `rights` row whose `ex_date` differs from NSE's, or whose `record_date`
(within 60 days of NSE's ex-date) is not 0 to 7 days after it, sends the event to review as
`date_disputed`; its terms are not used.

- **demerger**: one row per resulting company. Every row whose `leg_type` is `demerger` (or
  empty) is a leg; `amalgamation` rows are not, and an event with only amalgamation rows is
  skipped (`amalgamation_only`). Any leg with `low` confidence sends the event to review.
  The resulting company is `resulting_isin`, else the ISINs that first traded under
  `resulting_symbol` no earlier than 5 days before the ex-date, with bars on or after it, and
  outside the parent's chain. More than one such ISIN sends the event to review
  (`resulting_company_ambiguous`). With a ratio for every leg and every resulting company
  traded, the research ratio priced at the resulting companies' first closes is a cross-check,
  and a second version ([method rule 16](method.md#factors)). The CoA percentages are another
  cross-check.
- **rights**: the first row with `medium` or `high` confidence that has both a ratio and an
  `issue_price` gives the terms. Any row with `non_equity_consideration` sends the event to
  review (`non_simple_research`). If every row is `low`, the event goes to review
  (`low_confidence`). Otherwise NSE's parsed terms are used.
- **bonus_instrument**: the first row with `value_per_share_rs`. A `low` row goes to review.
  `notes` (or `non_equity_consideration`) that say `non-promoter`, `non promoter`, `public only`
  or `promoters excluded` send it to review (`some_holders_only`). A value below 1% of the
  previous close is skipped as price-neutral; otherwise it is credited as a cash distribution.
- **split_bonus**: only `our_data_wrong` is read; ratios are not used, and a split or bonus whose
  text did not parse stays in review (`unparsed`) whatever research it has. To fix a split or
  bonus, write a [correction](#correctionstsv).
- **our_data_wrong** = `yes: ...` on any row: the event's research terms are not used and it
  goes to review as `research_says_row_wrong` until a correction fixes it.

### Rules

- **Write NSE's ex-date in `ex_date`.** If you believe NSE's date is wrong, write the true date
  instead: for a demerger or rights issue within 60 days of NSE's, the event goes to review as
  `date_disputed`, where a person decides. If NSE's date is wrong, the fix is a correction:
  `add` at the right date (a `move` applies only to an event that was accepted). More than 60
  days away the row no longer matches and becomes a separate event, so do not do that.
- **One row per leg. Never add a second row for the same leg.** For a demerger every row is a
  leg, so a second row for the same resulting company counts that company twice. For rights the
  first confident row in file order wins, not the latest.
- **Change a value through a correction with `known_on`, not by editing the row.** Once a row's
  value is in `factor_versions`, editing or deleting the row withdraws that version from the
  build (the ledger keeps it; the build counts it as `withdrawn_from_ledger`), so the record of
  what was believed earlier is lost. A row whose event is still in review produced no version,
  and you can fix it in place.
- **`source_date` is the publication date the source shows.** Never write an earlier date.
  Without it the value has no `available_at` and never enters a point-in-time replay.
- Use only values a source states; leave the rest empty and say why in `notes`.

### Example

```csv
table,symbol,isin,ex_date,leg_type,resulting_name,resulting_symbol,resulting_isin,ratio_a,ratio_b,issue_price,face_value,partly_paid,coa_parent_pct,coa_resulting_pct,non_equity_consideration,value_per_share_rs,record_date,our_data_wrong,confidence,source_url,source_date,quote,notes
demerger,PARENT,INE000X01011,2025-01-06,demerger,Child Ltd,CHILD,INE000Y01019,1,10,,,,,,,,2025-01-06,no,high,https://example.com/filing.pdf,2024-11-20,"1 equity share of Child for every 10 shares of Parent",record-date intimation on NSE
```

## corrections.tsv

Reviewed fixes to factors, tab-separated, one per line. Lines starting with `#` and a header
line starting with `op` are skipped.

```text
op  isin  ex_date  type  value  new_ex_date  note  [known_on]
```

| Column | Value |
|---|---|
| `op` | `add`, `replace`, `delete` or `move` |
| `isin` | Any ISIN of the chain |
| `ex_date` | The event's ex-date |
| `type` | `split`, `bonus`, `rights`, `demerger`, `dividend` or `other` |
| `value` | `add`/`replace`: the factor, or rupees per share for `dividend`. Empty for `delete`/`move` |
| `new_ex_date` | `move` only: the right ex-date |
| `note` | Required: the evidence (source and the bars' agreement, e.g. `Bonus 1:1 per NSE filing <url>; gap 0.502`) |
| `known_on` | The date the corrected value was public (the circular or filing date) |

Value rules for `add` and `replace`: above 0. A `split` is new face value / old and not 1 (above
1 is a consolidation). A `dividend` is rupees per share. Every other type is below 1. A
public-only bonus (promoters waived) is old share count / new share count from the allotment
filing, not the stated ratio.

An event is (chain, ex-date, type). Each line **adds** a version and never edits one:

| `op` | Effect | Status in `corrections_log` when it cannot apply |
|---|---|---|
| `add` | A new event | `exists_use_replace` if the event already has an accepted version |
| `replace` | A new version with `value` | `not_found` if the event has no accepted version |
| `delete` | A version of value 1 (cash 0 for a dividend) | `not_found` |
| `move` | A deletion at `ex_date` and a version with the same value at `new_ex_date` | `not_found`; `destination_exists` if `new_ex_date` already has one |

An event that waits in review (e.g. an `ambiguous` bonus) was never accepted, so fix it with
`add`, not `replace`. The review row stays in `review.csv` after the fix, because review is
decided before corrections; check `actions.corrections` and the `corrections_log` table
instead.

**`known_on`** makes the version available from the end of that day (`available_basis`
`correction`). A line without it reaches the hindsight series only (`correction_undated`), never
a replay. A replay before `known_on` keeps what was believed then. A `move` is available from
the later of the old version's time and `known_on`.

```tsv
add	INE000X01011	2019-12-19	split	0.1		NSE circular: unit face value 10 -> 1; gap 0.1003	2019-12-10
```

## lineage_overrides.tsv

Verdicts on ISIN successions, tab-separated: `op old_isin new_isin note`, with `op` = `link` or
`unlink`. Lines starting with `#` and a header starting with `op` are skipped. Write a note
with the evidence.

- `link` applies only to a succession the rules found: the new ISIN starts trading after the
  old one stops, within 30 sessions, under the same symbol or an NSE symbol change. A `link`
  on any other pair is listed for review as `override_unused`.
- A `link` wins over the rules. A rules link sharing an ISIN with it becomes a review case
  (`owner_displaced`), so a chain never forks.
- `unlink` keeps the two histories apart. The pair stays in `review.csv` as `owner_unlink`, as a
  record of the verdict.
- A link's `known_on`, from which a replay may use it, is the latest of the new ISIN's first
  day + 10 days, its first day + 30 days, and the 31st session after the old ISIN's last day
  (until then a competing successor could still appear). It stays empty, and the link out of
  every replay, until the bars reach that session ([method rule 34](method.md#point-in-time)).

How to decide: the [review-lineage skill](../.claude/skills/review-lineage/SKILL.md) and
[method rules 9-12](method.md#isin-lineage).

## holidays.txt

Past NSE trading holidays, one date per line (`YYYY-MM-DD` or `DD-Mon-YYYY`); `#` starts a
comment. It is read from the curation folder, together with the `--holidays FILE` list and NSE's
current-year list (`raw/lists/holidays.json`, from `pull lists`). Coverage (`gap` vs `holiday`)
and the demerger exit calendar use the combined list.

NSE publishes only the current year's list. Build past years from NSE's yearly holiday
circulars, then check them against the archive: each true holiday should move a date from
`coverage.gap` to `coverage.holiday`, and none should appear in `on_listed_holiday`.
