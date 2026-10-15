# Foundations: data pipelines, agents, and harness engineering

Written for someone who knows Python and nothing else in this project. Read it top to bottom
once; later phases build on every section here.

Contents
1. The problem in one story
2. Data pipelines from zero
3. The tools: DuckDB, dbt, Dagster
4. What "breaks" in a pipeline (our five incident types)
5. What an LLM is, as a Python function
6. What an agent is: a loop
7. Harness engineering: everything except the model
8. The harness, piece by piece, mapped to this project
9. Walkthrough of the Phase 0 code
10. What Phase 1 builds and why
11. Glossary
12. Self-test

---

## 1. The problem in one story

It's 3 a.m. A company's nightly pipeline, the code that copies yesterday's orders out of the
shop's database and turns them into the revenue tables the CEO reads at 9 a.m., has failed.
A data engineer gets paged, opens the logs, finds an error such as
`column "order_total" not found`, works out that the shop's developers renamed that column to
`total_amount` yesterday, patches the pipeline, reruns it, checks that the numbers look right,
and goes back to bed.

This project builds an AI that does that engineer's job: **investigate → diagnose → propose a
fix → prove the fix worked**. The last step is the point. An AI that *says* "fixed it!" is
worthless; ours only gets credit when the pipeline is rerun and the checks pass.

We can't break a real company's pipeline, so we build a **sandbox**: a small fake company with
a real pipeline, and code that deliberately breaks it in realistic ways (we call those
*incidents*). Because we caused each break, we know the true answer and can grade the AI exactly.

## 2. Data pipelines from zero

You know Python lists and dicts. A **table** is a list of dicts that all have the same keys:

```python
orders = [
    {"order_id": 1, "customer_id": 7, "order_total": 59.90, "order_date": "2026-09-16"},
    {"order_id": 2, "customer_id": 3, "order_total": 12.00, "order_date": "2026-09-16"},
]
```

The keys are **columns**, each dict is a **row**, and the list of column names with their
types (`order_id: int`, `order_total: float`, ...) is the table's **schema**.

**SQL** is the language for asking questions of tables. This SQL:

```sql
select order_date, sum(order_total) as revenue
from orders
group by order_date
```

does what this Python does:

```python
from collections import defaultdict
revenue = defaultdict(float)
for row in orders:
    revenue[row["order_date"]] += row["order_total"]
```

A **data pipeline** is a chain of steps that moves data from where it's created to where it's
used, cleaning and reshaping it along the way. Ours has three layers, which is the standard
pattern:

```
shop database ──ingest──▶ RAW ──clean──▶ STAGING ──combine──▶ MARTS ──▶ dashboards
 (source)                 exact copy     fixed types,          business tables:
                          of source      renamed columns,      daily revenue,
                                         duplicates removed    customer lifetime value
```

Kitchen analogy: groceries are delivered (ingest → raw), washed and chopped (staging), and
cooked into dishes (marts). If the supplier changes the packaging, delivers twice, or skips a
day, the dish comes out wrong, sometimes loudly (the cook can't open the box: an error) and
sometimes silently (half the usual vegetables: no error, just a bad dish).

Two more words you'll see constantly:

- **Partition**: data split into chunks, usually one per day. "The 2026-09-17 partition" means
  that day's orders. Pipelines process one partition per run, so a missing day is a common incident.
- **Data-quality (DQ) check**: an assertion about data, exactly like a Python `assert`:
  `assert all(r["order_id"] is not None for r in orders)`. Typical ones: not null, unique,
  values within an allowed set, row count within normal range. Silent breaks are caught here.

## 3. The tools

**DuckDB**: a database that lives in a single file and runs inside your Python process. No
server and no installation beyond `pip`. It's like SQLite, but built for analytics (big scans
and sums). We use it as our fake company's **warehouse** (the database the analytics live in).

```python
import duckdb
con = duckdb.connect("warehouse.duckdb")
con.sql("select count(*) from orders").fetchone()   # -> (1000,)
```

