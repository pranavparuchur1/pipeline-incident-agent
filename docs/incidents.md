# Incident catalog

The catalog holds 16 incidents in 5 families. Each is injected into 2026-10-01, the day after the clean baseline, whose landing files have been delivered but not yet processed. Each incident has a ground truth, true fixes, and decoy fixes. The Phase 2 gate proves them (see docs/results/phase2.md).

## Incidents

| ID | Family | What happened | Fails at | Evidence the gate requires | Outcome |
|---|---|---|---|---|---|
| S1 | schema_change | orders.order_total renamed to total_amount upstream | ingest | `missing columns ['order_total']`, `unexpected columns ['total_amount']` | recover |
| S2 | schema_change | customers.country renamed to country_code upstream | ingest | `missing columns ['country']`, `unexpected columns ['country_code']` | recover |
| S3 | schema_change | orders.order_total now exported as a currency string ('$59.90') | ingest | `failed loading raw.orders`, `$` | recover |
| S4 | schema_change | orders gained a new upstream column coupon_code | ingest | `unexpected columns ['coupon_code']` | recover |
| S5 | schema_change | payments.method dropped upstream (no source for a required column) | ingest | `missing columns ['method']` | escalate |
| M1 | missing_partition | orders file for the day never arrived | ingest | `landing file missing`, `orders` | recover |
| M2 | missing_partition | payments file arrived empty (header only) | quality_checks | `partition_present:raw.payments`, `assert_payments_match_orders` | recover |
| M3 | missing_partition | order_items file for the day never arrived | ingest | `landing file missing`, `order_items` | recover |
| D1 | duplicates | orders export retried and appended: every order appears twice | quality_checks | `unique_stg_orders_order_id` | recover |
| D2 | duplicates | about 5% of order_items rows duplicated in the export | quality_checks | `unique_stg_order_items_order_item_id` | recover |
| D3 | duplicates | yesterday's orders re-sent inside today's orders file | quality_checks | `unique_stg_orders_order_id`, `row_count_trend:raw.orders` | recover |
| F1 | failed_job | bad deploy: fct_orders.sql selects a column that does not exist | transform | `fct_orders`, `order_total` | recover |
| F2 | failed_job | bad deploy: stg_orders renamed order_amount, breaking every downstream mart | transform | `order_amount` | recover |
| F3 | failed_job | orders file truncated mid-write (export crashed) | ingest | `failed loading raw.orders` | recover |
| R1 | row_count_drop | partial export: only orders placed before 09:00 were delivered | quality_checks | `row_count_trend:raw.orders`, `row_count_trend:raw.order_items` | recover |
| R2 | row_count_drop | upstream filter bug: all 'web' channel orders missing from the export | quality_checks | `row_count_trend:raw.orders` | recover |

## Fixes

Each fix is written as action names with their params. Alternative fixes are separated by " or ". `<day>` is the incident day, 2026-10-01.

| ID | True fix(es) | Decoy fix(es) |
|---|---|---|
| S1 | `apply_column_mapping(table=orders, source_column=total_amount, target_column=order_total)` | `request_upstream_redelivery(tables=[orders], day=<day>)` or `rerun_only()` |
| S2 | `apply_column_mapping(table=customers, source_column=country_code, target_column=country)` | `request_upstream_redelivery(tables=[customers], day=<day>)` |
| S3 | `set_column_parser(table=orders, column=order_total, parser=strip_currency)` | `request_upstream_redelivery(tables=[orders], day=<day>)` or `rerun_only()` |
| S4 | `ignore_extra_columns(table=orders, columns=[coupon_code])` | `request_upstream_redelivery(tables=[orders], day=<day>)` |
| S5 | `escalate_to_owner(owner=upstream_app_team, summary=payments export no longer includes method)` | `request_upstream_redelivery(tables=[payments], day=<day>)` or `rerun_only()` |
| M1 | `request_upstream_redelivery(tables=[orders], day=<day>)` | `rerun_only()` |
| M2 | `request_upstream_redelivery(tables=[payments], day=<day>)` | `rerun_only()` |
| M3 | `request_upstream_redelivery(tables=[order_items], day=<day>)` | `rerun_only()` |
| D1 | `request_upstream_redelivery(tables=[orders], day=<day>)` or `deduplicate_on_ingest(table=orders, key=order_id)` | `rerun_only()` |
| D2 | `request_upstream_redelivery(tables=[order_items], day=<day>)` or `deduplicate_on_ingest(table=order_items, key=order_item_id)` | `rerun_only()` or `deduplicate_on_ingest(table=orders, key=order_id)` |
| D3 | `request_upstream_redelivery(tables=[orders], day=<day>)` | `deduplicate_on_ingest(table=orders, key=order_id)` or `rerun_only()` |
| F1 | `revert_model_change(model=fct_orders)` | `revert_model_change(model=stg_orders)` or `rerun_only()` |
| F2 | `revert_model_change(model=stg_orders)` | `revert_model_change(model=fct_orders)` or `rerun_only()` |
| F3 | `request_upstream_redelivery(tables=[orders], day=<day>)` | `rerun_only()` or `deduplicate_on_ingest(table=orders, key=order_id)` |
| R1 | `request_upstream_redelivery(tables=[orders, order_items, payments], day=<day>)` | `rerun_only()` or `request_upstream_redelivery(tables=[orders], day=<day>)` |
| R2 | `request_upstream_redelivery(tables=[orders, order_items, payments], day=<day>)` | `rerun_only()` |

## Remediation allow-list

These are the only actions anything may use to change the sandbox, the agent included. Each one validates its params strictly. Applying an action never reruns the pipeline. Verification is a separate step.

| Action | Description | Params | Changes state |
|---|---|---|---|
| `request_upstream_redelivery` | Ask the upstream system to re-export these tables for a day. | tables: list of event tables; day: YYYY-MM-DD | Yes |
| `apply_column_mapping` | Read a renamed upstream column into its contract column at ingest. | table: raw table; source_column: upstream name; target_column: contract name | Yes |
| `set_column_parser` | Parse a reformatted upstream value at ingest. | table: raw table; column: contract column; parser: one of the keys of `PARSERS` (sandbox/ingest.py) | Yes |
| `ignore_extra_columns` | Ignore new upstream columns that are not part of the contract. | table: raw table; columns: list of upstream column names | Yes |
| `deduplicate_on_ingest` | Keep one row per key when loading a table. | table: event table; key: unique key column | Yes |
| `revert_model_change` | Restore one dbt model to the deployed version. | model: dbt model name | Yes |
| `rerun_only` | Change nothing; just rerun the pipeline. | none | No |
| `escalate_to_owner` | Hand the incident to a human team; change nothing. | owner: one of upstream_app_team, analytics_engineering, data_platform; summary: what and why | No |
