# Phases 4 and 5: the agent, the verification loop and the traces

Written for someone who knows Python and has read [00-foundations.md](00-foundations.md) and
[phase3.md](phase3.md). Phase 3 built the tools and the fences (read-only connections, roles, evidence ids,
the trace). This phase puts a model behind those tools, wraps it in a loop that judges it from the outside,
and measures how well it does on incidents it was never tuned on.

Contents
1. What these phases built
2. Master and workers: who does what, and why
3. Workers as separate processes
4. The v1 finding: a master with every tool never delegated
5. The diagnosis schema and the fixed taxonomy
6. The episode loop, step by step
7. Why the agent never judges its own success
8. Traces and evidence citation
9. The held-out results, and how to read a confidence interval
10. Cases that separate diagnosis from pattern-matching
11. Three harness bugs and what each one teaches
12. Glossary
13. Self-test

---

## 1. What these phases built

From `docs/results/phase4_5.md`:

- **A master agent** (an Opus session) with triage tools, `delegate` and `propose_fix`.
- **Workers** (Sonnet sessions) that the master hands reading tasks to. They can only read.
- **A structured diagnosis**: the master must end with a JSON object in a fixed shape.
- **The episode loop** (`agent/episode.py`): inject a break, page the agent, apply what it proposes,
  verify by rerunning, roll back and retry once if that failed, then grade.
- **Traces**: every model call, tool call, evidence item, proposal and verification written to
  `runs/<config>/<incident>-<stamp>/trace.jsonl`.

Analogy: Phase 3 gave the fire marshal a flashlight and a radio. Phase 4 hires the marshal and a small
team of scouts. Phase 5 is the inspector who arrives afterwards, lights the proposed fix, and checks whether
the building really is back to normal. The marshal does not get to say "all clear" on their own.

The one-line architecture, from `docs/architecture.md`: "The agent investigates and decides. The harness
acts and judges. Those two jobs never mix."

## 2. Master and workers: who does what, and why

Two kinds of work happen in an investigation, and they have different value per token.

- **Reading** is bulky: a landing file header, a page of SQL output, a dbt model diff, a runbook section,
  a chart. Most of it is irrelevant. It needs care but not much deep reasoning.
- **Judgement** is small: given what was found, what is the root cause, which of the allowed repairs fits,
  is there a safe repair at all? A wrong answer here costs the most.

So the project pays for the expensive model (Opus) only where judgement is, and sends the reading to a
cheaper one (Sonnet). Think of a senior engineer who does not scroll through raw logs themselves; they
ask a junior to look and report back, then decide. The comment in `agent/config.py` says it directly:

```python
# What the master can call itself when it has workers. Everything else (landing files, SQL, charts, model
# diffs, runbooks) is read by workers, so the expensive model spends its tokens on judgement, not reading.
MASTER_TRIAGE_TOOLS = {"get_job_status", "search_logs", "run_data_quality_checks", "list_changed_models",
                       "get_ingest_config", "delegate", "propose_fix"}
```

Five **triage** tools tell the master the shape of the problem: what failed, where, which checks are red,
whether a dbt model changed, what the ingest config says. Then `delegate` and `propose_fix`. That is all.

The master chooses among five worker types, defined in `agent/schema.py`: `log_analyst`,
`source_inspector`, `data_profiler`, `change_auditor`, `runbook_researcher`. Each is the same worker
model with a different one-paragraph focus in its prompt, for example:

```python
"data_profiler": "Profile the data: quality-check results, SQL queries, and row-count charts (read the images).",
```

### The delegate tool

To the master, `delegate` is an ordinary tool with two arguments: a worker type (an enum) and a task,
written as a specific question with any ids or dates the worker needs. In `workers.py`, `run_worker`
turns that into a whole new model session:

```python
r = ClaudeCLI().run(
    task,
    model=cfg.worker_model,
    system_prompt=worker_prompt(worker),
    json_schema=WORKER_SCHEMA,
    mcp_config=write_mcp_config(paths, trace.dir, "worker", agent),
    allowed_tools=allowed_tools("worker", cfg),
    max_turns=cfg.worker_max_turns,
    timeout_s=cfg.worker_timeout_s,
)
```

