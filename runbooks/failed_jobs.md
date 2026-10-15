# Failed Jobs: Model Errors, Corrupt Files, Transient Failures

## Model errors after a deploy
Symptom: step `transform` fails; the log shows `dbt_build` errors such as a model `error`, with downstream models `skipped (upstream failure)`.

### Find the root cause
1. Read the dbt errors in `logs/pipeline.log` for the run (step=dbt_build). Note the first model with status `error`.
2. Check whether the data changed: did `ingest` succeed? Is the landing data the same shape as before? If yes, suspect the code.
3. Compare each dbt model file in the sandbox `dbt_project/models/` with the deployed (repo) version. The model whose file differs is the changed one.
4. Walk the DAG upstream (see architecture.md lineage). A symptom in a mart can have its root cause in a staging model, and a staging change can break several marts.
5. Confirm by reading the diff: a removed or renamed column, a changed expression, or a new filter.

### Remedy
- Revert the model that CHANGED, not the one that errored. Example: an error in `fct_orders` may come from an edited `stg_orders`.
- Use `revert_model_change` with the single model name; it restores that file to the deployed version.
- If several models changed, revert one at a time, rerunning to confirm which fixes the failure.
- If the change is intentional (a planned business-logic change), escalate to `analytics_engineering` instead of reverting.

### Also check
- Silent model changes: a model can build successfully but compute wrong numbers (e.g. a new filter lowers rows). Look at `reconciliation` and dbt test failures, then diff the models.
- Test failures do not stop `transform`; read `quality_checks` results too.

## Corrupt or truncated landing files
Symptom: `ingest` fails with `failed loading raw.<table> from <table>.csv:` followed by a CSV parse error (unterminated quoted value, wrong number of columns, unexpected end of file).

- Open the file: check the size, last lines, and whether the final row is cut off.
- Compare the header to the contract. If the header is fine and the tail is cut off, the export was interrupted.
- This is a one-off delivery glitch. Use `request_upstream_redelivery` for the day with the affected tables, then rerun.
- Do not edit the CSV by hand or truncate it to the last good row: a partial file means missing rows.
- If redelivery produces the same broken file, escalate to `upstream_app_team`.

## Transient failures
Symptom: failure with no data or code cause: lock/IO error, process crash, timeout, or a run that succeeded on previous days with identical inputs.

- Confirm the cause is gone first (disk space, file lock released, no other process holding the warehouse).
- Use `rerun_only` only when you can state why it will now succeed. Evidence: same inputs, error is environmental, condition cleared.
- If the rerun fails the same way, it was not transient. Re-triage from the first error.
- Do not rerun repeatedly hoping for a pass; reruns are idempotent but hide root causes.
- Platform-level problems (Dagster, warehouse, disk) go to `data_platform`.

## Pipeline process crashed (no run recorded)
- If the run result shows `pipeline process crashed`, read the tail of the error in the output; the run never reached Dagster steps.
- Typical causes: missing dependency, unreadable warehouse file, bad config. Owner: `data_platform`.

## Verification
- Rerun the partition: all three steps succeed, models built, tests pass.
- Compare model outputs (row counts, revenue) with a known-good day.
- Confirm the reverted or fixed model matches the deployed version.

## Escalation
- Model logic or intentional change: `analytics_engineering`.
- Export or file problem persisting after redelivery: `upstream_app_team`.
- Infrastructure or orchestration: `data_platform`.
