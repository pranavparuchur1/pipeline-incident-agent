# Phase 2: breaking things on purpose

Written for someone who knows Python and has read [00-foundations.md](00-foundations.md) and
[phase1.md](phase1.md). Those explain what a pipeline, dbt, a harness, snapshots and fingerprints
are; this one does not repeat them. Here we walk through the code that breaks the sandbox
deliberately, fixes it deliberately, and proves both.

Contents
1. What Phase 2 built, and why it comes before the agent
2. Why ground truth makes evaluation mechanical
3. The five families, loud and silent
4. Persistent changes vs one-off glitches (S1 vs M1)
5. The remediation allow-list as least privilege
6. Escalate-only incidents: doing nothing as the right answer
7. Verification: rerun, checks, and fingerprints
8. Decoy fixes
9. The gate and its three kinds of trial
10. What the first gate run caught
11. The dev / held-out split
12. Glossary
13. Self-test

---

## 1. What Phase 2 built, and why it comes before the agent

Phase 1 gave us a healthy company. Phase 2 gives us a way to damage it in 24 specific,
repeatable ways, and a way to repair it in a controlled manner. Still no model is called.

Think of a flight simulator. Before you let a trainee pilot fly it, you need the instructor's
panel: buttons that trigger "engine failure" or "crosswind", and a rule book saying what the
correct response is for each. The simulator is only useful for grading if the instructor knows,
for every button, what the right answer is. Phase 2 is the instructor's panel and the rule book.

It consists of:

- A **catalog** (`incidents/catalog.py`): 24 incidents, each with the ground truth, the true
  fixes and the decoy fixes.
- A **simulated upstream** (`sandbox/upstream.py`): the shop's export system, which can change
  its mind about column names.
- A **remediation layer** (`sandbox/remediation.py`): the small, fixed list of repair actions.
- A **verifier** (`sandbox/verify.py`): decides from the sandbox itself whether a pipeline has
  recovered.
- A **gate** (`incidents/harness.py`): proves every claim the catalog makes.

## 2. Why ground truth makes evaluation mechanical

Most AI evaluation is vague: someone reads an answer and decides if it seems good. Ours does
not have to be, because **we cause every failure ourselves**. Here is the data structure behind
each incident:

```python
@dataclass
class Incident:
    id: str
    family: str
    title: str
    root_cause: dict          # what actually went wrong (graded against the agent's diagnosis)
    expected_failed_step: str # ingest | transform | quality_checks
    expected_evidence: list[str]
    inject: Callable[[SandboxPaths, date], None]
    true_fixes: list[Fix]
    decoy_fixes: list[Fix] = field(default_factory=list)
    outcome: str = "recover"  # recover | escalate
```

Compare this to a teacher who wrote the exam and the answer key at the same time. Later, when
the agent says "root cause: column renamed, table orders, column order_total", Python compares
that dict with `root_cause` field by field. No opinion is involved.

The key discipline is that the answer key is not trusted just because we wrote it. A catalog
might say "S1 fails at ingest" and be wrong. So Phase 2 includes a **gate** that checks the key
against reality (section 9). An answer key that has been tested against the real system is what
allows us to say a score is a measurement.

## 3. The five families, loud and silent

The foundations doc introduced these. Here they are with their catalog ids. Every incident id is
a letter for the family and a number.

| Family | Ids | Typical look |
|---|---|---|
| schema_change | S1 to S8 | Loud: ingest stops with a `missing columns` / `unexpected columns` error |
| missing_partition | M1 to M4 | Either loud (file never arrived, ingest fails) or silent (file arrived empty, M2) |
| duplicates | D1 to D4 | Silent: ingest succeeds, `quality_checks` fails |
| failed_job | F1 to F5 | Loud: a model or file breaks the run |
| row_count_drop | R1 to R3 | Silent: everything loads, only the row-count trend check fails |

Each incident records `expected_failed_step`, the place where the run first goes red. For loud
incidents that is early (`ingest` or `transform`); for silent ones it is the last step,
`quality_checks`, because nothing complained before the checks looked at volume.