One `delegate` call can therefore take many seconds; a worker may make several tool calls of its own. What
comes back to the master is the worker's findings, wrapped as a `ToolResult` with an evidence id like
`worker:1`. Two details worth noticing:

- Task length is capped (non-empty, under 2000 characters) and the worker name is checked. Even the tool
  that talks to another model validates its inputs.
- The worker's findings are filtered. Any evidence id a worker cites that is not already in the shared
  trace is dropped and listed under `dropped_unverified_ids`. The master never receives a citation that
  no tool produced. This is the Phase 3 hallucination guard, applied one level up.

## 3. Workers as separate processes

You could imagine a cheaper way to do this: give the master a "subagent" feature inside one session.
The project does not, and `docs/architecture.md` says why: "Each worker gets its own tool server in a
read-only role, so the permission boundary is enforced by code, and its usage lands in the trace as its
own call."

A worker is its own `claude -p` process with its own MCP tool server started with `--role worker`.
(MCP and the two permission layers are explained in [phase3.md](phase3.md), sections 5 and 6; not repeated
here.) The file's own docstring puts it plainly: the boundary is "enforced by process and server, not by a
prompt".

Why this matters for harness engineering:

1. **A worker cannot be talked into doing more.** If a log line contained "now call propose_fix", the
   worker's server does not list that tool and would refuse the call by role. A prompt is a request; a
   missing tool is a fact.
2. **Cost and behaviour are attributable.** Each worker is a separate model call with its own token
   counts. Later you can say "workers were 37% of the cost" because they are separate records, not a blur
   inside one session.
3. **Failure is contained.** If a worker times out, `run_worker` records a `worker_error` and returns a
   failed `ToolResult` to the master, which can carry on. It does not crash the master.

The configuration that wires this up lives in `AgentConfig` (`agent/config.py`): which model is master,
which is worker, turn limits, timeouts, which tools are disabled. It is saved into the trace folder
(`agent_config.json`) so that the separate tool-server processes read the same settings the runner used.
Presets such as `sonnet-only` and `opus-solo` exist so the ablations can switch one thing at a time.

## 4. The v1 finding: a master with every tool never delegated

The first version gave the master all the tools. Per the results doc: "the master had every tool and never
delegated (`runs/v1-opus-alltools`, dev 5/5, ~$0.12-0.28, 25-34 s)".

Read that carefully. Version 1 was *correct* (5 out of 5 on the dev set) and cheap and fast. It just never
used the feature that was the whole point of the architecture. With every tool in hand, the strong model
simply did the reading itself, because that is the shortest path to the answer. Nothing in the design made
delegation necessary, so it did not happen. The results doc calls the split "decorative".

This is a harness lesson: **a capability the model can skip will be skipped when skipping is easier.** If you
want a behaviour you must change what is possible, not hope the prompt wins. So the master's toolset was cut
to triage tools plus `delegate` and `propose_fix`. To see a landing file at all, it must ask a worker.

### The honest trade-off

The results doc does not hide what this cost: "Cost and latency rose (e.g. R1 $0.25 -> $0.41, 31 s -> 69 s);
the ablations measure whether it pays."

So the change made the system slower and dearer on R1, and the doc does not claim a free lunch. The
reasons for doing it anyway are that an all-tools master does not exercise the design being studied, and that
whether the split earns its cost is an empirical question for the Phase 6 ablations (`sonnet-only`,
`opus-solo`, and so on), not something to assume. Keep both halves in your head: this phase *builds* the
master/worker split and shows it works; it does not yet *prove* it is better value than one strong model.

## 5. The diagnosis schema and the fixed taxonomy

The master does not finish with a paragraph. It must return JSON that fits `DIAGNOSIS_SCHEMA`:

```python
FAMILIES = ["schema_change", "missing_partition", "duplicates", "failed_job", "row_count_drop", "other"]
CAUSES = [
    "column_renamed", "column_type_changed", "column_added", "column_dropped",
    "file_missing", "file_empty", "partial_export", "filtered_export",
    "duplicate_rows_in_file", "stale_rows_resent",
    "bad_model_deploy", "corrupt_landing_file", "transient_failure", "other",
]
```

The `root_cause` object has a `family` and a `cause` taken from those lists (each is an `enum`), plus
nullable `table`, `column` and `model` fields and a free-text `explanation`. Beside it: a `summary`, a list
of `evidence` claims each with evidence ids, a `confidence` between 0 and 1, a `decision` of `fix` or
`escalate`, an `escalation_owner`, and `alternatives_considered`.

### Why a fixed vocabulary makes grading mechanical

Suppose the model could write the cause in its own words. Then "the orders export contained yesterday's rows
again" and "stale data resent upstream" are the same truth in different words, and judging them needs a
person or another model. Both are slow and both can be wrong.

With the closed list, grading is string equality. The ground truth for an incident is also a family, a cause
and the affected table/column/model. `root_cause_match` in `episode.py` is a few lines:

```python
family = rc.get("family") == truth["family"]
cause = rc.get("cause") == truth["cause"]
targets = {k: _norm(rc.get(k)) == _norm(truth[k]) for k in ("table", "column", "model") if k in truth}
return {..., "correct": family and cause and all(targets.values())}
```

"Correct" means family, cause and target all match. No judge model, no argument, same answer every time. The
`explanation` field is for humans; the enum fields are for the grader. This is the same idea as a multiple-
choice exam versus an essay: you give up some nuance in exchange for marks that nobody can dispute. The
price is that anything outside the taxonomy has to be squeezed into `other`, so the list must be chosen to
cover what the catalog contains.

The decision field gets the same treatment. `fix` or `escalate`; the grader compares it with what the
incident expects. And note the schema description of the field: "fix = you called propose_fix; escalate = no
safe automated fix". Escalating is a first-class answer, not a failure.

## 6. The episode loop, step by step

`run_episode` in `agent/episode.py` is the heart of Phase 5. Its docstring: "inject -> page the agent ->
apply its proposal -> verify -> (retry) -> grade". In order:

**1. Inject.** Restore the sandbox to a clean baseline, then apply the incident (`incident.inject`), then run
the pipeline for the incident day. If that run did not fail (or the process itself crashed), the episode is
thrown away with `InfrastructureError`; there is nothing valid to page about.

**2. Page with only what an alert would say.** The agent sees one sentence, built by `alert_text`:

```python
return (f"PAGE: Dagster job `nightly` FAILED for partition {incident_day} (run_id {run.run_id[:8]}, failed step "
        f"`{run.failed_step}`). Investigate, find the root cause, and decide the remediation.")
```

Date, run id, failed step. Not the incident name, not the true cause, not a hint. This is what a real pager
message looks like, and it keeps the test honest: anything the agent knows beyond this, it found with tools.

**3. Investigate.** `run_master` starts the Opus session with the master prompt, the diagnosis schema, and
the master tool server. The master reads triage output, delegates, reads findings, forms a view.

**4. Propose.** The master calls `propose_fix` once, or decides to escalate. The proposal is recorded in the
trace and nothing changes. The master then finishes with the structured diagnosis.

**5. The harness applies.** If the diagnosis says `fix` and there is a proposal, the harness runs each action
through `apply_action`, the same allow-listed remediation functions from Phase 2. If an action is invalid,
`apply_error` is recorded.

**6. Verify.** `verify(paths, day, ref)` reruns the pipeline and decides. From the architecture doc:
"recovered <=> rerun succeeds AND every check passes AND every table equals the clean run's fingerprint."
The last clause is the important one: "Checks can be satisfied by destroying data. A content hash of every
table against a clean run cannot." A fingerprint is a hash of each table's contents, computed from a run with
no incident. If your repair makes the checks pass but leaves one table subtly different, it fails.

