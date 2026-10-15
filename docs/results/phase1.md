# Phase 1 results (2026-10-14)

Sandbox: a synthetic shop ("Larkspur Goods") with a real pipeline:
landing CSVs → `raw` (DuckDB, by-name contract) → dbt `staging` (5 views) → dbt `marts` (3 tables),
orchestrated by a Dagster job `nightly` (ingest → transform → quality_checks).

| Check | Result |
|---|---|
| `pia sandbox init`: 30 history days (2026-09-01..09-30) | 30/30 runs SUCCESS, 37/37 checks each day, 624 s total |
| Checks per run | 28 dbt tests + 9 custom (4 partition-present, 3 row-count-trend, freshness, raw→mart reconciliation) |
| Gate: restore `baseline` → run 2026-10-01, twice | both SUCCESS, 37/37 checks, wall 16 s / 15 s |
| Table fingerprints of the two runs (13 tables in raw/staging/marts) | identical (e.g. `marts.fct_orders` 13,841 rows `45110827a2f3`) |
| dbt tests have teeth (Sonnet worker, scratch copy) | duplicating one day's orders → `unique_stg_orders_order_id` FAIL (412 rows) and `assert_order_totals_match_items` FAIL; downstream marts SKIPPED |
| Unit tests | 19 passed (generator determinism, idempotent loads, loud schema/missing-file errors, rollback, trend check, snapshot/fingerprint) |

## Decisions and findings

- **Each pipeline run is its own Python process.** dbt-duckdb keeps the warehouse file open for the
  life of the process, so the first in-process design couldn't snapshot afterwards
  (`WinError 32`). A child process releases every handle when it exits. Cost: 244 s → 624 s for
  30 days (~20 s per run, mostly importing Dagster and dbt).
- **Ingest matches columns by name against a contract** and fails loudly on missing or unexpected
  columns, inside a transaction (a failed load keeps the previous good partition).
- **Staging never dedupes or filters**, so injected problems stay visible to tests.
- **Test failures don't stop the transform step**; the quality-check step reports them all, then
  fails the run. Model errors stop the run immediately.
- Fingerprints ignore `_loaded_at` and `_run_id`, so two correct runs compare equal.