"Loud versus silent" matters to the agent because it decides where to look. A loud failure
carries its own explanation in the log. A silent one must be found by comparing the data with
what normal looks like. Compare a smoke alarm (loud) with a slowly rising gas smell (silent):
the second one needs somebody who thinks to sniff.

How an incident is made is worth seeing, because it is very small. Several helpers build on
two ideas, "rewrite a landing file" and "record a persistent upstream change":

```python
def _delete(table):
    return lambda paths, day: paths.landing_file(table, day).unlink()

def _double(table, every=1):
    """Upstream export retried and appended: every `every`-th row appears twice."""
    return lambda paths, day: _rewrite(paths, table, day, lambda rs: rs + rs[::every])
```

M1 is `_delete("orders")`: the file is simply gone. D1 is `_double("orders")`: every order
appears twice. A catalog entry is a title, a ground-truth dict, an `inject` function, and the
fix lists.

## 4. Persistent changes vs one-off glitches (S1 vs M1)

This is the most important design idea in Phase 2, because it decides whether one particular
action, "ask upstream to resend", is a real fix or a fake one. The docstring at the top of
`sandbox/upstream.py` says it:

```
* One-off delivery glitches (a missing, truncated or duplicated file). Asking upstream to
  redeliver fixes them: the export runs again and produces a correct file.
* Persistent changes (a renamed, retyped, added or dropped column). These are deliberate on the
  upstream side, so a redelivery reproduces the same file.
```

Use a restaurant analogy. You ordered a pizza and the box arrived empty. Calling the restaurant
("please send it again") works, because the mistake happened once. Now suppose the restaurant
changed its menu and the pizza you ordered no longer exists in its old form. Calling and asking
for a resend gets you the new-style pizza again. The problem is not the delivery; it is that the
supplier changed, and you must adapt your kitchen.

In code, the two cases differ in one place. A one-off glitch edits the landing file once:

```python
# M1: the file for the day never arrived
_delete("orders")
```

A persistent change is written into `<sandbox>/upstream/changes.json` and applied to every export
from an effective day onwards:

```python
def redeliver(paths, table, day):
    """Upstream re-runs its export for one table/day: a correct file, plus any persistent changes."""
    rows, columns = apply_changes(table, day, export_rows(table, day), COLUMNS[table], load_changes(paths))
    return write_csv(paths.landing_file(table, day), rows, columns)
```

Now follow the two incidents.

**M1 (orders file missing).** The only damage is the missing file. `redeliver` regenerates the
correct file, and nothing in `changes.json` alters it. Verification: recovered. The catalog lists
`request_upstream_redelivery(tables=[orders], day=<day>)` as the true fix and `rerun_only()`
as a decoy (rerunning with the file still missing fails again).

**S1 (`order_total` renamed to `total_amount`).** Injecting S1 records a `rename_column` change
in `changes.json`, then exports the day's file. If an agent asks for redelivery, `redeliver`
reads `changes.json`, applies the rename again, and writes the same bad header. The rerun fails
with the same error. That is why the catalog lists redelivery as a **decoy** for S1, and the true
fix is `apply_column_mapping(table=orders, source_column=total_amount, target_column=order_total)`,
which changes how *our* ingest reads the file.

Notice the consequence for evaluation. A shallow agent that learned "something wrong with
the input file, request a resend" would pass M1 and fail S1. An agent that reads the error and
reasons about whether the change is standing or one-off passes both. The incidents come in pairs
like this on purpose, so the score reveals which kind of agent we built.

## 5. The remediation allow-list as least privilege

Everything that may change the sandbox goes through one dictionary, `ACTIONS` in
`remediation.py`. The module docstring states the policy:

```
Anything not listed here is impossible: there is no "run arbitrary SQL" action and no
"skip the failing test" action.
```

There are eight actions. Six change state: `request_upstream_redelivery`,
`apply_column_mapping`, `set_column_parser`, `ignore_extra_columns`, `deduplicate_on_ingest`,
`revert_model_change`. Two do not: `rerun_only` and `escalate_to_owner`.

