# Larkspur Goods Data Pipeline: Architecture

Reference for on-call engineers. Read this before triage if you do not know where a table or log lives.

## Overview
- The shop's app database is exported nightly as CSV files into a landing zone.
- A nightly Dagster job loads one day (one partition) at a time into DuckDB, builds dbt models, then runs quality checks.
- Layers: landing zone -> `raw` schema -> `staging` schema (views) -> `marts` schema (tables).
- Each run processes exactly one partition date (`YYYY-MM-DD`). Reruns of a day are idempotent (delete-then-insert per `_dt`).

## Job and steps
- Dagster job name: `nightly`. Steps run strictly in order:
  1. `ingest`: load landing files into `raw.*`.
  2. `transform`: `dbt build` (models and dbt tests).
  3. `quality_checks`: volume, completeness, freshness, reconciliation, plus dbt test results.
- A failed step causes downstream steps to be `skipped`. The first failed step is the one to investigate.
- Each run executes in a fresh child process so the warehouse file handle is released on exit.
- `transform` fails the run on a model error. dbt test failures do NOT stop `transform`; they are reported by `quality_checks`.

## Where things live
- Landing files: `landing/<table>/dt=<YYYY-MM-DD>/<table>.csv`. The `products` dimension is a single file: `landing/products/products.csv`.
- Warehouse: `warehouse.duckdb` (DuckDB). Schemas: `raw`, `staging`, `marts`, `ops`.
- Pipeline log: `logs/pipeline.log`. One event per line; the line number is a stable id you can cite as `log:<n>`.
- Log line shape: `<ts> LEVEL [run=<8-char id> partition=<date> step=<step>] message`.
- Run history: Dagster instance under `dagster_home` (status, steps, timings, events per run).
- DQ results: table `ops.dq_results` (run_id, partition_date, check_id, kind, status, observed, expected, message, checked_at).
- Ingest config: `config/ingest.json` (per-table rename, parsers, ignore_columns, dedupe_key). Empty means "strict contract, no adaptations".
- dbt project: `dbt_project/` in the sandbox (the deployed copy). The repo copy is the known-good reference version.

## Raw lineage columns
- Every raw row carries `_dt` (partition date), `_source_file` (landing path), `_loaded_at`, `_run_id`.
- Use these to trace any bad row back to its landing file and the run that loaded it.

## Data contracts (raw.*)
Ingest matches columns BY NAME against these. Missing or unexpected columns fail the load.
- `products`: product_id INTEGER, sku VARCHAR, name VARCHAR, category VARCHAR, unit_price DECIMAL(10,2), active BOOLEAN.
- `customers`: customer_id INTEGER, email VARCHAR, first_name VARCHAR, country VARCHAR, signup_date DATE.
- `orders`: order_id BIGINT, customer_id INTEGER, order_ts TIMESTAMP, status VARCHAR, channel VARCHAR, currency VARCHAR, order_total DECIMAL(12,2).
- `order_items`: order_item_id BIGINT, order_id BIGINT, product_id INTEGER, quantity INTEGER, unit_price DECIMAL(10,2).
- `payments`: payment_id BIGINT, order_id BIGINT, method VARCHAR, amount DECIMAL(12,2), paid_ts TIMESTAMP.
- Accepted values enforced by dbt: order status in placed/shipped/cancelled/returned; channel in web/app/marketplace; currency USD; payment method in card/paypal/gift_card.

## Table lineage
- `raw.products` -> `staging.stg_products`.
- `raw.customers` -> `staging.stg_customers` -> `marts.dim_customers`.
- `raw.orders` -> `staging.stg_orders` -> `marts.fct_orders`, `marts.fct_daily_revenue`, `marts.dim_customers`.
- `raw.order_items` -> `staging.stg_order_items` (adds `line_amount` = quantity * unit_price) -> `marts.fct_orders`.
- `raw.payments` -> `staging.stg_payments` -> `marts.fct_orders`.
- Staging models are pass-through views with renames; they do no dedup or filtering. Duplicates in raw reach staging and the marts.
- A model error in a staging model skips every mart that depends on it.

## dbt tests
- Generic tests in `staging.yml` and `marts.yml`: unique/not_null keys, relationships, accepted_values.
- Singular tests: `assert_order_totals_match_items` (order total equals sum of line amounts), `assert_payments_match_orders` (every non-cancelled order has payments summing to its total).

## Ownership
- `upstream_app_team`: owns the app export (landing file content, schema, delivery). Escalate for schema decisions and export bugs.
- `analytics_engineering`: owns the dbt models, tests, and marts. Escalate for model logic and business definitions.
- `data_platform`: owns ingest code, ingest config, orchestration (Dagster), and the warehouse.

## Related runbooks
- triage.md, schema_changes.md, missing_data.md, duplicates.md, failed_jobs.md, remediation_policy.md.
