# Phase 0 results (2026-10-14)

`uv run python scripts/smoke.py` → **6/6 passed**.

| Check | Result |
|---|---|
| duckdb query | `(1000, 499500)` as expected |
| dbt-core + dbt-duckdb | dbt 1.12.6, duckdb adapter 1.11.0 |
| dagster job executes | dagster 1.13.26, in-process run succeeded |
| master (Opus) text | `claude-opus-5-5` → `ready` |
| worker (Sonnet) structured JSON | `{"failed_task": "load_orders", "error_class": "duckdb.BinderException"}` |
| worker reads chart image | truth `09-17`, ~69% drop → `{"drop_date_mm_dd": "09-17", "approx_drop_pct": 69}` |

## Cost of a call

API-equivalent dollars = what the same tokens would cost on the API. On a subscription this is
an estimate, not a charge; the binding constraint is the plan's usage limits.

| Call | Model | Cache write tokens | Output tokens | Wall time | API-equiv. |
|---|---|---|---|---|---|
| CLI defaults, one-word reply | Opus | n/a | n/a | n/a | $0.188 |
| master text (trimmed flags) | Opus | 852 | 4 | 3.5 s | $0.0069 |
| worker JSON (trimmed flags) | Sonnet | 1,478 | 88 | 1.0 s | $0.0068 |
| worker image (trimmed flags) | Sonnet | 2,010 | 134 | 3.4 s | $0.0094 |

Replacing Claude Code's default system prompt and disabling its built-in tools cut a trivial call
by about 27×. Single runs, so the timings are indicative only.

## Notes

- `--bare` would trim further but only accepts API-key auth, so it can't be used on a subscription.
- The npm `claude.cmd` shim goes through cmd.exe, which mangles long arguments; the client calls
  the native `claude.exe` behind it directly.
- Unit tests: 8 (mocked; no model calls).