This is **least privilege** again, now applied to the repair tool. Compare giving a repair
technician a keyring with exactly the eight keys they need versus a master key to the building.
Even a perfectly well-meaning technician with a master key can cause a disaster by accident;
with eight keys, the damage is capped by what the keys open.

Each action is also strict about its inputs. Look at how `apply_column_mapping` guards itself:

```python
_table(table)
src, dst = _ident(source_column, "source_column"), _ident(target_column, "target_column")
if dst not in RAW_CONTRACT[table]:
    raise RemediationError(f"target_column {dst!r} is not in the raw.{table} contract")
```

`_ident` accepts only lowercase identifiers (`^[a-z_][a-z0-9_]{0,62}$`), so text such as
`order_total; drop table raw.orders` is refused before it can reach any query. The target must
be a column in the contract, so a model cannot invent a new one. `ignore_extra_columns` refuses
outright to ignore a contract column: "refusing to ignore contract columns". If it didn't, an
agent could "fix" a missing-column error by ignoring the column that is missing.

And `apply_action` rejects unknown names and wrong parameters:

```python
action = ACTIONS.get(name)
if action is None:
    raise RemediationError(f"action {name!r} is not on the allow-list")
```

A subtle design choice: **applying an action never reruns the pipeline.** The docstring says
"an action can't mark its own work as done." Repair and verification are different functions
called by different code. We will see why in section 7.

The ingest-side actions work by editing a small JSON file, the ingest config
(`_update_ingest_config`). Ingest consults it when reading a file: a `rename` map, per-column
`parsers`, `ignore_columns`, a `dedupe_key`. The fix does not alter data directly; it changes a
documented setting, which a human can read afterwards. That makes every repair auditable.

## 6. Escalate-only incidents: doing nothing as the right answer

Two incidents, **S5** and **S7**, have `outcome="escalate"`. For these, no automated action
restores the data correctly, and the right behaviour is to hand it to a human.

**S5**: upstream dropped the `payments.method` column entirely. Our pipeline requires a method
for every payment. We cannot invent the values, and asking upstream to resend reproduces a file
without the column. The true fix is:

```python
("escalate_to_owner", {"owner": "upstream_app_team",
                       "summary": "payments export no longer includes method"})
```

The action changes nothing. The gate's rule for these incidents is worth reading carefully:

```python
elif incident.outcome == "escalate":
    ok = not v.recovered  # escalation changes nothing; the pipeline must still be broken
```

It checks that after "the right answer", the pipeline is **still failing**. That sounds odd,
and it is the point. The correct response is not to make the dashboard green, but to say, quickly
and with evidence, "this needs a person, here is what and why".

Why would this ever be the right answer? Consider an on-call engineer who finds the payment
method has vanished and quietly fills the column with `"card"` so that tests pass. The
dashboards are green, and every payments-by-method chart is now fiction. A confident wrong fix
is worse than an honest "I can't". This is the kind of judgment a harness should reward, and the
catalog has to include cases where it is rewarded, or an agent could score perfectly by
always fixing something.

**S7** is the harder version. The `order_items.quantity` column began arriving with a unit
suffix ("2 units"). There is a parser action in the allow-list (`strip_currency`) that looks
like it could help, and it is listed as a decoy: it strips `$` and commas, not the word "units",
so it doesn't fix the problem. More importantly, whether to change the meaning of an upstream
field is a decision for the owning team. So the true fix is again escalation.

## 7. Verification: rerun, checks, and fingerprints

`sandbox/verify.py` opens with the rule:

```
recovered  <=>  the rerun succeeded
            AND every quality check passed
            AND every raw/staging/marts table matches the known-good reference fingerprint
```

In code, that is one property:

```python
@property
def recovered(self) -> bool:
    return self.run.success and not self.failed_checks and not self.mismatched_tables
```

Three conditions, and each blocks a different kind of cheating.

1. **The rerun succeeded.** Someone claiming "it's fixed" must show a real pipeline run that
   completes.
2. **Every check passed.** The row-count, freshness, uniqueness and reconciliation checks from
   Phase 1 must be green.
