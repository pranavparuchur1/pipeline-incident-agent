# Phase 1: the sandbox pipeline

Written for someone who knows Python and has read [00-foundations.md](00-foundations.md). That
document explains what a pipeline, dbt, Dagster and a harness are; this one does not repeat it.
Here we walk through the actual code Phase 1 added, file by file.

Contents
1. What Phase 1 built, and why before any AI
2. One day's data, end to end
3. The generator
4. Ingest
5. dbt
6. The custom data-quality checks
7. Dagster, failure flow, and the logs
8. The harness angle: snapshots, fingerprints, one process per run
9. How the gate proved it
10. Glossary
11. Self-test

---

## 1. What Phase 1 built, and why before any AI

Phase 1 built a fake company ("Larkspur Goods", a small online shop) with a real, working
data pipeline: landing CSVs, a DuckDB warehouse, dbt models and tests, a Dagster job, custom
checks, a plain-text log, and the ability to save and restore the whole thing.

No model is called anywhere in it. That is deliberate. Think of a driving test: before you can
grade a driver you need a road, and you need to know exactly where the cones are. Our agent's
job is to find out what went wrong in a pipeline. To grade that, we have to cause the break
ourselves (Phase 2), so we know the true answer. Causing a break only means something if
there is a healthy pipeline to break, and if "healthy" is something we can prove, repeatably.
Phase 1 is that proof.

The contract of Phase 1 in one sentence: **a clean day runs green end to end, and running it
again from the same starting point gives exactly the same result.**

## 2. One day's data, end to end

Take one day, say 2026-10-01. Here is every place its data lands:

```
 landing/ (CSV files, one per table per day)
   orders/dt=2026-10-01/orders.csv   customers/...   order_items/...   payments/...
   products/products.csv   (a dimension, delivered once, no date)
        |
        |  ingest.py   (check columns by name, cast types, delete-then-insert, one transaction)
        v
 raw.orders  raw.customers  raw.order_items  raw.payments  raw.products     <- DuckDB tables
   + lineage columns: _dt  _source_file  _loaded_at  _run_id
        |
        |  dbt, "views": stg_orders.sql  stg_customers.sql  stg_order_items.sql
        |                stg_payments.sql  stg_products.sql
        v
 staging.stg_*      (renamed columns, no dedupe, no filtering)
        |
        |  dbt, "tables": fct_orders.sql  dim_customers.sql  fct_daily_revenue.sql
        v
 marts.fct_orders  marts.dim_customers  marts.fct_daily_revenue
```

Landing file paths come from `SandboxPaths.landing_file` in `sandbox/config.py`:
`landing/<table>/dt=<day>/<table>.csv`. The `dt=` folder name is a convention borrowed from
big-data systems (Hive-style partitioning); it just means "this folder holds that day".

### The four lineage columns

Every raw table has the source columns plus four more, added by ingest
(`LINEAGE_COLUMNS` in `ingest.py`):

```python
LINEAGE_COLUMNS = {"_dt": "DATE", "_source_file": "VARCHAR", "_loaded_at": "TIMESTAMP", "_run_id": "VARCHAR"}
```

- `_dt`: which day's partition this row belongs to.
- `_source_file`: which landing file it came from.
- `_loaded_at`: when it was loaded.
- `_run_id`: which Dagster run loaded it.

Why bother? Imagine finding a bad row in a mart. Without lineage you can't answer "where did
this come from, and which run put it there?". With it you can follow a row back to a file and
a run. It is the paper trail, like a parcel's tracking number. Later phases will cite these
columns as evidence ("the duplicates all share `_run_id` X"). Staging keeps `_dt` (renamed
`partition_date`), `_source_file` and `_run_id`, and drops `_loaded_at`.

### Views and tables

`dbt_project/dbt_project.yml` sets two defaults:

```yaml
    staging:
      +materialized: view
      +schema: staging
    marts:
      +materialized: table
      +schema: marts
```

A **view** is a saved query: nothing is stored, and every read re-runs the SELECT against raw.
A **table** is the stored result. Staging is a thin relabelling of raw, so a view is cheap and
always current. Marts do joins and aggregation that people query often, so we store the
answer. (Analogy: a view is a recipe card; a table is the cooked dish.)

## 3. The generator

`sandbox/generator.py` plays the role of "the shop's app database exporting files". The key
function is two lines:

```python
def _rng(day: date | None, salt: str) -> random.Random:
    return random.Random(f"{SEED}:{salt}:{day.isoformat() if day else 'static'}")
```

You know `random.seed(...)` from Python. The trick here is that we do not seed the global
generator once and hope. Every `(day, purpose)` pair gets its **own** `random.Random`,
seeded from a string like `"<seed>:orders:2026-10-01"`. Consequences:

- Generating 2026-10-01 gives the same rows whether you generated 09-15 first, last, or never.
  Days don't share a generator whose state depends on call order.
- Changing the logic for one purpose (`"volume"` vs `"orders"`) doesn't shift the random
  stream of another.

The files are written with a fixed line terminator (`lineterminator="\n"`), so they are
**byte-identical**, not just "the same data". Why that matters:

1. The Phase 1 gate compares two runs. If the inputs differed even slightly, a difference in
   outputs would be unexplainable.
2. In Phase 2 we will break a day's files on purpose. "Broken" only means something against a
   known-good original; byte-identical regeneration gives us the original on demand.
3. Anyone cloning the repo gets the same world, so results can be reproduced.

Other details worth reading in the file: ids are dense and globally unique (order ids continue
from the previous day's total, via `_order_id_offset`), each order's `order_total` is computed
from its items, and each non-cancelled order gets a payment for that same total. Those are the
facts that the dbt singular tests later check. `tests/test_sandbox.py` pins them:
`test_generator_is_deterministic`, `test_generator_ids_unique_across_days`,
`test_order_totals_match_items_and_payments`.

## 4. Ingest

`sandbox/ingest.py` moves a CSV into a raw table. Four ideas live in it.

### The by-name contract

`RAW_CONTRACT` declares, for each table, the exact column names and types we expect:

```python
"orders": {
    "order_id": "BIGINT", "customer_id": "INTEGER", "order_ts": "TIMESTAMP",
    "status": "VARCHAR", "channel": "VARCHAR", "currency": "VARCHAR",
    "order_total": "DECIMAL(12,2)",
},
```

Ingest reads the CSV's header, compares it to the contract by **name** (not by position), and
refuses to continue if they differ:

```python
unexpected = [c for c in file_cols if c not in contract]
missing = [c for c in contract if c not in file_cols]
if unexpected or missing:
    raise IngestError(
        f"schema mismatch loading raw.{table} from {path.name}: "
        f"missing columns {missing or '[]'}, unexpected columns {unexpected or '[]'}"
    )
```

So if upstream renames `order_total` to `total_amount`, the error reads:

```
schema mismatch loading raw.orders from orders.csv: missing columns ['order_total'], unexpected columns ['total_amount']
```

That shape (a `missing` list and an `unexpected` list) is a gift to a diagnosing agent: a
rename shows up as one entry in each list.

### Loud failure beats silent NULL

The tempting alternative is a forgiving loader: read what columns exist, fill the rest with
NULL. Then a renamed column loads without any error; the `order_total` column is just empty.
Nothing crashes. Every revenue figure quietly becomes zero and someone notices a week later.
A pipeline that stops at the door with a clear message is far cheaper than one that lets bad
data in. The rule of thumb: *fail where the problem enters, not where it is finally noticed.*
(`test_renamed_column_fails_loudly` and `test_missing_landing_file_fails_loudly` pin this.)

### Delete-then-insert, so reruns are safe

```python
con.execute(f"delete from raw.{table} where _dt = ?", [day])
con.execute(f"insert into raw.{table} by name select ... from read_csv(...)")
```

Loading day D means: first remove any rows already stored for `_dt = D`, then insert the file.
Run it twice and you get the same table as running it once. This property is called
**idempotency**, and it is what makes a retry safe. (Contrast an append-only load: a retry
would double every row, which is exactly the "duplicate records" incident. We want that to
happen only when we choose to inject it.) `test_load_is_idempotent` checks it.

### Transactions and rollback

The delete and insert sit inside `begin` ... `commit`. A **transaction** is an all-or-nothing
bundle: either every statement in it takes effect, or none does. If the insert fails halfway
(say a value that can't be cast to a number), the code runs `rollback` and the delete is undone
too:

```python
    except duckdb.Error as e:
        con.execute("rollback")
        raise IngestError(f"failed loading raw.{table} from {path.name}: {e}") from e
```

Without the transaction, a failed load would leave the day deleted and not replaced, a hole.
With it, the previous good partition survives. `test_bad_value_rolls_back` checks that.

The loader also notes the products dimension: it has no date, so `day is None` and the whole
table is replaced.

## 5. dbt

The dbt project is in `dbt_project/`. A **model** is one `.sql` file with one SELECT.

### `source()` and `ref()`

Staging models read from raw with `source()`:

```sql
from {{ source('raw', 'orders') }}
```

and `models/sources.yml` declares which raw tables exist. Marts read from other models with
`ref()`:

```sql
from {{ ref('stg_orders') }} as o
```

These are not just spellings. dbt reads every `ref()` and `source()` to build a dependency
graph, then runs models in the right order. It is why you never write "run staging first" in
code. `fct_orders` refs `stg_orders`, `stg_order_items` and `stg_payments`, so those run first.

The staging models are plain renames: `order_ts as ordered_at`, `order_total as order_amount`,
`_dt as partition_date`. `stg_order_items` also computes `quantity * unit_price as line_amount`.
The three marts: `fct_orders` (one row per order plus item and payment rollups),
`dim_customers` (one row per customer with lifetime value), `fct_daily_revenue` (orders and
revenue per day and channel). `macros/generate_schema_name.sql` is a small override so that
schemas are named exactly `staging` and `marts` rather than dbt's default prefixed names.

### Generic tests

In `models/staging/staging.yml`, tests are declared under columns:

```yaml
- name: order_id
  data_tests: [unique, not_null]
- name: status
  data_tests:
    - accepted_values:
        arguments:
          values: [placed, shipped, cancelled, returned]
- name: customer_id
  data_tests:
    - relationships:
        arguments:
          to: ref('stg_customers')
          field: customer_id
```

dbt ships four of these "generic" tests, and we use all of them:

| Test | Plain meaning | Python equivalent |
|---|---|---|
| `unique` | no two rows share this value | `len(set(ids)) == len(ids)` |
| `not_null` | no row is missing this value | `all(x is not None for x in xs)` |
| `accepted_values` | value is one of an allowed list | `x in {"placed", "shipped", ...}` |
| `relationships` | every value exists in another table's column (a "foreign key") | `set(order_ids) <= set(orders_table_ids)` |

Each compiles to a SELECT that returns the *offending rows*. Zero rows = pass.

### Singular tests

A **singular** test is a standalone SQL file in `dbt_project/tests/` that you write for one
specific rule. We have two:

- `assert_order_totals_match_items.sql`: the order's total must equal the sum of its line
  amounts (within a cent).
- `assert_payments_match_orders.sql`: every non-cancelled order must have payments summing to
  its amount.

Generic = reusable one-column rules from a menu. Singular = a custom rule across tables that
no menu item expresses. The same "returns the bad rows" convention applies.

### Why staging must not dedupe

It is common in real projects to put `row_number() ... where rn = 1` in staging to remove
duplicates "for safety". We deliberately do not. The file descriptions say it: "No dedup or
filtering." If staging silently cleaned duplicates, the `unique` test would pass on top of
broken raw data and the incident would be invisible. The sandbox exists to let problems
surface to the detectors, so every layer passes problems through rather than absorbing them.
(A real team might dedupe somewhere. Here, hiding is the one thing we cannot afford.)

### What "dbt build skips downstream of a failed test" means

`dbt build` runs models and their tests together, in dependency order. If a test on a model
fails, dbt does not build the models that depend on it; it marks them `skipped`. Think of an
assembly line with an inspector: if the flour is bad, the cake stage doesn't start. In our
pipeline `stg_orders` is a view, but its tests (`unique_stg_orders_order_id`) still gate the
marts: the results doc records that after duplicating a day's orders, the downstream marts
were SKIPPED. A skipped mart means yesterday's table is still sitting there, stale. That is
exactly the kind of fact an investigator needs to know.

## 6. The custom DQ checks

dbt tests ask "is each row valid?". They can't say "is there enough data?" because a table
with half its rows is still perfectly valid row by row. `sandbox/dq.py` adds five kinds of
check, all returning the same `CheckResult(check_id, kind, status, observed, expected, message)`.
Each run: 28 dbt tests plus 9 custom ones (4 partition-present, 3 row-count-trend, 1 freshness,
1 reconciliation), 37 in total. Every result is stored in a table, `ops.dq_results`, so an
investigation can later cite it.

**partition_present** (one per event table): does `raw.<table>` have more than zero rows for
today's `_dt`? A missing file already fails ingest, but an *empty* file, or a table that
silently got no rows, is caught here.

**row_count_trend** (orders, order_items, payments): compare today's row count with the median of
the previous seven partitions, and require a ratio between 60% and 160%:

```python
TREND_WINDOW = 7
TREND_LOW, TREND_HIGH = 0.6, 1.6
...
median = statistics.median(history)
ratio = today / median if median else 0.0
ok = TREND_LOW <= ratio <= TREND_HIGH
```

Why the median and not the mean? One odd day (an earlier outage) would drag a mean around; the
median shrugs it off. Why a wide band? The generator makes weekends busier and adds slow
growth, so normal days vary. With fewer than three prior partitions the check passes with a
"trend check skipped" message instead of guessing. `test_row_count_trend_flags_drop` pins this.

**freshness**: is the newest order date in `marts.fct_orders` at least today's date? It
checks the *output*, not the input. If the transform didn't update the marts, this fails even
if raw looks fine.

**reconciliation**: do the day's distinct order ids in raw match the number of rows in
`marts.fct_orders` for that day? It is bookkeeping: count what went in, count what came out.

### What each catches that dbt tests can't, and which incident

| Check | Catches that a dbt test cannot | Incident family it would catch |
|---|---|---|
| partition_present | An empty or absent day (every row present is valid, there are just none) | Missing partition |
| row_count_trend | A day that is valid but far too small or too big | Row-count drop (also missing partition, as a ratio of 0) |
| freshness | Marts that look fine but are old | Missing partition; a transform that never ran |
| reconciliation | Rows lost or multiplied between raw and the mart | Duplicate records (mart has more rows than distinct raw ids) |

Where are the other two families? **Schema change** and **failed job** are loud: ingest
raises, or Dagster marks a step failed, so a DQ check isn't what finds them. Duplicates are
also caught by dbt's own `unique` test and the singular tests. These mappings are what the
checks are designed to do, based on reading the code; Phase 2 is where each is actually
injected and confirmed.

Notice what none of the checks do: they don't repair anything. They only produce evidence.

## 7. Dagster

`sandbox/pipeline.py` defines three **ops** (steps, each a decorated function) and one **job**
that wires them together:

```python
@dg.job(name=JOB_NAME)
def nightly():
    quality_checks(transform(ingest()))
```

Read it inside-out: `ingest()` runs first, its return value (rows loaded) feeds `transform`,
whose return value (the dbt results) feeds `quality_checks`.

### How failure flows

- **ingest** fails (missing file, schema mismatch): it logs the error, raises `dg.Failure`, and
  Dagster marks the later steps skipped. Nothing was loaded, so nothing is built on it.
- **transform** runs `dbt build` through `dbt_runner.py`, which turns dbt's results into plain
  `NodeResult` objects. Here is the important split:

```python
# Model errors stop the run here. Test failures don't: the dq step reports them with the rest.
if build.exception or any(n.resource_type == "model" and n.status == "error" for n in build.nodes):
    raise dg.Failure(description=summary)
```

  If a *model* errors (broken SQL, missing column), the run stops at `transform`. If only
  *tests* fail, `transform` still completes, and passes the results on.
- **quality_checks** combines dbt's test outcomes with the five custom kinds, stores all of them,
  logs each failure, and raises `dg.Failure` if any failed.

Why not stop at the first failed test? Because an investigator is better served by one report
listing everything wrong ("unique failed AND reconciliation failed AND totals mismatch") than
by a run that stops at the first one and hides the rest.

### The run store

Dagster keeps its own record of every run (status, per-step status, timings, events) in
`dagster_home/`, inside the sandbox. `run_pipeline_in_process` reads the events back into a
small `RunSummary` (`success`, `failed_step`, `error`, `step_status`). The CLI command
`pia sandbox status` reads the same store. Because it lives inside the sandbox folder, a
snapshot captures it too. When the agent later calls `get_job_status`, this is what answers.

### The plain-text log

Dagster's records are structured. Real on-call engineers also grep a text log, so
`sandbox/runlog.py` writes one line per event:

```
2026-10-14T22:40:01.123 ERROR [run=3f2a9c1e partition=2026-10-01 step=ingest] message...
```

Timestamp, level, a bracketed context (run id's first eight characters, partition, step), then
the message. Messages with newlines (stack traces) are flattened onto one line so that
**one event = one line**. The file is append-only, so a line's number never changes, which makes
the line number a stable **evidence id**: a later diagnosis can say `log:42` and anyone can
open the file and check. `test_runlog_one_line_per_event` pins the one-line rule.

## 8. The harness angle

From Phase 0's lens: the harness is everything around the model. Phase 1's contribution is
the **test bench**: a way to put the sandbox into an exact known state, run something, and
check the result.

### Snapshot and restore

`sandbox/snapshot.py` is deliberately plain: a snapshot is a directory copy of the whole
sandbox (warehouse file, landing files, Dagster store, logs), and restore deletes the root and
copies it back.

```python
def restore(paths: SandboxPaths, name: str) -> None:
    ...
    if paths.root.exists():
        shutil.rmtree(paths.root)
    shutil.copytree(src, paths.root)
```

`pia sandbox init` builds 30 days of history and saves a snapshot named `baseline`. Every
future incident starts with `restore baseline`, so every experiment begins from identical
state, and a failed repair attempt can be undone. (Phase 5's verification depends on this.)

### Fingerprints

How do you prove two runs gave "the same" warehouse? Comparing the files byte for byte
won't work (DuckDB files include internal bookkeeping). `fingerprint()` instead hashes the
*contents* of each table in raw, staging and marts:

```python
f'select count(*), sum(hash(row({col_list}))) from "{schema}"."{table}"'
```

It hashes every row and **sums** the hashes. Addition is order-independent, so it doesn't
matter in what order rows came back. The result is a short string such as
`45110827a2f3 (13841 rows)` for `marts.fct_orders`, the example in the results doc.

Why exclude `_loaded_at` and `_run_id`? Because they are different on every run by design:
one is the wall-clock time, the other a fresh random id. If they were hashed, two perfectly
correct runs would never compare equal and the fingerprint would be useless. The fingerprint
captures "same data", and the lineage columns that say "when and by whom" are left out.
(`test_fingerprint_ignores_run_id` pins it.) Later, "did the repair restore the right data?"
becomes: does the fingerprint equal the baseline's?

### One process per run

The first design ran Dagster and dbt inside the one Python process that was running the CLI.
It broke at snapshotting. dbt-duckdb keeps the warehouse file open for as long as the process
lives, and on Windows you cannot copy or delete a file another handle holds open, so the next
snapshot died with `WinError 32` (file in use by another process).

The fix is in `run_pipeline`: start each run as a **child process**, which releases every file
handle when it exits.

```python
proc = subprocess.run(
    [sys.executable, "-m", "pipeline_incident_agent.sandbox.pipeline", str(paths.root), day.isoformat()],
    capture_output=True, text=True, encoding="utf-8",
)
```

The child prints one marker line (`PIA_RUN_RESULT {...json...}`) that the parent parses back
into a `RunSummary`; if there is no marker, the child crashed and the parent says so, with the
tail of its output. This also resembles how a real scheduler works: each run is a separate job.

The cost is time: the 30-day history took 244 s with the in-process design and 624 s with
one process per run, about 20 s per run, mostly importing Dagster and dbt each time. We
accepted that: it is paid once at `init`, and the gate runs themselves take 15-16 s. A slower
setup that is correct beats a faster one that cannot snapshot.

## 9. How the gate proved it

All numbers below are from `docs/results/phase1.md`.

- `pia sandbox init` built 30 history days (2026-09-01 to 09-30): 30 of 30 runs succeeded,
  with 37 of 37 checks passing each day, in 624 s total.
- The gate: restore `baseline`, run 2026-10-01, twice. Both runs succeeded with 37 of 37
  checks; wall time 16 s and 15 s.
- Fingerprints of the 13 tables in raw, staging and marts were identical across the two runs
  (for example `marts.fct_orders`: 13,841 rows, `45110827a2f3`).
- The tests have teeth. Passing is cheap if the tests can never fail, so a scratch copy had one
  day's orders duplicated. `unique_stg_orders_order_id` failed (412 rows),
  `assert_order_totals_match_items` failed, and the downstream marts were skipped.
- 19 unit tests passed, covering generator determinism, idempotent loads, loud schema and
  missing-file errors, rollback, the trend check, and snapshot/fingerprint.

Two things were proved, and they are different. "Green twice with equal fingerprints" shows the
clean pipeline is stable and repeatable. "Deliberate duplicates turn it red" shows red is
reachable. Phase 2 builds on both: it will break things properly and demand that the checks
notice.

---

## 10. Glossary

- **Byte-identical**: files that match in every byte, not merely in meaning.
- **Contract (by-name)**: the declared column names and types ingest requires; compared by name, not position.
- **Dagster op / job**: a step function / the wiring of steps into one runnable pipeline.
- **Data test (generic)**: a reusable dbt check on one column: `unique`, `not_null`, `accepted_values`, `relationships`.
- **Data test (singular)**: a custom SQL file in `tests/` that returns the offending rows.
- **Delete-then-insert**: remove a partition's existing rows, then load; makes reruns safe.
- **Evidence id**: a stable pointer to a fact (such as a log line number) that a claim can cite.
- **Fingerprint**: an order-independent hash of a table's contents, ignoring load-time columns.
- **Idempotent**: doing it twice gives the same result as doing it once.
- **Lineage columns**: `_dt`, `_source_file`, `_loaded_at`, `_run_id`: where a row came from.
- **Materialization (view vs table)**: a view stores a query; a table stores the result.
- **Reconciliation**: comparing what went in with what came out.
- **`ref()` / `source()`**: how dbt models name their inputs, from which dbt builds the run order.
- **Rollback**: undoing everything in an unfinished transaction.
- **Seeded RNG**: a random generator started from a fixed value so it repeats exactly.
- **Skipped (dbt)**: a node not built because something upstream failed.
- **Test bench**: a controlled setup (here snapshot + restore + fingerprints) for repeatable experiments.
- **Transaction**: a group of database statements that all succeed or all vanish.
- **Trailing median**: the middle value of the recent past, used as the "normal" to compare against.

## 11. Self-test

1. Why does Phase 1 contain no AI, and what does the agent need from it?
2. Name the four lineage columns and give one reason they will be useful to an investigator.
3. The generator seeds a separate `random.Random` per day and purpose. What breaks if one global
   seed is used instead?
4. Upstream renames a column. Describe what a forgiving loader does versus ours, and which is better.
5. Why is delete-then-insert safe to retry, and what incident would an append-only load invite?
6. A load fails halfway through its insert. What state is the table in afterwards, and which
   mechanism guarantees that?
7. Why must staging not dedupe, and what would go wrong if it did?
8. A table has half its normal rows but every row is valid. Which tests notice, and which can't?
   Also: why are fingerprints computed without `_loaded_at` and `_run_id`?

<details><summary>Answers</summary>

1. The agent cannot be graded without a world where the truth is known. Phase 1 supplies a
   healthy pipeline, so Phase 2 can break it on purpose and know the correct diagnosis and fix.
2. `_dt` (the day), `_source_file` (the file), `_loaded_at` (when), `_run_id` (which run).
   Example: spotting that duplicate rows all share one `_run_id` points at a double load.
3. Each day's data would depend on how many random numbers earlier calls consumed, so
   generating one day alone, or in a different order, would give different data. Per-day,
   per-purpose generators make each day independent and reproducible.
4. A forgiving loader fills the missing column with NULLs: no error, and wrong numbers appear
   later. Ours raises a schema-mismatch error listing `missing columns` and `unexpected
   columns` at the door. Failing loudly where the problem enters is better.
5. Re-running first deletes that day's rows, so the end state is the same however many times
   it runs. An append-only load would double the rows on retry: the duplicate-records incident.
6. The previous good partition is intact, because the delete and insert are in one
   transaction and the error handler runs `rollback`.
7. Dedupe in staging would clean the problem before the `unique` test saw it, so the test would
   pass over broken raw data and the incident would be invisible. The sandbox exists to let
   problems reach the detectors.
8. dbt row-level tests pass (the rows are valid); `row_count_trend` fails if it falls outside
   60%-160% of the trailing seven-partition median. Fingerprints exclude those two columns
   because they differ on every run by design; including them would make two correct runs
   never match.

</details>
