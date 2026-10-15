# Remediation Policy

Only the actions below may change the system. There is no arbitrary SQL, no "skip the test", and no manual edit of data. If no action fits, escalate.

## Allow-listed actions
| Action | Params | What it changes |
|---|---|---|
| `request_upstream_redelivery` | `tables` (list of event tables), `day` (YYYY-MM-DD) | Upstream re-exports the listed tables for that day; the landing files are rewritten correctly, including any persistent upstream changes. Event tables only: customers, orders, order_items, payments. |
| `apply_column_mapping` | `table`, `source_column`, `target_column` | Ingest reads the contract column `target_column` from the renamed upstream column `source_column`. Target must be in the raw contract. |
| `set_column_parser` | `table`, `column`, `parser` | Ingest parses a reformatted value with a named parser. Parser available: `strip_currency` (removes `$` and `,`). Column must be in the contract. |
| `ignore_extra_columns` | `table`, `columns` (list) | Ingest ignores new upstream columns. Refuses any contract column. |
| `deduplicate_on_ingest` | `table` (event table), `key` | Ingest keeps one row per `key` when loading the table. Persists for later loads. |
| `revert_model_change` | `model` | Restores one dbt model file to the deployed version. |
| `rerun_only` | none | Changes nothing; the pipeline is simply rerun. |
| `escalate_to_owner` | `owner`, `summary` | Hands the incident to a team; changes nothing. Owners: `upstream_app_team`, `analytics_engineering`, `data_platform`. |

Notes:
- Applying an action never reruns the pipeline. Verification is a separate step.
- Parameters are validated strictly (lowercase identifiers, known tables, real columns); bad parameters are rejected.

## Principles
1. Smallest change that addresses the root cause. One action per cause; do not stack speculative fixes.
2. Never apply a fix that only makes a check pass. Forbidden examples: loosening a threshold, removing a test, deleting failing rows, defaulting missing values to NULL or zero.
3. Every fix is verified: rerun the partition, confirm all checks pass, and compare with a known-good state (row counts, revenue, prior days).
4. Fix the cause, not the place where it surfaced. The error location in the DAG is often downstream of the cause.
5. Match the remedy to the failure class:
   - One-off delivery glitch (missing, truncated, duplicated file): redelivery of all affected related tables.
   - Persistent upstream change: adapt in ingest (mapping, parser, ignore) or escalate.
   - Bad deploy of a model: revert the model that changed.
   - Environmental flake: rerun, only after the cause has cleared.
6. Redelivery does not fix deliberate upstream changes; it reproduces them.
7. If the evidence is thin, gather more or escalate. Do not guess.

## Choosing quickly
| Finding | Action |
|---|---|
| landing file missing / row drop (partial or filtered) | `request_upstream_redelivery` (orders, order_items, payments) |
| corrupt or truncated file | `request_upstream_redelivery` |
| duplicates within one file | `deduplicate_on_ingest` or redelivery |
| stale rows from a previous day | `request_upstream_redelivery` |
| renamed column | `apply_column_mapping` |
| `$`-formatted amounts | `set_column_parser` (`strip_currency`) |
| new non-contract column | `ignore_extra_columns` |
| required column dropped, no source | `escalate_to_owner` (`upstream_app_team`) |
| changed dbt model broke build or numbers | `revert_model_change` |
| transient, cause cleared | `rerun_only` |
| nothing safe applies | `escalate_to_owner` |

## When to escalate
- No allow-listed action addresses the root cause.
- A fix would require inventing, guessing, or deleting data.
- A redelivery reproduces the same problem (persistent upstream behavior).
- The change looks intentional and business-relevant (model logic, metric definition).
- Evidence is inconclusive after the triage steps.

## Who to escalate to
- `upstream_app_team`: export content, schema decisions, dropped columns, persistent export bugs.
- `analytics_engineering`: dbt model or test logic, mart definitions, intentional model changes.
- `data_platform`: ingest code and config, Dagster, warehouse, infrastructure.
- The summary must include: what failed, cited evidence, the root cause or best hypothesis, what was tried, and what you need.

## Verification checklist
- Rerun the same partition date.
- Steps `ingest`, `transform`, `quality_checks` all succeed.
- `ops.dq_results` for the new run: no failures.
- Row counts and revenue within normal range compared with the trailing median.
- No regression in other partitions.
- Record the action, its parameters, the run ids before and after, and the evidence.