**dbt** (data build tool): you write each table as a `.sql` file containing one `SELECT`.
dbt works out the order (staging before marts, because marts select from staging), runs them
as `CREATE TABLE ... AS SELECT ...`, and runs your DQ checks (dbt calls them *tests*,
declared in YAML). Each `.sql` file is a **model**. Think of dbt as `make` for SQL tables.
Real data teams use dbt everywhere, which is why we use the real thing rather than imitating it.

```sql
-- models/staging/stg_orders.sql
select order_id, customer_id, cast(order_total as double) as amount, order_date
from {{ source('raw', 'orders') }}
```

**Dagster**: an **orchestrator**, the conductor that runs pipeline steps in order, on a
schedule, and records what happened: which run, which step, success or failure, how long,
and the logs. When our agent calls `get_job_status()` or `search_logs()`, it is reading what
Dagster recorded. In Python it looks like decorated functions:

```python
import dagster as dg

@dg.op
def ingest_orders(): ...

@dg.op
def run_dbt(_after_ingest): ...

@dg.job
def nightly():
    run_dbt(ingest_orders())
```

**matplotlib**: draws charts. We render row-count trends as PNG images so the AI can *look* at
them, which is the "multimodal" part (section 8).

**uv**: installs Python packages and runs commands in the project's virtual environment.
`uv run pytest` means "run pytest with this project's exact package versions".

## 4. What breaks: our five incident families

| Incident | What happened | Loud or silent? | How a human finds it |
|---|---|---|---|
| Schema change | Source renamed/dropped/retyped a column | Loud: dbt errors | Error log names the missing column; compare schemas |
| Missing partition | A day's data never arrived | Usually silent | Row count 0 for a date; gap in the partition list |
| Duplicate records | Same rows loaded twice (a retry) | Silent | Uniqueness check fails; count doubles |
| Failed job | A step crashed (bad config, out of disk, timeout) | Loud | Job status = failed; stack trace in logs |
| Row-count drop | Upstream sent only part of the data | Silent | Count far below trend: easiest to *see* on a chart |

Note the "silent" ones. Nothing errors; only a check or a trend reveals them. That's why the
agent needs DQ checks and charts, not just logs.

## 5. What an LLM is, as a Python function

Strip away the mystique and a large language model is roughly this:

```python
def llm(system_prompt: str, messages: list[dict]) -> str:
    """Given instructions and a conversation, return the most plausible next message."""
```

Facts that matter for engineering around it:

- **It is stateless.** It remembers nothing between calls. A "conversation" is just you
  re-sending the whole history every time.
- **It only knows what's in the input** (plus what it learned in training). It cannot see your
  database unless you put the data into the prompt. The input is called the **context**, and
  it has a size limit measured in **tokens** (pieces of words; roughly 4 characters each).
- **It is not deterministic or guaranteed correct.** It can produce confident nonsense
  (**hallucination**). It can also claim it did something it didn't.
- **It costs money or quota per token**, in and out. Bigger models (Opus) reason better but
  cost more and are slower than smaller ones (Sonnet, Haiku).
- **Multimodal** models accept images as input as well as text.

## 6. What an agent is: a loop

A plain LLM call answers once. An **agent** is an LLM in a loop with **tools**: Python
functions the model may ask you to run. The model never runs anything itself. It writes
"please call `search_logs(query='ERROR')`", your code runs that function, and you send the
result back. This repeats until the model says it's done.

```python
def agent(task: str, tools: dict[str, callable]) -> str:
    history = [{"role": "user", "content": task}]
    for step in range(MAX_STEPS):                    # a budget, so it can't loop forever
        reply = llm(SYSTEM_PROMPT, history)          # model decides the next move
        if reply.wants_tool:
            fn = tools[reply.tool_name]              # only tools WE chose to offer
            result = fn(**reply.tool_args)           # WE run it, not the model
            history.append(tool_result(result))      # feed the evidence back
        else:
            return reply.text                        # final answer
    return "gave up: step budget exhausted"
```

