# Phase 0 write-up: scaffold and smoke test

This is a learning document. It explains what Phase 0 of the pipeline-incident-agent project built, why each choice was made, and the numbers that were actually measured. It assumes you know basic programming but not data engineering or LLM tooling. Every term in **bold** is defined in the glossary near the end.

---

## 1. What this project is

Data teams run **pipelines**: automated jobs that move data from one place to another and reshape it along the way. Pipelines break. A column gets renamed upstream, a day of data never arrives, duplicate rows sneak in, a job crashes, or a table suddenly has a third of its usual rows. When that happens, a person has to read logs, look at tables, figure out the root cause, and fix it. This is called handling an **incident**.

This project builds an AI agent that does the diagnosing. The line from the plan that defines it:

> An agent that diagnoses failed data pipelines and proposes fixes, where a fix only counts if the sandbox confirms it worked.

The second half of that sentence is the important part. Language models can sound confident and be wrong. So the agent does not get credit for a diagnosis because it reads well. It gets credit only when its proposed fix is applied in a **sandbox** (a safe, disposable copy of the pipeline), the failing job is rerun, and the checks pass. Think of it like a mechanic who is only paid if the car actually starts afterwards.

### What Phase 0 set up

The plan splits the build into phases, each ending at a gate (a pass/fail condition) with measured results. Phase 0 is "Scaffold + smoke". Its gate is that `scripts/smoke.py` passes 6 out of 6 checks. Phase 0 did three things:

1. Created the project with `uv` and installed the data stack (DuckDB, dbt, Dagster, matplotlib).
2. Wrote a **headless Claude client**: a small Python class that calls Claude for text, for structured JSON, and for questions about images.
3. Wrote a **smoke test**, a quick script that proves all the pieces work together before any real building starts.

A **smoke test** is named after electronics: you power the device on and see if smoke comes out. It does not prove the system is correct. It proves the system is not obviously broken. That is exactly what you want before investing in later phases.

---

## 2. The stack, and why each piece

### The vocabulary first

- **Data pipeline.** A chain of steps that takes raw data and produces clean, usable tables. Analogy: a water treatment plant. Raw water comes in, goes through several stages, and clean water comes out. If one stage breaks, everything downstream is affected.
- **Orchestrator.** The program that runs the pipeline's steps in the right order, on a schedule, and records what happened (which step ran, succeeded, failed, and what it logged). Analogy: the plant's control room. It does not treat the water itself; it starts each stage, watches the gauges, and keeps a log.
- **dbt model.** In dbt, a model is a SQL `SELECT` query saved in a file. dbt runs it and saves the result as a table or view. Models build on each other (raw, then staging, then marts), and dbt works out the order. Analogy: a recipe that depends on other recipes.
- **Data-quality check.** An automated rule that asserts something about the data, for example "order_id is never null", "order_id is unique", or "today's row count is within normal range". When a rule fails, you find out before a stakeholder does. In dbt these are called tests.

### The tools

| Tool | What it is | Why it is here |
|---|---|---|
| **uv** | A fast Python package and project manager | Creates the project, pins dependencies, and runs scripts (`uv run python scripts/smoke.py`) in a reproducible environment. |
| **Python 3.12** | The language | Wide library support for the whole stack. The measured version was 3.12.15. |
| **DuckDB** | An embedded analytical database stored in one file | Needs no server and no Docker, so it runs on a Windows laptop. The plan's reason: "Runs on Windows with no server or Docker." |
| **dbt-core + dbt-duckdb** | dbt is the transformation tool; dbt-duckdb is the adapter that lets it talk to DuckDB | It is the real tool data teams use, so schema and test failures look like real-world ones. This matters for a portfolio piece aimed at data engineer roles. |
| **Dagster** | An orchestrator written in Python | Gives real run and step status and logs, which the agent's tools (`get_job_status`, `search_logs`) will read in a later phase. It runs "in process", meaning inside a normal Python program with nothing extra to deploy. |
| **matplotlib** | A Python plotting library | Renders charts (row-count trends, run timelines) to PNG files. Those images become evidence the agent can look at. |

The common theme is "real tools, no infrastructure." A reviewer can clone the repo and run everything locally.

---

## 3. Master and worker model tiers

The agent uses two Claude models for different jobs. In the client file they are named as constants:

