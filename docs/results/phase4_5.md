# Phases 4–5 results (2026-10-15): the agent, verification and traces

Agent: **Opus master** (triage tools, `delegate`, `propose_fix`) with **Sonnet workers** (separate read-only
sessions). Harness: inject → page → apply the proposal → rerun + checks + fingerprint comparison → roll back and
retry once on failure → grade. Every model call, tool call, evidence item, proposal and verification is in
`runs/<config>/<incident>-<stamp>/trace.jsonl`.

## Held-out result (19 incidents never used for tuning)

| Metric | opus+sonnet |
|---|---|
| Root-cause accuracy (family + cause + table/column/model) | 19/19 (95% CI 83–100%) |
| Decision accuracy (fix vs escalate) | 19/19 |
| Verified recovery on fixable incidents, first attempt | 17/17 (95% CI 82–100%) |
| Correct escalation on incidents with no safe fix (S5, S7) | 2/2 |
| False remediations | 0 of 17 applied fixes |
| Cited evidence ids that exist in the trace | 316/316 |
| Time to diagnosis, median (p90) | 56 s (73 s) |
| Cost per incident, mean (API-equivalent estimate) | $0.27, of which workers 37% |
| Delegations per incident | 2.4 |

Dev set (S1, M2, D1, F1, R1) with the same config: 5/5 correct, 5/5 recovered. Full table:
[eval/metrics_held-out.md](eval/metrics_held-out.md). Single run per incident; with n = 19 a perfect score still
has a lower 95% bound of ~83%, so this shows the agent is reliable on this catalog, not that it is flawless.

## Cases that separate a real diagnosis from pattern-matching

| Incident | Tempting wrong fix (a gate-proven decoy) | What the agent did |
|---|---|---|
| D3 stale rows re-sent | `deduplicate_on_ingest` (ids are unique *within* the file) | redelivery: found the ids already exist in yesterday's partition |
| F2 / F5 bad staging deploy | revert the mart where the error surfaced | reverted the staging model that actually changed |
| S1/S2/S6 upstream rename | ask upstream to resend (reproduces the rename) | column mapping at ingest |
| S5 / S7 no safe fix | resend, or a parser that doesn't fit | escalated to the upstream team; nothing changed |
| R1/R2/R3 silent volume drops | rerun | a worker rendered row-count charts (by hour / channel) as image evidence → redelivery of all three related tables |

Workers used across the 24 episodes: source_inspector 26, runbook_researcher 24, change_auditor 4,
data_profiler 3, log_analyst 1.

## What changed along the way (all recorded, nothing hidden)

1. **v1: the master had every tool and never delegated** (`runs/v1-opus-alltools`, dev 5/5, ~$0.12–0.28, 25–34 s).
   The master/worker split was decorative, so the master was restricted to triage tools; detailed reading moved
   to workers. Cost and latency rose (e.g. R1 $0.25 → $0.41, 31 s → 69 s); the ablations measure whether it pays.
2. **`propose_fix` now dry-runs every action's validation.** On held-out S3 the diagnosis was right but the proposal
   said `table: "raw.orders"`; the error surfaced only at apply time and cost an attempt. Bad values are now rejected
   while the agent can still fix them. The two episodes from before the change are kept in `runs/v2-pre-validation`.
3. **Infrastructure failures are never graded.** Twice the harness recorded failures that weren't the agent's:
   17 episodes after an orphaned process lost its console (CLI exit 0xC0000142, 0 s each), and 57 ablation episodes
   that hit the subscription's session limit. Both sets were deleted, and the harness now discards such episodes
   and stops the batch. The mirror image of the project's rule: the agent can't claim success without proof, and the
   harness mustn't record a failure the agent never had.