Read those eight lines carefully: every interesting engineering decision sits in them. Which
tools are in `tools`? What does `SYSTEM_PROMPT` say? What is `MAX_STEPS`? What do you do with
the final answer: trust it, or check it? Those questions *are* harness engineering.

## 7. Harness engineering: everything except the model

> **Model + harness = agent.** The model is the engine. The harness is the rest of the car:
> steering, brakes, dashboard, seatbelts, and the test track.

You don't train the model, and you can't change how it thinks. Almost all of the reliability
of an AI system comes from the code around it, and that code is the harness. Harness engineering
is the craft of designing that code so an unreliable component (the model) yields a reliable
system. It's the same move as building reliable systems from unreliable networks or disks:
you add checks, limits, retries and records.

The harness answers ten questions:

| # | Question | Harness component | Where in this project |
|---|---|---|---|
| 1 | How do we call the model, and what happens when that fails? | Model access layer | `llm/claude_cli.py` (Phase 0) |
| 2 | What exactly does the model see? | Context engineering (system prompt, evidence selection) | Phases 0, 4 |
| 3 | What can the model do? | Tools | Phase 3 |
| 4 | What is the model *not allowed* to do? | Permissions / guardrails / sandbox | Phase 3 |
| 5 | How do we get answers code can check? | Structured output (JSON Schema) | Phase 0 (proved), Phase 4 |
| 6 | Which model does which job? | Orchestration: master/worker routing | Phase 4 |
| 7 | How do we know it's actually right? | **Verification against the world** | Phase 5 |
| 8 | What happened, and why? | Traces / observability | Phase 5 |
| 9 | How good is it, in numbers? | Evals with ground truth | Phase 6 |
| 10 | What does it cost, and can others rerun it? | Budgets, recording/replay | Phases 0, 6, 7 |

## 8. The harness, piece by piece

### 8.1 Model access layer
A thin, boring, well-tested wrapper that every model call goes through. It handles launching
the call, parsing the reply, raising clear errors (expired login!), and recording tokens,
time and cost. One choke point means one place to add tracing, caching or replay later.
Phase 0's `ClaudeCLI.run()` is this layer.

### 8.2 Context engineering
The model only knows what you send it, so choosing what to send is a design job. Too
little and it guesses; too much and it gets distracted, slow and expensive. In Phase 0 we
**removed** context: Claude Code normally sends a long default system prompt describing its
own coding tools, which is irrelevant here. Replacing it with one short line cut a trivial call
from about $0.19 to about $0.007 API-equivalent (~27×). In the agent, workers will condense
5,000 log lines into "3 relevant errors, with line ids" so the master's context stays small
and on-topic.

### 8.3 Tools
Tools are how the agent touches the world. Good tools are **narrow** (do one thing), **typed**
(clear arguments), and **return evidence with ids** (`log line #4812`) so a claim can be
traced back. Ours will be `get_job_status`, `search_logs`, `inspect_schema`,
`run_data_quality_checks`, `query_table`, `render_chart`, `search_runbooks`, `propose_fix`.

### 8.4 Permissions and the sandbox
Principle of **least privilege**: give each component only the powers it needs. Most tools
are **read-only**: they can look but not change anything. Only the master gets `propose_fix`,
which accepts only actions from an **allow-list** (for example `backfill_partition`,
`dedupe_table`, `apply_column_mapping`) and only touches the sandbox. Workers *cannot* fix
things even if they "want" to, because the harness doesn't hand them the tool. Never rely on
the prompt ("please don't delete tables") for safety. Rely on what the code allows.

### 8.5 Structured output
Free text like "I think it's probably the schema thing" can't be graded by code. With a
**JSON Schema** you force the answer into a shape:

```json
{"root_cause": "schema_change", "evidence": ["log:4812", "schema:raw.orders"],
 "confidence": 0.85, "proposed_action": {"type": "apply_column_mapping", "from": "order_total", "to": "total_amount"}}
```

Now Python can compare `root_cause` with the ground truth, check the cited evidence ids
exist, and execute the action. Phase 0 proved the CLI enforces schemas (`--json-schema`).