**7. Roll back.** If verification failed, `restore(paths, "pre_fix")` puts the sandbox back to the state
before the repair, and the trace gets a `rollback` event. The agent's wrong move is undone.

**8. One retry with feedback.** `MAX_ATTEMPTS = 2`. The second prompt is the same page plus the previous
diagnosis, the proposed actions, and exactly what verification reported (failed checks, tables that differ
from the known-good run, errors). `feedback_text` ends: "Re-examine the evidence: the root cause may be
different from what was concluded." It gets one more go, not unlimited tries, so a lucky guess on the fifth
attempt cannot hide a poor first one. (The headline metrics below are about the *first* attempt for exactly
that reason.)

**9. Grade.** `grade()` compares the diagnosis with ground truth, checks the decision, counts false
remediations, and counts how many cited evidence ids exist in the trace. Then it appends `episode_end` and
writes `episode.json`.

The loop also stops early if the agent recovered, if it chose to escalate (nothing to apply), or if the
model call itself errored.

## 7. Why the agent never judges its own success

Look at where each judgement sits:

- *Did the repair work?* Decided by `verify`, using a rerun and fingerprints. The agent's confidence number
  plays no part.
- *Was the diagnosis right?* Decided by `root_cause_match` against ground truth the agent cannot reach
  (Phase 3: the answer key is not reachable through any tool).
- *Is a citation real?* Decided by `cited_ids_valid` against the trace.
- *Was a fix a false remediation?* Counted by `grade()`: a state-changing fix that did not recover, or any
  state change on an incident where escalation was right.

Why not simply ask the model "did it work?" Because a model asked to evaluate its own work is the least
independent examiner possible. It wrote the plan, it believes the plan, and it will find the output
plausible. A fluent "yes, resolved" costs nothing to produce. A table hash that matches costs a real
repair. The project's rule from foundations ("An AI that *says* fixed it is worthless") is made structural
here: the agent has no way to write a success into the record.

The `confidence` field is still collected, but only as something to report and to calibrate later. It is an
output to be graded, never an input to the grader.

## 8. Traces and evidence citation

Phase 3 introduced the trace as a flight recorder. Phase 5 is where it earns that name, because the whole
episode now lands in one file, across processes. Each line is an event with a `type`. The ones you will see
in a run:

| `type` | What it records |
|---|---|
| `episode_start`, `attempt_start` | the alert, which attempt |
| `tool_call` | role, tool, arguments, ok/not ok, summary, full evidence text, which agent |
| `llm_call` | model, tokens, cost estimate, turns, tools used, final text |
| `proposal` | the actions, rationale, cited ids |
| `diagnosis` | the structured diagnosis and seconds taken |
| `verification` | what was applied, whether it recovered, failed checks, differing tables |
| `rollback`, `episode_end` | undo, and the grade plus usage |

Reading a real trace (the D3 run under `runs/opus+sonnet/`) shows the design working. The master's first
four calls are triage tools (`get_job_status`, `run_data_quality_checks`, `list_changed_models`,
`get_ingest_config`). Then lines with `"role": "worker"` appear: `inspect_landing_file`, `query_table`. These
are the `source_inspector` worker, whose `llm_call` is recorded under the agent name
`worker1-source_inspector`. One of its SQL queries failed:

```
"summary": "query error: Binder Error: Referenced column \"_partition_date\" not found in FROM clause!"
```

The worker simply looked up the schema and tried again with the right column name. Nobody hid that wobble;
it is in the file. Then a second worker (`runbook_researcher`) searched the runbooks, and the master's
`delegate` calls close out. After that comes the `proposal`, the master's `llm_call`, the `diagnosis`, and
the `verification` line with `"recovered": true` and an empty `tables_differing_from_known_good`.

### Evidence citation, measured

Phase 3 made `propose_fix` refuse ids no tool returned. Phase 5 measures the same thing over the whole
diagnosis: `cited_ids_valid` collects every id in the diagnosis's evidence claims and checks each against
`trace.evidence()`. The held-out result is **316/316**. Every id the agent cited exists in the trace.

