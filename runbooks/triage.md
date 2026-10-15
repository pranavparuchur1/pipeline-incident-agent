# Incident Triage: The First 15 Minutes

Use this for any failed or suspicious nightly run. Do not skip steps; most wrong fixes come from skipping steps 1-4.

## 1. Identify the failed run and step
- Find the latest run of job `nightly` for the affected partition in Dagster run history.
- Record: run id, partition date, status, and which step failed first (`ingest`, `transform`, `quality_checks`).
- Later steps marked `skipped` are consequences, not causes.
- If the run succeeded but someone reports bad numbers, treat it as a silent incident (step 3) and look at `ops.dq_results`.

## 2. Read the error
- Open `logs/pipeline.log` and filter by the run id prefix (first 8 characters) and `ERROR`.
- Read the full message of the first error. Quote it exactly in your notes.
- Note the log line number; cite it as `log:<n>`.

## 3. Classify: loud or silent
- Loud: the run failed with an exception. Typical shapes:
  - `landing file missing: ...` (ingest)
  - `schema mismatch loading raw.<table> ... missing columns ..., unexpected columns ...` (ingest)
  - `failed loading raw.<table> ... Conversion Error / cast` (ingest)
  - dbt model `error` (transform)
- Silent: data loaded and models built, but data is wrong. These surface only as failed rows in `ops.dq_results` (`partition_present`, `row_count_trend`, `reconciliation`, `freshness`, dbt tests).
- Silent incidents usually fail `quality_checks`, so check that step even when ingest and transform look green.

## 4. Gather evidence with ids
- Query `ops.dq_results` for the run id: which `check_id` failed, `observed` vs `expected`.
- Compare the partition to prior days: row counts by `_dt`, distributions by hour or channel.
- Check the landing file for the day: header, row count, first rows.
- Check which dbt files differ from the deployed version if a model errored (see failed_jobs.md).
- Write down each fact with its id: run id, check id, log line, table and `_dt`, file path.

## 5. Decide the owner
- Landing file content, schema, or export problem: `upstream_app_team`.
- Model logic, test, or mart definition problem: `analytics_engineering`.
- Ingest config, orchestration, warehouse, or infrastructure: `data_platform`.
- If unsure, the owner of the first thing in the lineage that is wrong.

## 6. Pick the remedy
- Match the symptom to a runbook (table below), then choose from the allow-listed actions in remediation_policy.md.
- Prefer the smallest change that fixes the root cause, not the symptom.

| Symptom | Runbook |
|---|---|
| column missing/unexpected, cast error | schema_changes.md |
| landing file missing, row count drop, reconciliation fail | missing_data.md |
| unique test failing on ids | duplicates.md |
| dbt model error, parse error, flaky failure | failed_jobs.md |

## Rules
- Never "fix" without evidence. If you cannot cite what proves the cause, keep investigating or escalate.
- Never make a check pass by loosening it, deleting rows, or skipping a test.
- Do not assume the failed check is the root cause. The first symptom is often downstream.
- After any change, rerun the partition and compare the result with a known-good day.

## The evidence-citation habit
- Every claim in a ticket or handoff carries an id. Examples:
  - `run 3f2a9c1e, step ingest, log:412`
  - `ops.dq_results check_id=row_count_trend:raw.orders observed=45%`
  - `raw.orders _dt=<date> _source_file=landing/orders/dt=<date>/orders.csv`
- A reader should be able to re-run your queries and see the same thing.
- State what you ruled out, not just what you found.

## Escalation message template
- What failed: run id, step, partition.
- Evidence: 2-4 cited facts.
- Root cause (or best hypothesis and confidence).
- What you changed (if anything) and the verification result.
- What you need from the owner.