### 8.6 Orchestration: master and workers
Different jobs need different brains. Pay for the expensive one only where it matters:

```
                ┌──────────── MASTER (Opus) ────────────┐
                │ plans the investigation, weighs       │
                │ evidence, picks root cause, decides   │
                │ whether to fix (only one with         │
                │ propose_fix)                          │
                └───┬──────────┬──────────┬──────────┬──┘
                    ▼          ▼          ▼          ▼
            WORKER (Sonnet) WORKER     WORKER      WORKER
            summarize logs  diff       read DQ     read a chart
                            schemas    results     image
```

Workers do narrow, repetitive jobs with clear inputs and outputs; their condensed findings go
up to the master. This saves cost (Opus usage is the scarce resource on a Pro plan), keeps
the master's context clean, and lets workers run in parallel. We will *measure* whether the
split is worth it (Phase 6 ablation: Sonnet-only master, no workers).

The same pattern was used to *build* this project: Opus (the main session) designed Phase 0
and wrote the client; a Sonnet sub-agent drafted the Phase 0 write-up; Opus then reviewed it,
found one unverified claim (what `--verbose` does), tested it against the real CLI, and
corrected the doc. That review step is the master's real job.

### 8.7 Multimodal evidence
Some incidents are easiest to *see*. A row-count drop is one dot falling off a cliff on a
chart. Workers receive the PNG directly. In Phase 0 we generated a chart with a drop planted
on 09-17, sent only the image (no numbers), and Sonnet answered `09-17, ~69%`. The truth was
`09-17, ~69%`.

### 8.8 Verification: the most important idea in this project
**The model's claim is not evidence; the world's state is.** After the agent proposes a fix:

```python
snapshot = sandbox.snapshot()              # so we can undo
sandbox.apply(diagnosis.proposed_action)    # do the repair (allow-listed action only)
run = sandbox.rerun(failed_job)             # actually rerun the pipeline
checks = sandbox.run_dq_checks()            # actually rerun the checks
recovered = run.succeeded and checks.all_passed and matches_ground_truth_data()
if not recovered:
    sandbox.restore(snapshot)               # a failed fix must not leave a mess
```

`recovered` is computed by Python from the sandbox, never taken from the model's text. A
fix that makes checks pass by *hiding* the problem (deleting all rows passes "no duplicates"!)
is a **false remediation**, and we measure it separately. That's why we also compare the
repaired data with what the correct data should be.

### 8.9 Traces
A **trace** is the complete record of one incident investigation: every model call (prompt,
reply, tokens, time), every tool call (arguments, result), and the final verification.
Every claim in a diagnosis points at evidence ids inside its trace. When the agent is wrong,
the trace tells you *why* (missed a log line? misread a chart? picked a bad fix?). The
dashboard in Phase 7 is basically a nice view over traces.

### 8.10 Evals
An **eval** is a test suite for the AI. Because we injected every incident, we know the true
root cause and the true fix, so grading is mechanical, not vibes. Metrics:

- **Root-cause accuracy**: % of incidents where `root_cause` matches the truth
- **Recovery rate**: % where the verified rerun passed
- **False-remediation rate**: % of applied fixes that were wrong or that hid the problem
- **Time to diagnosis**: wall-clock seconds
- **Cost per incident**: tokens and API-equivalent dollars

Two rules carried over from your earlier projects: keep a **held-out** set you don't tune on,
and **never claim a number you didn't measure**.

### 8.11 Budgets and replay
Every loop has a step budget, and every eval run has a cost budget (Pro limits are tight on
Opus). Every model call is recorded, so the dashboard and the eval report can be **replayed**
without calling the model, which means anyone who clones the repo can see the results without
a Claude login.

## 9. Walkthrough of the Phase 0 code

### `src/pipeline_incident_agent/llm/claude_cli.py`, the model access layer

We don't call an API. We run the `claude` program (Claude Code) as a **subprocess**: Python
starting another program and talking to it through text pipes, the way you'd type into a
terminal and read what it prints. Its `-p` ("print") flag is headless mode: take one input,
answer, exit.