What that does and does not say. It says the agent did not invent citations. It does not say each cited
fact supports its claim; a real id can be attached to a wrong claim. Whether the *conclusion* is right is
the job of root-cause accuracy, which is graded separately. Two independent measures, each covering what
the other cannot.

## 9. The held-out results, and how to read a confidence interval

The incidents are split (Phase 2). Some were used while building and tuning the prompts and tools (dev:
S1, M2, D1, F1, R1). The **held-out** set, 19 incidents, was never used for tuning. Scores on it estimate
how the agent does on cases it was not shaped around.

| Metric | opus+sonnet |
|---|---|
| Root-cause accuracy (family + cause + table/column/model) | 19/19 (95% CI 83-100%) |
| Decision accuracy (fix vs escalate) | 19/19 |
| Verified recovery on fixable incidents, first attempt | 17/17 (95% CI 82-100%) |
| Correct escalation on incidents with no safe fix (S5, S7) | 2/2 |
| False remediations | 0 of 17 applied fixes |
| Cited evidence ids that exist in the trace | 316/316 |
| Time to diagnosis, median (p90) | 56 s (73 s) |
| Cost per incident, mean (API-equivalent estimate) | $0.27, of which workers 37% |
| Delegations per incident | 2.4 |

The dev set with the same configuration: 5/5 correct and 5/5 recovered. Note that 17 fixable plus 2
escalations makes 19.

### How to read "95% CI 83-100%" with n = 19

A score of 19 out of 19 sounds like 100%. It is the best result the test could produce, but it comes from
only 19 trials, and a different, larger set of incidents might contain cases the agent gets wrong. A
**confidence interval** says how much a small sample can tell you.

The reasoning, in plain terms: suppose the agent's true success rate were only 80%. How likely is it to get
19 right out of 19? Roughly 0.8 multiplied by itself 19 times, about 1.4%: very unlikely. Try 90%: about
13%, not that unlikely. So a true rate of 80% looks implausible after 19 straight successes, but 90% does
not. Keep going and you find the dividing line near 83%: below it, a perfect run is rare enough (under 5%)
that we stop believing it. That is the lower end of the interval. The upper end is 100% because nothing in
the data argues against it.

So "19/19, 95% CI 83-100%" means: "We saw no failures, and the data are consistent with a true success rate
anywhere from about 83% up to 100%." It does not mean 83% is the expected score; it means 83% is roughly the
floor we cannot rule out. The same applies to 17/17 (lower bound 82%). The project uses a Wilson interval
for this (`eval/report.py` per the architecture doc), which behaves sensibly at 0% and 100% where the simple
textbook formula does not.

The results doc states the sensible conclusion: "with n = 19 a perfect score still has a lower 95% bound of
~83%, so this shows the agent is reliable on this catalog, not that it is flawless." Two more limits to keep:
it is a single run per incident (a model can answer differently on a rerun), and the catalog is synthetic
and authored by the same project. The result is evidence about this benchmark, not a promise about real
production pipelines.

Other rows worth reading. **0 false remediations of 17 applied fixes** is a safety number: nothing the agent
changed made things worse or went unverified. **56 s median, 73 s at the 90th percentile** means nine runs in
ten finished diagnosis within about a minute (p90 means "90% of runs were this fast or faster"). **Cost
$0.27, workers 37%** shows where money goes; it is an "API-equivalent estimate" because the project runs on
a subscription and computes cost from token counts.

## 10. Cases that separate diagnosis from pattern-matching

A model that has seen many pipeline incidents could pattern-match: "duplicates, so deduplicate", "error in
the mart, so fix the mart", "rows missing, so rerun". The catalog is built with a **decoy fix** for several
incidents: a plausible repair that the Phase 2 gate proved does *not* restore correct data. Getting the right
answer there requires reading the evidence.

