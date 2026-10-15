# pipeline-incident-agent

An AI incident engineer for data pipelines that **only gets credit when the sandbox proves its fix worked.**

A failed nightly run pages the agent. An Opus "master" triages and decides; Sonnet "workers" read logs, landing
files, SQL, dbt diffs, runbooks and row-count **charts (as images)** and report back with evidence ids. The master
writes a structured diagnosis and proposes a fix from a narrow allow-list. It applies nothing itself. The harness
applies the proposal in a sandbox, reruns the pipeline, and compares every table against a known-good run. If that
fails, it rolls back and gives the agent one more try.

## Results (held-out: 19 incidents never used for tuning)

| | Opus master + Sonnet workers |
|---|---|
| Root cause exactly right (family, cause, table/column/model) | **19/19** (95% CI 83–100%) |
| Fix verified by rerun + checks + table fingerprints, 1st attempt | **17/17** fixable incidents |
| Correctly escalated when no safe fix exists | **2/2** |
| Wrong fixes applied (false remediations) | **0** |
| Cited evidence ids that really exist in the trace | 316/316 |
| Time to diagnosis (median) | 56 s |
| Cost per incident (API-equivalent estimate) | $0.27 (37% spent by workers) |

Ablations on the same 19 incidents ([details](docs/results/phase6.md)):

| | Opus + Sonnet workers | Sonnet master + workers | Opus alone | No chart images |
|---|---|---|---|---|
| Exact root cause | 19/19 | 16/19 | 18/19 | 19/19 |
| Verified fixes | 17/17 | 17/17 | 17/17 | 17/17 |
| Cost / incident | $0.27 | $0.15 | $0.18 | $0.25 |
| Median time | 56 s | 47 s | 43 s | 59 s |

Every configuration fixed every fixable incident; the misses are labelling slips. On a sandbox this small, Opus
alone was cheaper and faster than Opus + workers with no measurable accuracy loss: the split is built for evidence
too large for one context, which this catalog doesn't reach.

One run per incident on a 24-incident catalog the author built, so read this as "reliable on these failure
classes", not "solves data incidents". Details and caveats: [docs/results/phase4_5.md](docs/results/phase4_5.md).

## How it works

```
landing CSVs → raw (DuckDB) → dbt staging → dbt marts → quality checks        ← Dagster job, one process per run
      ▲ incident injected                                    │ run fails
      │                                                      ▼
 harness ◄── proposal (nothing applied) ── Opus master ── delegate ──► Sonnet workers (read-only)
      │ apply allow-listed actions, rerun, compare every table with a clean run
      ▼
 recovered? ── no ──► roll back, one retry with the verification result
```

- **Sandbox** (`sandbox/`): deterministic synthetic shop data; by-name ingest contract; a dbt project with 28 tests;
  9 volume/freshness/reconciliation checks; snapshot/restore; content fingerprints of every table.
- **Incidents** (`incidents/`, [catalog](docs/incidents.md)): 24 across schema changes, missing partitions,
  duplicates, failed jobs (incl. bad dbt deploys) and silent row-count drops. Each has ground truth, true fixes and
  *decoy* fixes, and a gate proves all three (24/24, twice).
- **Tools** (`tools/`): 11 read-only tools that return evidence ids, served over MCP. Workers can't remediate: their
  tool server doesn't have `propose_fix`, and refuses it by role.
- **Agent** (`agent/`): master/worker prompts, diagnosis schema, delegation, the episode loop.
- **Traces** (`runs/`): every model call, tool call, piece of evidence, proposal and verification.
- **Dashboard** (`dashboard/`): static replay of the traces. No login or model calls needed.

More: [architecture](docs/architecture.md) · [build plan](docs/PLAN.md) · beginner write-ups in [docs/learn](docs/learn).

## Run it

Requirements: Windows/macOS/Linux, [uv](https://docs.astral.sh/uv/), and for the agent itself the
[Claude Code CLI](https://docs.claude.com/en/docs/claude-code) signed in with your own Claude account
(`claude auth login`). The agent runs on that subscription through `claude -p`, not the API.

On Windows, clone to a short path (e.g. `C:\src\pipeline-incident-agent`): some of Dagster's installed
files are deep enough to exceed the 260-character path limit under long parent folders.

```bash
uv sync
uv run pytest                                   # no model calls; tool tests skip until a baseline exists
uv run pia sandbox init                          # build 30 days of pipeline history (~10 min)
uv run pia incidents list
uv run pia incidents gate --ids S1,F1,M2         # prove incidents break, true fixes recover, decoys don't
uv run python scripts/smoke.py                   # check the Claude CLI: Opus, Sonnet, JSON, image input
uv run pia agent run S1                          # one full episode; trace in runs/opus+sonnet/
uv run python scripts/show_trace.py              # readable timeline of the newest episode
```

**See the results without running anything:** open `dashboard/index.html` (or serve the folder:
`python -m http.server -d dashboard`). It replays the committed traces.

Reproduce the evaluation: `bash scripts/run_eval.sh`, then `uv run pia eval report --set held-out`.
It skips episodes that already exist and stops cleanly if your plan's usage limit is reached.

## Decisions worth defending

- **The agent never judges its own success.** Verification is rerun + every check + every table's fingerprint equal
  to a clean run, so "delete rows until the unique test passes" can't count as a fix.
- **Least privilege in code, not prompts.** Read-only DuckDB with external access off; single-statement SELECT only;
  workers are separate processes with a read-only tool server; fixes come only from 8 validated actions.
- **Ground truth by construction.** Incidents are injected, so grading is mechanical; the dev/held-out split was
  fixed before any agent run; runbooks were written without access to the answer key.
- **Upstream changes vs one-off glitches are modelled.** Asking upstream to resend fixes a missing file but not a
  deliberate column rename, so the agent has to tell them apart.
- **Infrastructure failures are never graded.** Twice the harness wrongly recorded failures (an orphaned process; a
  subscription session limit). Both were caught, discarded, and are now detected automatically.

## Limitations

- One run per incident; n = 19 held-out. Intervals are wide; differences between configs under ~20 points are noise.
- The incident catalog was written by the same author as the agent. Real incidents are messier: multiple causes,
  partial evidence, flaky infrastructure.
- Cost is an API-equivalent estimate from token counts; the runs used a subscription.
- The sandbox is small (one shop, 30 days, ~14k orders) and single-machine.