```python
proc = subprocess.run(
    self.command(model, system_prompt, json_schema, effort),  # the program + its flags
    input=stdin,                # what we "type" into it: our message as JSON
    capture_output=True,        # collect what it prints instead of showing it
    text=True, encoding="utf-8",
    env=env,                    # environment variables WITHOUT ANTHROPIC_API_KEY
    cwd=self.workdir,           # an empty folder, so no project files leak in
    timeout=self.timeout_s,     # never hang forever
)
```

Each flag in `command()` is a harness decision:

| Flag | Harness reason |
|---|---|
| `--model opus` / `sonnet` | Routing: which brain does this job |
| `--system-prompt "..."` | Context: replace Claude Code's long default prompt with ours |
| `--tools ""` | Permissions: disable Claude Code's own tools (file edit, shell). Our agent will get only *our* tools |
| `--setting-sources ""`, `--strict-mcp-config` | Isolation: ignore your personal Claude settings and plugins, so every call is reproducible |
| `--no-session-persistence` | Stateless: don't save a chat history between calls |
| `--input-format/--output-format stream-json` | One message format for text and images; machine-readable output |
| `--verbose` | Required by the CLI for stream-json output in print mode |
| `--json-schema` | Structured output |

Why strip `ANTHROPIC_API_KEY`? If that variable exists, the CLI bills the API instead of your
subscription. Removing it from the child's environment makes the wrong billing impossible,
not merely unlikely. That's a guardrail in code, not in a prompt.

How an image travels: the PNG's bytes are turned into text with **base64** (a way to write
binary data using only letters and digits) and placed in the message:

```python
{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0..."}}
```

The CLI prints many JSON lines (events); `parse_result_event()` keeps the final one of
`type: "result"`, and `to_llm_result()` turns it into a Python dataclass with text,
structured output, token counts, time and API-equivalent cost.

### `scripts/smoke.py`: a tiny eval
Each check has a known right answer computed by our code. The image check is the important
one: we *generate* the chart with the drop on a known date and grade the reply against it.
Generate the truth, then grade against it. That is exactly how the full eval will work, at
small scale.

### `tests/test_claude_cli.py`: unit tests without the model
These replace `subprocess.run` with a fake (**mocking**) so they test *our* parsing and
safety logic for free, in 0.15 s. The model is never called. Rule: test the harness
deterministically; evaluate the model statistically.

## 10. What Phase 1 builds and why

Phase 1 builds the fake company's working pipeline. No AI is involved yet, because the agent
can't be graded on a world that doesn't exist.

1. **Synthetic source data**: a generator for a small shop: customers, products, orders,
   order items, payments, one partition per day, with a fixed random seed so every run
   produces identical data.
2. **Raw → staging → marts in dbt**: about 8 models (for example `stg_orders`,
   `fct_orders`, `fct_daily_revenue`, `dim_customers`) with dbt tests (not null, unique,
   accepted values, relationships) and a row-count-vs-trend check.
3. **A Dagster job**: ingest a day's partition → `dbt build` → DQ checks, recording run
   status and structured logs that our tools will later read.
4. **Snapshot/restore**: copy the whole sandbox (warehouse file + run history) and put it back,
   so each incident starts from a clean, identical state. Verification depends on this.

Gate: a clean pipeline is green end to end, twice in a row, starting from a restore, and the
gate report shows the run log and check results.

## 11. Glossary