| Incident | Tempting wrong fix (a gate-proven decoy) | What the agent did |
|---|---|---|
| D3 stale rows re-sent | `deduplicate_on_ingest` (ids are unique *within* the file) | redelivery: found the ids already exist in yesterday's partition |
| F2 / F5 bad staging deploy | revert the mart where the error surfaced | reverted the staging model that actually changed |
| S1/S2/S6 upstream rename | ask upstream to resend (reproduces the rename) | column mapping at ingest |
| S5 / S7 no safe fix | resend, or a parser that doesn't fit | escalated to the upstream team; nothing changed |
| R1/R2/R3 silent volume drops | rerun | data_profiler worker read the row-count charts (by hour / channel) -> redelivery of all three related tables |

Going row by row:

- **D3.** The symptom is duplicate order ids, and the textbook fix is to deduplicate on ingest. But that
  fixes duplicates *inside one file*, and here the ids are unique within the file. They clash with
  yesterday's rows already in the warehouse. The upstream export resent old rows. In the trace, the source
  inspector joins today's partition to yesterday's in SQL and finds the overlap; the proposal is
  `request_upstream_redelivery` for `orders`. A pattern-matcher would have stopped at "duplicates".
- **F2 / F5.** The error appears in a mart, so reverting the mart is tempting. The mart is innocent; a
  staging model upstream of it was changed. This is why `change_auditor` and `list_changed_models` exist,
  and why the prompt says "revert the model that actually changed".