```python
# Model tiers. The master does open-ended reasoning (root cause, remediation
# decisions); workers do narrow, repeated jobs (summarize logs, diff schemas).
MASTER = "opus"
WORKER = "sonnet"
```

- **Master (Opus).** The larger, more capable, more expensive model. It does open-ended reasoning: weigh the evidence, name the root cause, decide whether to remediate.
- **Workers (Sonnet).** A cheaper, faster model for narrow, repeated jobs: summarize logs, diff two schemas, read check results, read a chart, re-verify after a repair.

This is an **orchestrator/worker** (also called master/worker) design. Analogy: a hospital. The senior physician makes the diagnosis and decides on treatment, but nurses and technicians take the vitals, run the blood tests, and prepare the summaries. You would not want the senior physician spending their day taking temperatures, and you would not want a technician deciding on surgery.

### Why split by cost and difficulty

1. **Cost.** If you send every small job to the expensive model, you pay (or, on a subscription, burn usage limits) for capability you did not need.
2. **Difficulty.** Reading one log line and extracting a task name is easy. Deciding which of three plausible causes is the real one is hard. Match the model to the task.
3. **Focus.** A worker gets a small, well-defined job and returns a condensed result. The master then reasons over a short pile of evidence instead of a huge pile of raw logs.

Phase 6 of the plan includes ablations (for example, Sonnet-only master, or no workers) to test whether the split actually helps. In Phase 0 it is a design choice, not a proven benefit.

---

## 4. Why the agent calls Claude through the CLI in headless mode

The usual way to call Claude from code is the Anthropic API, which bills per token. This project instead calls the **Claude Code CLI** in **headless mode** (`claude -p`, where `-p` means "print": run once, print the answer, exit) using the owner's Claude subscription. From the plan:

> No API billing. `ANTHROPIC_API_KEY` is stripped from the child process so a stray key cannot switch billing.

The client does the stripping like this:

```python
env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
```

The client then launches `claude` as a **child process** (a program started by your program) with that cleaned environment. If an API key were left in the environment, the CLI could quietly use it and start billing per token. Removing it makes that impossible. There is a unit test for exactly this (`test_api_key_is_stripped_from_child_env`).

### A note on scope

This is a personal-use setup on the owner's own machine, signed in with their own account (`claude auth login`). Someone who clones the repo uses their own login. Because not every reviewer will have one, a **replay mode** is planned: every LLM call is recorded, and the dashboard and eval report can be served from the recordings with no login. Replay mode is a plan, not something Phase 0 built.

### The flags, one by one

The client builds the command in a method called `command`:

```python
cmd = [
    self.binary, "-p",
    "--model", model,
    "--system-prompt", system_prompt,
    "--tools", "",
    "--strict-mcp-config",
    "--setting-sources", "",
    "--no-session-persistence",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
]
```

The module docstring summarizes the intent: "Every call is stateless: built-in tools are disabled, a short custom system prompt replaces Claude Code's default one, and no session is persisted."

| Flag | What it does here |
|---|---|
| `-p` | Headless: take one input, print the result, exit. No interactive chat. |
| `--model` | Chooses the tier (`opus` or `sonnet`). |
| `--system-prompt` | Replaces Claude Code's long default system prompt with a short one we write. The system prompt is the standing instruction ("You are a precise assistant."). A shorter one means fewer tokens per call. |
| `--tools ""` | Disables the CLI's built-in tools (file editing, shell, and so on). We want the model to answer, not to act on the machine. Later phases will expose only the project's own tools, on purpose. |
| `--strict-mcp-config` | Only use MCP (tool-server) configuration explicitly given. With none given, no extra tool servers are loaded. |
| `--setting-sources ""` | Load no settings files (user, project, or local). This stops personal config from leaking into the call and keeps behavior the same on every machine. |
| `--no-session-persistence` | Do not save the conversation to disk. Each call is independent. |
| `--input-format stream-json` | Input is JSON messages on standard input rather than a plain command-line string. This is what makes image input possible (section 5). |
| `--output-format stream-json` | Output is a series of JSON events, one per line, ending with a `result` event that holds the answer, token counts and cost estimate. |
| `--verbose` | Required: in print mode the CLI refuses stream-json output without it ("--output-format=stream-json requires --verbose", checked 2026-10-14). |
| `--json-schema` | Added only when a schema is given: forces the answer to fit that JSON structure (section 6). |
| `--effort` | Added only when requested: sets how much reasoning effort the model spends. |

