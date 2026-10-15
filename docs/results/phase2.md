# Phase 2 results (2026-10-15)

Incident injector: 24 incidents in 5 families ([catalog](../incidents.md)), each with ground truth,
true fixes and decoy fixes, all on the incident day 2026-10-01 from the clean baseline.

## Gate

For every incident, from a fresh restore of the baseline:

- **break**: inject → run → the run fails at the expected step, with the expected evidence in its log lines / error / failed checks
- **true fix**: inject → apply → verify (rerun + all checks + every table equal to a clean run's fingerprint) → recovered
  (for escalate-only incidents: the escalation changes nothing and the pipeline is still failing)
- **decoy**: inject → apply a plausible wrong fix → verify → **not** recovered

| Run | Incidents | Trials | Wall time | Log |
|---|---|---|---|---|
| Pass 1 (original 16) | 16/16 | 59/59 | 1,107 s | [gate_run1.log](phase2/gate_run1.log) |
| Pass 2 (all 24, incl. 8 new) | 24/24 | 91/91 | 808 s | [gate_run2.log](phase2/gate_run2.log) |
| Pass 3 (the 8 new incidents again) | 8/8 | 32/32 | 296 s | [gate_run3.log](phase2/gate_run3.log) |

Every incident has passed the gate in two separate passes. Trial durations differ between passes because other work shared the CPU.

## What the first attempt caught

The first full pass (before fixes) scored 14/16. Both failures were wrong assumptions, found by the gate:

- **F2 (bad deploy in `stg_orders`)** doesn't fail at `transform` as assumed. The view still builds;
  the tests on the vanished column error, and dbt *skips* every downstream mart. The run fails at
  `quality_checks`, far from the model that changed. Catalog expectation corrected.
- **F3 (truncated CSV)**: DuckDB's CSV sniffer misread the damaged file and reported a misleading
  "schema mismatch" (the whole header as one column). Ingest now reads headers with Python's `csv`
  module and gives DuckDB an explicit dialect, so the error says what's wrong. Code fixed.
- **S7** was first written as a one-off file rewrite, so a redelivery would have silently "fixed" an
  incident labelled escalate-only. It's now a persistent upstream change, and redelivery is a decoy.

## Split (fixed before any agent run)

- Dev (may be used to tune prompts): S1, M2, D1, F1, R1, one per family.
- Held-out (final eval only): the other 19.

Unit tests: 99 passed (sandbox, remediation layer, tools).