3. **Every table equals a clean run's fingerprint.** This one is the subtle one.

### Why checks alone are not enough

Take duplicates. D1 doubled every order in the file. Suppose a "fix" does this:

```sql
delete from raw.orders where _dt = '2026-10-01';
```

It deletes the whole day's orders. Now `unique_stg_orders_order_id` passes (an empty table has
no duplicates), the reconciliation counts agree (zero equals zero), and the pipeline runs green.
By conditions 1 and 2 alone, the fix would count as a success. In fact an entire day of revenue
vanished.

Condition 3 catches this. We built a **reference**: run the clean pipeline for the incident day
once, from the baseline, and store the fingerprint of every table (`reference()` in
`harness.py`, saved with `save_reference`). Recall from Phase 1 that a fingerprint is an
order-independent hash of a table's contents. After the repair, `compare()` recomputes the
fingerprints and lists every table that differs:

```python
def compare(paths, reference):
    current = fingerprint(paths)
    return sorted(t for t in reference.keys() | current.keys() if reference.get(t) != current.get(t))
```

The deleted-rows result has a different fingerprint from the reference, so `mismatched_tables`
is not empty and `recovered` is `False`. That is how a **false remediation** is detected
mechanically. Note also the efficiency detail in `verify`: the fingerprint comparison runs only
when the rerun succeeded and all checks passed, since otherwise the answer is already "no".

This is the central idea of the whole project in miniature: **the world's state decides, not
anyone's claim.** Not the model's text, not even our own checks, because checks can be gamed.
An independent "what should this data be?" comparison closes the loophole.

(One honest limit: the reference is "a clean run of the day", so recovery means "identical to
what the pipeline would have produced had nothing gone wrong". For incidents where a fix cannot
reproduce the exact data, such as an escalation, the gate uses the separate escalate rule from
section 6.)

## 8. Decoy fixes

A **decoy** is a fix that looks sensible, and is plausible enough that a careless agent would
choose it, but must fail verification. Every incident lists some. Examples from the catalog:

- `rerun_only()` for a missing file (M1, M3). Nothing changed, so it fails again.
- `request_upstream_redelivery` for any persistent schema change (S1, S2, S3, S4, S5, S6, S8).
  The change is standing, so the new file has the same problem.
- `revert_model_change(model=stg_orders)` for F1, where the broken model is `fct_orders`
  (and the reverse for F2). A real model name, the wrong one. This is the "plausible but wrong
  target" pattern.
- `deduplicate_on_ingest(table=orders, key=order_id)` for D2, where the duplicates are in
  `order_items`. Right action, wrong table.
- `request_upstream_redelivery(tables=[orders])` for R1, where orders, order_items and payments
  were all cut. Redelivering only one table leaves the others short, so the reference
  fingerprint still doesn't match.

What do decoys prove? Two things.

First, they prove the **verifier is not rubber-stamping**. If every plausible-looking action came
out "recovered", the verifier would be useless. A decoy that fails is evidence that verification
has teeth, the same reason Phase 1 duplicated a day's orders to see tests go red.

Second, they prove the **problem has structure**. For S1 the decoy fails *because* the change is
persistent. That is a testable fact about the world, not a story in a prompt. Later, when we
see how often the agent picks a decoy, that number means something because the decoys are known
to be wrong.

## 9. The gate and its three trial kinds

`incidents/harness.py::check_incident` runs three kinds of trial for each incident. **Each
begins with a fresh `restore` of the baseline**, so trials cannot contaminate each other:

```python
def inject(paths, incident, day=INCIDENT_DAY):
    restore(paths, BASELINE)
    incident.inject(paths, day)
```

| Trial kind | What it does | Passes when |
|---|---|---|
| **break** | inject, run the pipeline | the run fails at the expected step AND the expected evidence appears in the log lines, error text or failed check ids |
| **true fix** | inject, apply each true fix, verify | `recovered` (for escalate-only incidents: still failing, untouched) |
| **decoy** | inject, apply each decoy, verify | NOT recovered |

The break trial deserves attention. It does not just check "the run failed". It checks that the
run failed **at the step the catalog says** and that the *specific strings* the catalog promises
are really there:

```python
missing = [e for e in incident.expected_evidence if e not in haystack]
ok = (not run.success) and run.failed_step == incident.expected_failed_step and not missing
```

That matters because the agent's tools (Phase 3) return exactly these lines and checks. If the
gate says "`missing columns ['order_total']` appears in S1's log", then an agent that cites it
is citing something real, and a grader can verify the citation.

Also note the comment in the code: "an invalid fix is a harness bug, not a pass". If a catalog
entry names an action with bad parameters, the trial records a failure, not a shrug.

The numbers from `docs/results/phase2.md`:

| Run | Incidents | Trials | Wall time |
|---|---|---|---|
| Pass 1 (original 16) | 16/16 | 59/59 | 1,107 s |
| Pass 2 (all 24, incl. 8 new) | 24/24 | 91/91 | 808 s |

The results doc adds that trial durations differ between passes because other work shared the
CPU, so the times are not a performance comparison. A third pass re-ran just the 8 new
incidents; read its own log (`gate_run3.log`) for the outcome, which this document does not
quote.

## 10. What the first gate run caught

The first full pass, before any fixes, scored 14/16. Both failures were **wrong assumptions in
our own catalog or code**, found by the gate. A third problem, S7, was also corrected (below).

**F2 (bad deploy in `stg_orders`).** We assumed it would fail at `transform`, like F1. It does
not. `stg_orders` is a view, so it builds fine; the *tests* that reference the vanished column
error, and dbt then skips every downstream mart. The run therefore fails at `quality_checks`,
far from the model that changed. Lesson: the place where a failure is *reported* can be several
steps away from the place where it *started*. An agent that stops at the first red step will look
in the wrong place, which is the reason the catalog now says what actually happens.

**F3 (truncated CSV).** DuckDB's CSV sniffer, the part that guesses a file's format, read the
damaged file and reported a misleading "schema mismatch", treating the whole header as one
column. Our own error message was lying. The fix was to read headers with Python's `csv` module
and hand DuckDB an explicit dialect, so the message says what is really wrong. Here the gate
found a real bug in the *pipeline*, which would have misled any investigator, human or AI.

**S7.** It was first written as a one-off file rewrite, which means redelivery would have
produced a clean file and silently "fixed" an incident labelled escalate-only. The gate's decoy
trial exposed the contradiction. S7 is now a persistent upstream change, so redelivery is a
decoy, as intended.

The lesson in one sentence: **the harness tests the test-writer too.** We assumed that the
people building the exam knew all the answers; the gate showed we did not. If we had skipped
the gate, the agent would have been graded against a wrong key and penalised for being right
(F2), or helped by a misleading error (F3), or credited for a one-line redelivery when the
correct behaviour was escalation (S7). Every number we report later is only as trustworthy as
this step.

## 11. The dev / held-out split

The results doc records a decision, with the note "fixed before any agent run":

- **Dev** (may be used to tune prompts): S1, M2, D1, F1, R1, one per family.
- **Held-out** (final eval only): the other 19.

(5 dev + 19 held-out = 24.)

This is the oldest rule in machine learning, applied to prompts. Suppose you look at the agent's
mistakes on all 24 incidents, and tweak the instructions until it gets them right. You have
now written prompts that contain, in disguised form, the answers to those exact incidents.
The score will be excellent and meaningless, the same as a student who practised on the exam
paper. This is **overfitting**.

The remedy is to split before anyone looks. Dev incidents may be inspected and used to refine
prompts; held-out incidents are touched once, at the end, to measure how the system does on
cases it was never tuned on. The word "before" is doing real work in the doc: if you choose the
split after seeing results you can (even unconsciously) put the awkward cases in dev. By fixing
it before any agent run, we remove that temptation.

Why one per family? So that dev covers every kind of problem and the prompts get a fair test
for each, while 19 incidents of varied difficulty remain for the final measurement.

## 12. Glossary