The client also runs the process from an empty working directory (`~/.pia-llm` by default) "so no CLAUDE.md or project settings leak into the prompt."

### Why not `--bare`?

The CLI has a `--bare` flag that strips things down even further. The Phase 0 finding was that `--bare` only accepts API-key authentication, which is the opposite of the goal. So it could not be used. The combination of flags above is the substitute, and the measurements in section 7 show it works well.

### One Windows quirk

`find_claude_binary()` looks for the native `claude.exe` and skips the npm `.cmd` shim, because, per the code comment, "cmd.exe mangles long arguments". Long system prompts and JSON schemas are passed as command-line arguments, so this matters.

---

## 5. Multimodal: letting workers look at charts

**Multimodal** means a model accepts more than one kind of input, here text plus images. In this project, a worker can be handed a rendered chart (a row-count trend, a run timeline) as evidence. The plan's reasoning: "Some incidents (a row-count cliff) are faster to see than to query." A human glancing at a line chart sees the cliff instantly; a model can do the same.

### How an image is sent

Images travel inside the stream-json user message as a **base64** block. Base64 is a way of encoding binary bytes (an image file) as plain text so they can sit inside JSON. The relevant code:

```python
def _image_block(path: Path) -> dict:
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise ValueError(f"unsupported image type: {path}")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}

def build_user_message(prompt: str, images: list[Path] | None = None) -> dict:
    content: list[dict] = [_image_block(Path(p)) for p in images or []]
    content.append({"type": "text", "text": prompt})
    return {"type": "user", "message": {"role": "user", "content": content}}
```

The message is a list of content blocks: images first, then the text question. The client writes it as one JSON line to the child process's standard input. A unit test confirms the order is `["image", "text"]`, that the media type is `image/png`, and that decoding the base64 gives back the original bytes.

### How the smoke test grades it mechanically

A weak test would ask the model "what does this chart show?" and eyeball the answer. This test is graded by code, with no judgment involved:

1. The script generates its own chart of daily row counts with matplotlib, with a drop planted on a known date: `DROP_DAY = date(2026, 9, 17)`. From that day on, counts are multiplied by 0.31.
2. The x-axis labels are written as `MM-DD`.
3. The prompt contains no data, only the question: on which date does the count first drop sharply, and by roughly what percent?
4. The model must answer with a date and a percentage. The test passes only if the date equals `09-17` and the percent is between 55 and 80.

Because the numbers are not in the prompt, the model cannot guess or copy them. It must read the date off the picture. A 31% remaining count means a 69% drop, which is the "truth" the test compares against.

---

## 6. Structured output with JSON Schema

If you ask a model a question in plain English, you get a paragraph. Code cannot reliably pull "the failed task" out of a paragraph. An agent is a program that makes decisions and calls tools based on model output, so it needs output it can parse with certainty.

A **JSON Schema** describes the shape of valid JSON: which fields exist, their types, which are required. The smoke test uses one for log extraction:

```python
schema = {
    "type": "object",
    "properties": {"failed_task": {"type": "string"}, "error_class": {"type": "string"}},
    "required": ["failed_task", "error_class"],
}
```

The client passes it with `--json-schema`, and the result comes back in `structured_output`, which the client exposes as `LLMResult.structured`. Code can then do `s.get("failed_task")` instead of parsing prose.

Why an agent needs this:

- **Reliability.** Downstream code can trust the fields exist and have the right types.
- **Verification.** You can only grade an output mechanically (as in the chart test) if it is structured.
- **Later phases.** The Phase 4 diagnosis is planned as structured: evidence (with ids), root cause, confidence, proposed action. Structured output is what lets each diagnosis link back to the evidence behind it.

The client also wraps results in a small dataclass, `LLMResult`, holding text, structured output, model name, token counts, estimated cost, duration and turns. Note the comment on the cost field: `# what the same tokens would cost on the API; NOT a real charge`.

---

## 7. Measured Phase 0 results

These are the numbers from the Phase 0 run. Nothing here is extrapolated beyond them.

### Smoke test: 6/6 passed

