# Missing, Partial, and Filtered Data

Covers missing or empty partitions and exports that arrived with fewer rows than normal.

## Symptoms
- `ingest` fails: `landing file missing: landing/<table>/dt=<date>/<table>.csv` (loud).
- Check `partition_present:raw.<table>` fails: `raw.<table> partition <date> has 0 rows` (silent: an empty file loads fine).
- Check `row_count_trend:raw.<table>` fails: today's rows fall outside 60%-160% of the trailing 7-partition median.
- Check `reconciliation:raw.orders->marts.fct_orders` fails: distinct raw orders for the day differ from `fct_orders` rows.
- Check `freshness:marts.fct_orders` fails: latest order date in the mart is behind the partition.
- Business symptom: revenue or order count for a day looks too low.

## Step 1: Which tables are affected?
- Compare row counts by `_dt` for `orders`, `order_items`, `payments` against the previous 7 days.
- Orders, items and payments are related. A drop in one with others normal suggests a partial export of that table.
- If all three dropped by a similar proportion, the export for the day was truncated or filtered across tables.

## Step 2: Missing file vs empty file
- Missing: the path does not exist; the error names it. Check whether the export ran at all (other tables present?).
- Empty: header only. `partition_present` fails; trend checks show 0%.

## Step 3: Partial vs filtered
- Partial (truncated) export: the file ends early. Rows are cut off by time: early hours present and late hours missing.
- Filtered export: a whole slice is absent regardless of time: e.g. one `channel` or one `status` has no rows, while the rest of the distribution looks normal.
- Compare today's distribution with prior days:
  - orders by hour of `order_ts`
  - orders by `channel`
  - orders by `status`
  - payments by `method`
- A chart or table of share-by-hour and share-by-channel for today vs a typical day makes the gap obvious.
- Record which hours or which category are missing; this is your evidence for the upstream ticket.

## Step 4: Check related tables
- Orders without items or payments (or the reverse) cause failures in `assert_order_totals_match_items` and `assert_payments_match_orders`, and relationship tests.
- Redelivering only one table can leave the set inconsistent. Check all three.

## Remedy for one-off export glitches
- Use `request_upstream_redelivery` for the day with ALL affected related tables together: `orders`, `order_items`, and `payments` (add `customers` only if it is also affected).
- Redelivery re-runs the export and produces a correct file for each table.
- Redelivering just `orders` when items/payments were also cut leaves referential mismatches.
- Then rerun the partition and verify.

## When redelivery will not help
- If the redelivered file is identical to the bad one, the cause is persistent (a standing upstream filter or schema change). Escalate to `upstream_app_team`.
- If the cause is a model filtering rows (marts lower than raw, raw fine), it is a dbt issue: see failed_jobs.md and escalate to `analytics_engineering`.

## Verification
- Rerun the partition. `partition_present`, `row_count_trend` and `reconciliation` must pass.
- Row counts for the day are now within the normal range of prior days.
- Hour and channel distributions match prior days.
- `assert_order_totals_match_items` and `assert_payments_match_orders` pass.

## Do not
- Do not loosen the 60%-160% threshold to make the check pass.
- Do not insert placeholder rows or copy a previous day's file.
- Do not treat a weekday-pattern day as an incident without checking the same weekday on prior weeks.
- Do not conclude "no data" without checking whether the export ran.