- **Agent**: an LLM in a loop that can call tools until it decides it's done.
- **Allow-list**: an explicit list of permitted actions; anything else is refused.
- **Base64**: encoding binary bytes as text so they can travel inside JSON.
- **Context / context window**: everything the model sees on one call; limited in tokens.
- **Data-quality (DQ) check**: an assertion about data (not null, unique, within range).
- **dbt model**: a `.sql` file with one SELECT that dbt materializes as a table or view.
- **Eval**: a test set with known answers, used to measure an AI system.
- **False remediation**: a "fix" that's wrong or that hides the problem.
- **Ground truth**: the known correct answer (we have it because we injected the incident).
- **Hallucination**: confident output that isn't true.
- **Harness**: all code around the model: access, context, tools, permissions, verification, traces, evals.
- **Headless**: a program run without its interactive interface (`claude -p`).
- **Held-out set**: eval cases never used while tuning, for an honest final score.
- **Incident**: a pipeline failure or silent data problem.
- **Least privilege**: give each component only the permissions it needs.
- **Marts**: final business-facing tables.
- **Master/worker (orchestrator/worker)**: one strong model coordinates; cheaper models do narrow subtasks.
- **Mock**: a fake replacement for a real component in a test.
- **Multimodal**: accepting more than one kind of input (text and images).
- **Orchestrator (data)**: a scheduler/runner of pipeline steps that records status and logs (Dagster).
- **Partition**: one chunk of a table, usually one day.
- **Raw / staging**: the exact-copy layer and the cleaned layer.
- **Replay**: serving recorded model calls instead of calling the model again.
- **Root cause**: the underlying reason for a failure, not the symptom.
- **Runbook**: a written procedure for handling a known kind of incident.
- **Sandbox**: an isolated copy of the environment where changes are safe and reversible.
- **Schema**: a table's column names and types.
- **Structured output**: model output forced into a JSON shape defined by a schema.
- **Subprocess**: a program started and controlled from Python.
- **Token**: the unit models read and write; roughly 4 characters of English.
- **Trace**: the full record of one agent run: every call, tool result and decision.
- **Verification loop**: apply fix → rerun → check → only then mark recovered.
- **Warehouse**: the analytics database (DuckDB here).

## 12. Self-test

1. In your own words, what is the difference between the *model* and the *harness*?
2. Which two of our five incident types are "loud", and why does the agent need DQ checks and
   charts for the others?
3. In the agent loop in section 6, who actually executes `search_logs`, the model or Python?
   Why does that matter for safety?
4. Why is "please don't change production tables" in the system prompt *not* a guardrail?
   What is the real guardrail in this project?
5. Why did removing context (the default system prompt) make the system better, not worse?
6. Give one job that belongs to the master and one that belongs to a worker, and say why.
7. A fix deletes every row of `fct_orders`, and afterwards the uniqueness check passes. Did
   the agent recover the pipeline? What do we call this, and how do we catch it?
8. Why does the smoke test *generate* the chart instead of using a chart from the internet?
9. Why do unit tests mock `subprocess.run` instead of calling Claude?
10. Why is stripping `ANTHROPIC_API_KEY` in code better than a README note saying
    "make sure no API key is set"?

<details><summary>Answers</summary>

1. The model turns input text/images into output text. The harness is all our code around it:
   how it's called, what it sees, which tools it gets, what it may not do, how its output is
   checked, recorded and scored.
2. Schema change and failed job raise errors. Missing partition, duplicates and row-count
   drops produce no error; only checks or trends reveal them.
3. Python. The model can only *request* a call, so it can only do what our offered tools
   allow, with arguments we can validate.
4. The model can ignore or misread a prompt. The real guardrail is code: workers aren't given
   `propose_fix`, the fix tool accepts only allow-listed actions, and it only touches the sandbox.
5. The default prompt described tools irrelevant to us: it cost tokens (~27× more) and
   could distract the model. Less, more relevant context is cheaper and usually better.
6. Master: decide the root cause across conflicting evidence (needs judgment). Worker:
   summarize 5,000 log lines into the relevant errors (narrow, repetitive, checkable).
7. No. It's a false remediation: the check passes because the problem was hidden. We catch it
   by also comparing the repaired data against the known-correct data, not just rerunning checks.
8. Because we must know the true answer to grade it mechanically; generating it means the
   ground truth is exact.
9. To test our parsing and safety logic deterministically, for free and fast. Model quality is
   measured separately by evals.
10. Code makes the mistake impossible; a note relies on someone remembering. Guardrails belong
    in code.

</details>