| # | Check | Result |
|---|---|---|
| 1 | DuckDB query | passed |
| 2 | dbt + dbt-duckdb installed | passed |
| 3 | Dagster job executes | passed |
| 4 | Opus text reply | returned `ready` |
| 5 | Sonnet structured JSON | `{"failed_task":"load_orders","error_class":"duckdb.BinderException"}` |
| 6 | Sonnet reads the chart | `{"drop_date_mm_dd":"09-17","approx_drop_pct":69}` against truth of 09-17 and about 69% |

Checks 1 to 3 need no LLM; the script's `--skip-llm` flag runs only those.

### Versions

Python 3.12.15, duckdb 1.5.6, dbt-core 1.12.6, dbt-duckdb 1.11.0, dagster 1.13.26, Claude Code CLI 2.1.300. Models: `claude-opus-5-5` and `claude-sonnet-5-5`.

### Overhead: the trimmed flags mattered

A trivial call using the CLI's default settings cost about $0.19 API-equivalent. With the trimmed flag set from section 4:

| Call | API-equivalent cost |
|---|---|
| Opus text | $0.0069 |
| Sonnet JSON | $0.0068 |
| Sonnet image | $0.0094 |

That is roughly 27 times less than the default.

**What "API-equivalent" means.** On a subscription you are not charged per call. The CLI still reports a number: an estimate of what the same tokens would have cost on the pay-per-token API. It is a useful yardstick for comparing designs (one prompt style versus another), but it is not money leaving your account. What actually constrains you on a subscription is **usage limits**. Pro limits are tight for Opus, so the Phase 6 evaluation will be kept small. This is also why the plan labels cost as "Tokens per incident + API-equivalent dollars, labelled as an estimate."

Why was the default so heavy? The default CLI loads its full default system prompt, built-in tool descriptions and settings on every call, and all of that counts as input tokens. The trimmed flags remove those. (This write-up does not break down exactly which part contributed how much; only the before and after totals were measured.)

### Tests

There are 8 unit tests for the client in `tests/test_claude_cli.py`. They are mocked: none make an LLM call, so they are free, fast and deterministic. They cover:

- image blocks come before text, with correct media type and base64 round trip
- an unsupported image type (`.bmp`) is rejected
- `parse_result_event` takes the last `result` event and skips non-JSON noise
- a transcript with no `result` event raises `ClaudeCLIError`
- `to_llm_result` maps token usage correctly
- an error event raises `ClaudeCLIError`
- the command disables tools and settings, and carries the schema and effort
- `ANTHROPIC_API_KEY` is stripped from the child environment

A **mock** replaces a real dependency with a fake one. In the API-key test, `subprocess.run` is replaced by a fake that records the environment it was given, so the test can assert the key is absent without launching anything.

---

## 8. Glossary

- **Agent.** A program that uses a language model to decide what to do next, often by calling tools, rather than just answering one question.
- **API (Anthropic API).** The pay-per-token interface for calling Claude from code. Not used here.
- **API-equivalent cost.** An estimate of what a set of tokens would cost on the API. On a subscription it is a yardstick, not a charge.
- **Base64.** An encoding that turns binary data into plain text characters, so images can be placed inside JSON.
- **CLI (command-line interface).** A program you run by typing commands in a terminal. Here, `claude`.
- **Child process.** A program started by another program. The Python client starts `claude` as a child.
- **dbt.** A tool for writing data transformations as SQL files (models) and testing the results.
- **Dagster.** A Python orchestrator that runs jobs, tracks run status and keeps logs.
- **Data-quality check.** An automated rule asserting something about data (not null, unique, row count in range).
- **DuckDB.** An embedded analytical database that lives in a single file; no server needed.
- **Environment variable.** A named setting inherited by programs, such as `ANTHROPIC_API_KEY`.
- **Gate.** A pass/fail condition at the end of a phase. Work does not move on until it is met.
- **Headless mode.** Running a normally interactive tool non-interactively, once, from code (`claude -p`).
- **Incident.** A pipeline failure or data problem that needs diagnosis and a fix.
- **JSON Schema.** A description of the allowed shape of a JSON document.
- **LLM (large language model).** The kind of AI model behind Claude.
- **Master/worker (orchestrator/worker).** A design where one stronger model plans and decides and cheaper models do narrow tasks.
- **MCP (Model Context Protocol).** A standard for plugging external tool servers into an AI client. Disabled here for a clean call.
- **Mock.** A fake stand-in for a real component, used in tests.
- **Multimodal.** Accepting more than one kind of input, such as text and images.
- **Orchestrator.** Software that runs pipeline steps in order, on schedule, and records what happened.
- **Pipeline (data pipeline).** A chain of automated steps that moves and transforms data.
- **Replay mode.** A planned feature that serves recorded LLM calls so reviewers need no login.
- **Sandbox.** A safe, disposable copy of the pipeline where fixes can be tried without risk.
- **Smoke test.** A quick check that the whole system is not obviously broken.
- **stream-json.** A CLI format where input and output are JSON events, one per line.
- **System prompt.** The standing instruction given to a model before the user's question.
- **Token.** The unit of text a model reads and writes; usage and cost are measured in tokens.
- **uv.** A fast Python project and package manager.
- **Usage limits.** The caps a subscription places on how much you can use models in a period.