- **Allow-list**: an explicit list of permitted actions; anything else is refused.
- **Catalog**: our list of incidents, each with ground truth, true fixes and decoys.
- **Decoy fix**: a plausible-looking wrong fix that must fail verification.
- **Dev set**: incidents the builder may study and tune prompts on.
- **Escalate-only**: an incident with no safe automated fix; the correct response is to hand it to a human.
- **Expected evidence**: strings the gate requires to appear in the failure's log, error or failed checks.
- **Expected failed step**: the pipeline step where the catalog says the run first goes red.
- **False remediation**: a fix that makes the checks pass by hiding or destroying the problem.
- **Gate**: the automated test that proves each catalog claim by running it.
- **Ground truth**: the known correct answer, available because we injected the fault.
- **Held-out set**: incidents reserved for the final evaluation, never used for tuning.
- **Incident family**: one of five groups of failure types (schema, missing partition, duplicates, failed job, row-count drop).
- **Injection**: deliberately creating a failure in the sandbox.
- **Loud vs silent failure**: loud raises an error; silent produces wrong data without one.
- **One-off delivery glitch**: a single bad delivery; redelivery fixes it.
- **Overfitting (to an eval)**: tuning until the test passes, so the score no longer predicts performance on new cases.
- **Persistent upstream change**: a standing change in the source; redelivery reproduces it, so the pipeline must adapt.
- **Reference fingerprint**: the stored table hashes of a clean run, used as the definition of "correct data".
- **Trial**: one inject-then-act-then-check experiment inside the gate (break, true fix or decoy).

## 13. Self-test

1. Why can we grade the agent's diagnosis mechanically, without a human reading it?
2. Name one loud family and one silent family, and say where in the pipeline each first shows up.
3. Why is "ask upstream to resend" a real fix for M1 but a decoy for S1?
4. Why is there no "run this SQL" action in the allow-list? What principle is that?
5. S5 is graded as correct when the pipeline is still failing afterwards. Explain.
6. A candidate fix deletes every row in `raw.orders` for the day, and all checks pass. Does
   `verify` call that recovered? Which condition catches it?
7. What are the three kinds of trial in the gate, and what does each prove?
8. The first gate pass scored 14/16. Why was that a good outcome, and what does "the harness
   tests the test-writer too" mean? Also: why was the dev/held-out split fixed before any agent
   run?

<details><summary>Answers</summary>

1. We caused each failure, so the incident record holds the true root cause and true fixes. The
   agent's structured answer is compared with those fields by code.
2. Loud: schema change or failed job (ingest or transform errors). Silent: duplicates or
   row-count drop, which only fail at `quality_checks`. The first is in the log; the second needs
   checks and comparison with the usual volume.
3. M1 is a one-off glitch: the export runs again and produces a correct file. S1 is a persistent
   upstream rename recorded in `changes.json`; every redelivery applies it again, so the same bad
   header comes back. The pipeline's own ingest has to adapt (`apply_column_mapping`).
4. Least privilege. A model that can run arbitrary SQL can do arbitrary damage, by mistake or
   under a bad prompt. Eight narrow, strictly validated actions cap what a mistake can do, and
   each repair is recorded as a small readable config change.
5. For S5 no automated action restores correct data (the column is gone upstream), and inventing
   values would be a false fix. The right behaviour is a fast, evidence-backed handoff to a
   human; the action changes nothing, so the pipeline legitimately remains broken.
6. No. Rerun and checks would pass, but the table fingerprints would differ from the reference
   clean run, so `mismatched_tables` is non-empty and `recovered` is false. This is condition 3,
   the reference comparison.
7. Break: the incident really fails at the expected step with the expected evidence (the key is
   true). True fix: the intended repair really works (or, for escalation, changes nothing).
   Decoy: wrong fixes really fail (the verifier has teeth).
8. It exposed wrong assumptions in our own catalog and code (F2's failing step, the F3 error
   message, S7's redelivery loophole) before they could distort the evaluation. The test-writer
   can be wrong, and the gate checks the writer. The split was fixed first so the choice of
   which cases to tune on could not be influenced by seeing results; otherwise prompts could be
   overfitted and the held-out score would stop meaning anything.

</details>
