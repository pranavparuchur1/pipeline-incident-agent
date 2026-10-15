# Phase 6 results (2026-10-15): evaluation and ablations

Same 19 held-out incidents, one episode each, four configurations. 76 graded episodes, none with errors
(runs that hit the subscription's session limit were discarded and rerun; see [phase4_5.md](phase4_5.md)).
Full table with every metric: [eval/metrics_held-out.md](eval/metrics_held-out.md).

| | opus+sonnet (main) | sonnet-only (Sonnet master + workers) | opus-solo (Opus, all tools, no workers) | opus+sonnet, no images |
|---|---|---|---|---|
| Exact root cause | 19/19 (83–100%) | 16/19 (62–94%) | 18/19 (75–99%) | 19/19 (83–100%) |
| Root-cause family | 19/19 | 16/19 | 19/19 | 19/19 |
| Decision (fix vs escalate) | 19/19 | 19/19 | 19/19 | 19/19 |
| Verified recovery, 1st attempt | 17/17 | 17/17 | 17/17 | 17/17 |
| Correct escalation | 2/2 | 2/2 | 2/2 | 2/2 |
| False remediations | 0 | 0 | 0 | 0 |
| Time to diagnosis, median | 56 s | 47 s | 43 s | 59 s |
| Cost / incident (API-equiv.) | $0.27 | $0.15 | $0.18 | $0.25 |
| Share of cost spent by workers | 37% | 52% | – | 34% |
| Cited evidence ids that exist | 316/316 | 232/232 | 218/218 | 311/311 |

95% Wilson intervals in brackets. With n = 19, no difference in this table is statistically clear.

## What the misses were

All four misses are **classification slips with a correct, verified fix**, not wrong diagnoses of what to do:

- sonnet-only on F2, F4, F5 (bad dbt deploys): named the right model and cause (`bad_model_deploy`), reverted the right
  model, recovered; but filed the family as `schema_change` or `other` instead of `failed_job`, at confidence 0.88–0.93.
- opus-solo on S8 (new upstream column): right family and cause, right fix; left the `column` field empty.

## What this says (and doesn't)

1. **On this catalog, every configuration fixes everything.** The incidents are distinguishable enough that the
   remediation decision is easy for all of them; the real differences are in precision of the write-up and in cost.
2. **Workers did not buy accuracy here; they cost more and took longer.** Opus alone was 33% cheaper and ~13 s faster
   than Opus + workers for 18 vs 19 exact diagnoses (within noise). The master/worker split's value would show on
   larger evidence (thousands of log lines, many tables) where an Opus context can't hold everything; this sandbox
   is small enough that it can. That is a finding, not a failure: the design is measured, not assumed.
3. **Sonnet as master is the cheapest ($0.15) with the same fixes**, but its taxonomy labels were less reliable and
   it was confidently wrong (0.90 mean confidence on misses). If labels feed routing or reporting, that matters.
4. **Images didn't change outcomes.** The three silent volume drops were also visible through checks and SQL, so
   chart reading was a redundant path, not a necessary one, on these incidents.

## Not measured

Repeatability (each incident ran once per config), incidents with multiple simultaneous causes, noisy or misleading
evidence, and larger data. These are the next experiments if the catalog is extended.