---

## 9. Self-test

Try to answer each question before opening the answer.

**1. What condition must be met before a proposed fix counts as a success in this project, and why?**

<details>
<summary>Answer</summary>

The fix must be applied in the sandbox, the failing job rerun, and the checks must pass. This is because a model can sound confident and still be wrong, so a diagnosis is only credited when something outside the model confirms it worked.

</details>

**2. In one sentence each, what do an orchestrator and a dbt model do?**

<details>
<summary>Answer</summary>

An orchestrator (Dagster here) runs pipeline steps in order and records their status and logs. A dbt model is a SQL SELECT query saved in a file that dbt runs to build a table or view, in dependency order with other models.

</details>

**3. Why is the work split between Opus (master) and Sonnet (workers)?**

<details>
<summary>Answer</summary>

By cost and difficulty. Opus is stronger and more expensive, so it is reserved for open-ended reasoning such as root cause and remediation decisions. Sonnet handles narrow, repeated jobs such as summarizing logs, diffing schemas and reading charts. The master then reasons over condensed evidence. Whether the split truly helps is tested later through ablations.

</details>

**4. Why does the client remove `ANTHROPIC_API_KEY` from the child process environment?**

<details>
<summary>Answer</summary>

So a stray key in the environment cannot make the CLI switch from the subscription to per-token API billing. A unit test verifies the key is absent from the environment passed to the child process.

</details>

**5. What do `--tools ""`, `--setting-sources ""` and `--no-session-persistence` each accomplish?**

<details>
<summary>Answer</summary>

`--tools ""` disables the CLI's built-in tools so the model answers rather than acts on the machine. `--setting-sources ""` loads no settings files, so personal or project config does not leak in. `--no-session-persistence` stops the conversation being saved, keeping every call stateless.

</details>

**6. Why could `--bare` not be used?**

<details>
<summary>Answer</summary>

It only accepts API-key authentication, but the project's goal is to run on a subscription login with no API billing.

</details>

**7. How does an image reach the model, and in what order are the content blocks arranged?**

<details>
<summary>Answer</summary>

The file's bytes are base64-encoded and placed in an image block inside a stream-json user message, which is written to the child process's standard input. Image blocks come first and the text prompt comes last. `--input-format stream-json` is what makes this possible.

</details>

**8. Why is the chart check in the smoke test graded mechanically and not by reading the model's answer?**

<details>
<summary>Answer</summary>

The script generates the chart itself with a planted drop on 2026-09-17, so it knows the true answer. The prompt does not contain the data, so the model has to read the date from the picture. The test passes only if the date is `09-17` and the percentage is between 55 and 80. That removes human judgment and guessing.

</details>

**9. Why does an agent need JSON Schema structured output instead of plain text answers?**

<details>
<summary>Answer</summary>

Code needs fields it can rely on (exist, correct type) to make decisions and call tools, and prose is unreliable to parse. Structure also enables mechanical grading and, in later phases, linking each diagnosis to the specific evidence behind it.

</details>

**10. The default CLI call cost about $0.19 API-equivalent and the trimmed call about $0.007. Does that mean you saved $0.18 per call on your bill?**

<details>
<summary>Answer</summary>

No. API-equivalent is an estimate of what the tokens would cost on the pay-per-token API, not a charge. On a subscription the practical constraint is usage limits, which are tight for Opus on Pro. The roughly 27x reduction (to $0.0069 for Opus text, $0.0068 for Sonnet JSON and $0.0094 for a Sonnet image call) means each call consumes far fewer tokens, which stretches those limits and is why evals can be kept small but still feasible.

</details>
