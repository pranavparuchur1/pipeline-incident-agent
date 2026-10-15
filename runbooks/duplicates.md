# Duplicate Rows

Two distinct causes produce failing `unique` tests on id columns. They need different handling.

## Symptoms
- dbt tests fail: `unique_stg_orders_order_id`, `unique_stg_order_items_order_item_id`, `unique_stg_payments_payment_id` or similar, with N failing rows.
- `reconciliation:raw.orders->marts.fct_orders` fails: mart row count exceeds distinct raw orders.
- Revenue for a day is inflated; `row_count_trend` may fail on the high side (above 160%).
- Staging models do no dedup, so duplicates in raw flow straight to marts.

## Cause A: duplicate rows within one file (retried export)
- The export retried and wrote some rows twice into the same day's file.
- Evidence:
  - the same key appears more than once with the same `_dt` and `_source_file`
  - rows are identical copies
  - the keys are all dated to the day being loaded
- Query pattern: group `raw.<table>` by key where `_dt = <day>` having count(*) > 1.

## Cause B: stale rows re-sent from a previous day
- Today's file contains rows that belong to an earlier partition (same ids, old order timestamps).
- Evidence:
  - the key already exists in raw under a different `_dt`
  - `order_ts` of the duplicated rows is from a previous day, not the partition day
  - within today's partition, keys are NOT duplicated against each other
- Query pattern: join today's keys to `raw.<table>` where `_dt <> <day>`; count overlaps; compare timestamps.
- Check `_dt` and `_source_file` of both copies to show which two files collide.

## How to distinguish
| Check | A: within-file | B: stale re-send |
|---|---|---|
| duplicate keys inside the partition | yes | no |
| key present in other partitions | no | yes |
| `order_ts` of dupes | the partition day | earlier day |
| row count that day | inflated | inflated by the stale block |

## Remedy
- Cause A with identical copies: `deduplicate_on_ingest` on the table, with its unique key column (e.g. `order_id`). One row per key is kept at load. Safe because the copies are identical.
- Cause B: dedupe at ingest is NOT appropriate. Keeping one row per key within the partition does not remove rows that collide with another partition, and a key-based filter could discard legitimate rows.
  - Use `request_upstream_redelivery` for the day, with all affected related tables (`orders`, `order_items`, `payments`), to get a clean file with only that day's rows.
- If redelivery returns the same duplicates, the cause is persistent: escalate to `upstream_app_team`.
- If duplicates appear only in a mart while raw is unique, the model is wrong: escalate to `analytics_engineering`.

## Choosing the dedupe key
- Use the table's primary key: `order_id`, `order_item_id`, `payment_id`, `customer_id`.
- Never dedupe on a non-unique column (such as `customer_id` on orders): it would delete valid rows.
- `deduplicate_on_ingest` applies to event tables only (customers, orders, order_items, payments) and persists for all future loads of that table.
- Because it persists, note it in the ticket and revisit once upstream fixes the export.

## Verification
- Rerun the partition. Unique tests pass; reconciliation passes.
- Row count for the day is in the normal range.
- No key in the day's partition also appears in another partition (for cause B).
- Revenue for the day matches the expected level (compare with the trailing median).

## Do not
- Do not delete rows from the warehouse by hand.
- Do not remove or weaken the unique test.
- Do not dedupe when the duplicated rows differ in content: that signals a data problem, not a retry.