- **Renames.** If upstream renamed a column on purpose, asking them to resend just resends the renamed
  file. Redelivery is only a real fix for a one-off glitch. Persistent change means adapting the pipeline
  with a column mapping. The prompt encodes the rule ("Prefer adapting the pipeline when upstream changed
  something deliberately; ask for redelivery only for one-off delivery problems"), but the agent still has
  to decide which kind of event it is looking at.
- **Escalations (S5, S7).** The hardest skill for a fix-it agent is to leave things alone. Here no allowed
  action is safe, so the right answer is `escalate_to_owner` and no state change. Both were escalated
  correctly (2/2), and the false-remediation count stayed at zero. A system that always attempts a repair
  would score badly here.
- **Silent volume drops (R1/R2/R3).** No error, just too few rows. `rerun_only` does nothing useful because
  the missing data was never delivered. The `data_profiler` worker rendered row-count charts split by hour
  or channel and read the image, which is how the gap becomes visible (in the R2 trace, one worker call is
  `render_chart` with `"by": "channel"` (made by the source_inspector worker, alongside its file checks); that worker's summary says the files look filtered, with no `web`
  rows at all). The master then proposed redelivery of all three related tables, not just the one that
  triggered the alert. See [phase3.md](phase3.md), section 7, for why the split chart is the right tool.

The worker mix used across the 24 episodes is also informative: source_inspector 26, runbook_researcher 24,
change_auditor 4, data_profiler 3, log_analyst 1. Most incidents hinge on "what did upstream actually
deliver?" and "what do our procedures say?". The specialised workers were called rarely but exactly where the
decoys needed them.

## 11. Three harness bugs and what each one teaches

The results doc lists "what changed along the way (all recorded, nothing hidden)". Three of these are about
the harness, not the model.

### Bug 1: validation happened too late (the S3 case)

On held-out S3 the diagnosis was right, but the proposal named `table: "raw.orders"`. The remediation
actions expect a bare table name. The mistake was invisible to `propose_fix`, which checked only that the
action name and parameter names were right. It surfaced when the harness applied the action, which was after
the agent had finished, and it cost an attempt.

The fix: `propose_fix` now calls `validate_action`, which runs the action's real validation in a **dry run**.

```python
def validate_action(name: str, params: dict) -> str:
    """Run every check an action would run, without changing anything. Raises RemediationError."""
    token = _DRY_RUN.set(True)
    try:
        return apply_action(SandboxPaths(DBT_PROJECT_DIR.parent / "_dry_run_"), name, params)
    finally:
        _DRY_RUN.reset(token)
```

The comment at the top of the module explains: "every validation runs, the three places that write state
are skipped. Lets propose_fix reject bad parameter values at proposal time, while the agent can still
correct them."

Lesson: **validate at the moment the model can still react.** An error returned inside a tool call is
something the model reads and fixes in the same session. The same error discovered later, by the harness,
becomes a failed attempt. Tool validation should be as strict as the real action, and the cheapest way to
keep them identical is to run the real code with writes switched off, instead of writing a second,
slightly different checker. The results doc keeps the two episodes from before the change in
`runs/v2-pre-validation` rather than quietly deleting them.

### Bug 2: grading failures that were not the agent's

The results doc reports twice that the harness "recorded failures that weren't the agent's":

1. **17 episodes** failed because "an orphaned process lost its console (CLI exit 0xC0000142, 0 s each)".
   Each ended in zero seconds with no real investigation.
2. **57 ablation episodes** failed because they "hit the subscription's session limit".

Both sets were deleted. Had they been kept, they would have counted as the agent failing, dragging down its
numbers for reasons unrelated to its skill (and, in the second case, distorting the ablation comparison).
Now the code guards against it. In `run_ids`, after each episode the error text is scanned for markers:

```python
if any(m in errs for m in _LIMIT_MARKERS + _INFRA_MARKERS):
    # Not the agent's fault: discard the episode so it is rerun later, and stop the batch.
    shutil.rmtree(REPO_ROOT / ep.trace_dir, ignore_errors=True)
```

Limit markers include phrases like "session limit" and "rate limit"; infrastructure markers include "claude
exited" and "no result event". There is a careful distinction in the comments: an agent that runs out of
turns still returns a proper result and *is* graded, because that is a genuine agent failure. A process that
died is not. And the batch stops, instead of burning through the remaining incidents all failing the same
way.

Lesson, in the results doc's words: "The mirror image of the project's rule: the agent can't claim success
without proof, and the harness mustn't record a failure the agent never had." Honest measurement needs both
directions. A measurement that blames the agent for the weather is as wrong as one that lets it praise
itself.

### Bug 3 (the theme): the harness needs tests and skepticism too

Bugs 1 and 2 share a root. We spend most of our scepticism on the model, and the harness gets treated as
trusted plumbing. It is code, and code has bugs. The two harness problems above were found because someone
looked at results that seemed off (episodes lasting zero seconds, a correct diagnosis with a failed fix) and
asked whether the fault was in the agent or in the measuring stick. Habits to take away:

- When a batch result looks strange, check the infrastructure before you blame, or credit, the model.
- Make the harness unable to produce a result without evidence: infrastructure failures are discarded, not
  scored.
- Test the harness as seriously as the agent. Phase 3 already had a case of this (a library upgrade crashed
  the tool server while unit tests of the tool functions passed). Different layers need different tests.
- Keep the discarded and superseded runs identifiable (`v1-opus-alltools`, `v2-pre-validation`) so the
  history of how the numbers were obtained is auditable.

## 12. Glossary

- **Ablation**: running the system with one part removed or swapped (e.g. no workers) to measure what that
  part contributes. Phase 6 does these.
- **Alert / page**: the short message that starts an episode; here only date, run id and failed step.
- **API-equivalent cost**: a cost estimate computed from token counts at API prices, used because the runs are
  on a subscription.
- **Attempt**: one master investigation inside an episode; there are at most two.
- **Confidence interval (95% CI)**: a range of true success rates consistent with the observed result.
- **Decoy fix**: a plausible but wrong repair that the gate proved does not restore correct data.
- **Delegate**: the master's tool for handing a reading task to a worker session.
- **Diagnosis schema**: the fixed JSON shape of the master's final answer.
- **Dry run**: executing all of an action's validation with its state-changing steps skipped.
- **Episode**: one incident from injection to grade.
- **Escalate**: decide that no safe automated fix exists and hand the incident to a human owner.
- **False remediation**: a state-changing fix that did not recover the pipeline, or any state change when
  escalation was correct.
- **Fingerprint**: a content hash of every table, compared against a clean run to prove data is truly right.
- **Held-out set**: incidents never used while tuning, kept to estimate performance on unseen cases.
- **Infrastructure failure**: a failure of the tooling (crashed process, usage limit), not the agent; never
  graded.
- **Master / worker**: the expensive decision-making model / the cheaper, read-only evidence-gathering model.
- **p90**: the value 90% of runs were at or below.
- **Roll back**: restore the sandbox to its state before a failed fix.
- **Taxonomy (fixed)**: the closed lists of root-cause families and causes that make grading string equality.
- **Triage tools**: the small set of overview tools the master keeps for itself.
- **Wilson interval**: a method for computing a proportion's confidence interval that behaves at 0% and 100%.

## 13. Self-test

1. Why is the master an expensive model and the workers cheaper ones? Which kind of work goes to which?
2. Name two reasons workers are separate processes with their own read-only tool server, rather than
   instructions in the master's prompt.
3. What did the v1 runs show, what was changed, and what did the change cost? Does the data so far prove the
   split pays for itself?
4. How does a fixed list of causes make grading mechanical, and what is the price of that choice?
5. List the steps of an episode from injection to grade, and say what information the agent gets in the page.
6. Why is verification "rerun passes AND checks pass AND tables match the clean fingerprint" and not just
   "checks pass"? Why is the agent not allowed to say whether it succeeded?
7. A result is "19/19 (95% CI 83-100%)". What does it support saying, and what does it not support?
8. Name the three harness lessons from the changes section, one per bug, and give the evidence from the code
   or the results for each.

<details><summary>Answers</summary>

1. Reading (landing files, SQL, charts, diffs, runbooks) is bulky and needs care but little deep reasoning, so
   it goes to the cheaper Sonnet workers. Judgement (root cause, which allowed fix, whether to escalate) is
   small but costly if wrong, so the Opus master spends its tokens there.
2. Permissions are enforced by code (the worker's server doesn't list `propose_fix` and refuses it by role;
   the CLI allow-list is a second fence), so no text in a log or prompt can widen them. Also, each worker is a
   separate recorded model call, so cost and behaviour are attributable, and a worker failure is contained as a
   `worker_error` instead of crashing the master.
3. With every tool, the master never delegated (dev 5/5, about $0.12-0.28, 25-34 s), so the split was
   decorative. The master was limited to triage tools plus `delegate` and `propose_fix`. Cost and latency rose
   (e.g. R1 $0.25 to $0.41 and 31 s to 69 s). No: the ablations are what measure whether it pays; this phase only
   shows the design works.
4. Family and cause are enums, and ground truth uses the same values, so `root_cause_match` is plain equality
   on family, cause and table/column/model, with no judge needed and the same result every time. The price is
   less nuance: anything outside the lists must go in `other`, so the lists must cover the catalog.
5. Inject the incident and run the pipeline (discard if it does not fail); page; master investigates and
   delegates; propose; harness applies; verify by rerun, checks and fingerprints; on failure roll back and retry
   once with feedback; grade. The page holds only the partition date, the run id and the failed step.
6. Checks can be satisfied by destroying data, whereas a fingerprint of every table against a clean run cannot.
   The agent does not judge itself because a model's claim of success is free to produce and it is the least
   independent examiner; the harness measures instead, so success cannot be asserted without proof.
7. It supports: no failures were seen, and the true success rate is plausibly anywhere from about 83% to 100%;
   the agent is reliable on this catalog. It does not support: the agent is flawless, the expected score is 83%,
   or that it will do equally well on real pipelines (single run per incident; synthetic catalog).
8. (a) Validate when the model can still react: S3's `raw.orders` was only rejected at apply time until
   `propose_fix` dry-ran every action. (b) Never grade infrastructure failures: 17 episodes with exit 0xC0000142
   and 57 session-limit episodes were deleted, and `run_ids` now discards them and stops the batch. (c) The
   harness needs tests and scepticism as much as the agent: both problems were found by questioning odd results
   (0 s episodes, a right diagnosis with a failed fix), and superseded runs are kept in labelled folders.

</details>
