# Upstream Schema Changes

Covers renamed, retyped (reformatted), added, and dropped columns in landing files. Contract: see architecture.md.

## How ingest detects them
- Ingest reads the landing header and compares column names to the raw contract.
- Any mismatch fails `ingest` with this shape:
  `schema mismatch loading raw.<table> from <table>.csv: missing columns [...], unexpected columns [...]`
- Value problems fail later in the same step with a cast error:
  `failed loading raw.<table> from <table>.csv: Conversion Error: Could not convert string '$59.90' to DECIMAL`

## Symptom to cause
| Error shape | Likely change |
|---|---|
| one missing column AND one unexpected column | rename |
| unexpected column only, no missing | new column added upstream |
| missing column only | column dropped upstream |
| cast / conversion error on a value like `$12.50` | value reformatted (e.g. currency symbol) |

## Confirm before acting
1. Read the landing header for the failing partition and compare it with the contract list.
2. Compare with the previous day's file header. If the previous day matches the contract, the change is effective from this day.
3. For a suspected rename, check the values: same type and range as the old column? Same position in the file?
4. For a type change, look at 5-10 raw values from the landing file.
5. Record: table, file path, old vs new header, first partition affected.

## Safe to adapt in ingest (data_platform)
- Renamed column, same meaning: `apply_column_mapping` (source name -> contract name).
- Reformatted value (e.g. `$59.90` instead of `59.90`): `set_column_parser` with the `strip_currency` parser on that contract column.
- New column that is not in the contract: `ignore_extra_columns`. Contract columns cannot be ignored.
- After each change, rerun the partition and verify (see remediation_policy.md).
- Only adapt when meaning is unchanged. If a rename also changes units, semantics, or granularity, escalate instead.

## Requires escalation
- A required contract column was dropped and no other column carries its value. There is nothing to map; we cannot invent data.
- The new column looks like it replaces a dropped one but is not clearly equivalent.
- Any change that alters the meaning of a metric (currency, timezone, amount including tax).
- Escalate to `upstream_app_team` to restore the column or confirm the new definition; notify `analytics_engineering` if marts are affected.
- Do not backfill a dropped required column with defaults or NULLs to get the load through.

## Why asking upstream to resend does NOT fix this
- Renames, new columns, drops and reformatting are deliberate, persistent upstream changes applied to every export from an effective day onward.
- A redelivery re-runs the export and reproduces the same changed file, with the same header and formats.
- `request_upstream_redelivery` is for one-off export glitches only (missing, truncated, duplicated files).
- If the change is wrong from upstream's point of view, that is a conversation with `upstream_app_team`, not a resend.

## Verification
- Rerun the failed partition. `ingest` must load all five tables with plausible row counts.
- Check `ops.dq_results` for the new run: all checks pass.
- Spot-check that the adapted column has real values, not NULL, in `raw.<table>` for the `_dt`.
- Check earlier partitions are unchanged (adaptations apply to every load, including reruns of old days).

## Pitfalls
- Mapping the wrong source column silently loads wrong data without any error. Confirm meaning first.
- A strict contract is intentional: the load fails loudly so bad data never reaches the marts. Do not loosen it.
- A single table can have several changes at once; fix one, rerun, and read the next error.
