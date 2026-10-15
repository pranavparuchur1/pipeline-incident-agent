# Build plan

An agent that diagnoses failed data pipelines and proposes fixes, where a fix only counts
if the sandbox confirms it worked.

## Fixed decisions

| Decision | Choice | Why |
|---|---|---|
| LLM access | Claude Code CLI in headless mode (`claude -p`), signed in with a Claude subscription | No API billing. `ANTHROPIC_API_KEY` is stripped from the child process so a stray key cannot switch billing. |
| Master model | Opus | Open-ended reasoning: weighs evidence, names the root cause, decides whether to remediate. |
| Worker model | Sonnet | Narrow, repeated jobs: summarize logs, diff schemas, read check results and charts, re-verify after repair. |
| Multimodal | Workers read rendered images (row-count trends, run timelines) as evidence | Some incidents (a row-count cliff) are faster to see than to query. Phase 0 proved a worker reads a planted drop date off a chart. |
| Warehouse | DuckDB file | Runs on Windows with no server or Docker. |
| Transformations | dbt-core + dbt-duckdb | The real tool data teams use; schema and test failures look real. |
| Orchestrator | Dagster (in process) | Real run/step status and logs for `get_job_status` / `search_logs`. |
| Cost metric | Tokens per incident + API-equivalent dollars, labelled as an estimate | A subscription has no per-call bill. |
| Reproducibility | Every LLM call is recorded; a replay mode serves the dashboard and eval report without a login | Cloners and reviewers don't need Claude access. |

## Phases (each ends at a gate with measured results and a beginner write-up)

0. **Scaffold + smoke.** uv project, stack installed, headless Claude client (text, JSON schema, images). Gate: `scripts/smoke.py` 6/6.
1. **Sandbox pipeline.** Synthetic e-commerce source data → raw → staging → marts in dbt; Dagster job that ingests partitions and runs dbt; data-quality checks; structured run logs; snapshot/restore of the whole sandbox. Gate: a clean run is green end to end, twice, from a restore.
2. **Incident injector.** Five families (upstream schema change, missing partition, duplicate records, failed job, row-count drop), several variants each, deterministic seeds, ground-truth label + the repair that truly fixes it. Gate: each variant reliably breaks the pipeline, and its true repair reliably makes it green again.
3. **Tools + runbooks.** `get_job_status`, `search_logs`, `inspect_schema`, `run_data_quality_checks`, `query_table`, `render_chart`, `search_runbooks`, `propose_fix`. Read-only tools are exposed to everyone; `propose_fix` only to the master and only against the sandbox, from an allow-list of remediation actions. Gate: tool unit tests; permission tests prove workers cannot remediate.
4. **Master/worker agent.** Opus plans and decides; Sonnet workers gather and condense evidence (text and images). Structured diagnosis: evidence (with ids), root cause, confidence, proposed action. Gate: diagnoses on a dev set of incidents.
5. **Verification loop + traces.** Apply the proposed fix in the sandbox, rerun the failing job and checks, and only mark "recovered" if they pass. Every diagnosis links to the log lines, SQL and check results behind it. Gate: no recovery is ever recorded without a passing rerun.
6. **Evaluation.** Held-out incident set; root-cause accuracy, recovery rate, false-remediation rate, time to diagnosis, tokens/cost per incident; ablations (Sonnet-only master, no workers, no images). Kept small because Pro limits are tight on Opus.
7. **Dashboard + write-up.** Incidents, evidence, decisions, recovery outcomes (works in replay mode); README, architecture, ADRs; clean-clone test; push.

## How the build itself is split

Opus (the main session) designs each phase and writes the agent, diagnosis and verification
core, and reviews everything. Sonnet subagents write well-specified pieces: incident variants,
runbooks, tests, dashboard UI, write-up drafts. Haiku subagents do bulk mechanical chores such as
checking that docs match code.
