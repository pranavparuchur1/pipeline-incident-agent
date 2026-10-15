"""Phase 0 smoke test: the stack imports, and both model tiers answer through the subscription.

Checks (each prints PASS/FAIL):
  1. duckdb runs a query
  2. dbt-core + dbt-duckdb are installed
  3. dagster imports and can define a job
  4. master (Opus) answers a plain text prompt
  5. worker (Sonnet) returns schema-valid structured JSON
  6. worker reads an image: a rendered row-count chart with a planted drop on a known date

Check 6 is graded mechanically: the chart is generated here with the drop on DROP_DAY,
and the model has to name that day from the picture alone (the data is not in the prompt).

Usage: uv run python scripts/smoke.py [--skip-llm]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import duckdb

from pipeline_incident_agent.llm.claude_cli import MASTER, WORKER, ClaudeCLI, LLMResult

DROP_DAY = date(2026, 9, 17)
results: list[tuple[str, bool, str]] = []
llm_calls: list[tuple[str, LLMResult]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def check_duckdb() -> None:
    rows = duckdb.sql("select count(*) as n, sum(i) as s from range(1000) t(i)").fetchone()
    check("duckdb query", rows == (1000, 499500), f"duckdb {duckdb.__version__} -> {rows}")


def check_dbt() -> None:
    out = subprocess.run([sys.executable, "-m", "dbt.cli.main", "--version"], capture_output=True, text=True)
    text = out.stdout + out.stderr
    detail = text.split("\n")[1].strip() if text else ""
    check("dbt-core + dbt-duckdb", out.returncode == 0 and "duckdb" in text, detail)


def check_dagster() -> None:
    import dagster as dg

    @dg.op
    def one() -> int:
        return 1

    @dg.job
    def tiny():
        one()

    res = tiny.execute_in_process()
    check("dagster job executes", res.success, f"dagster {dg.__version__}")


def render_row_count_chart(path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    days = [DROP_DAY - timedelta(days=13) + timedelta(days=i) for i in range(21)]
    counts = [48_000 + (i * 397) % 2_100 for i in range(len(days))]
    for i, d in enumerate(days):
        if d >= DROP_DAY:
            counts[i] = int(counts[i] * 0.31)
    fig, ax = plt.subplots(figsize=(9, 4), dpi=110)
    ax.plot(days, counts, marker="o")
    ax.set_title("fct_orders daily row count")
    ax.set_ylabel("rows")
    ax.set_xticks(days)
    ax.set_xticklabels([d.strftime("%m-%d") for d in days], rotation=60)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def check_llm() -> None:
    cli = ClaudeCLI()

    r = cli.run("Reply with exactly the word: ready", model=MASTER, system_prompt="Follow instructions exactly.")
    llm_calls.append(("master text", r))
    check("master (Opus) text", r.text.strip().strip(".").lower() == "ready", f"{r.model} -> {r.text.strip()!r}")

    schema = {
        "type": "object",
        "properties": {"failed_task": {"type": "string"}, "error_class": {"type": "string"}},
        "required": ["failed_task", "error_class"],
    }
    log = (
        "2026-09-17 02:14:07 INFO task=load_orders started\n"
        "2026-09-17 02:14:09 ERROR task=load_orders duckdb.BinderException: "
        'Referenced column "order_total" not found in FROM clause\n'
        "2026-09-17 02:14:09 INFO task=load_customers succeeded\n"
    )
    r = cli.run(
        f"Which task failed and what is the exception class?\n\n{log}",
        model=WORKER,
        system_prompt="Extract facts from pipeline logs.",
        json_schema=schema,
    )
    llm_calls.append(("worker json", r))
    s = r.structured or {}
    ok = s.get("failed_task") == "load_orders" and "BinderException" in s.get("error_class", "")
    check("worker (Sonnet) structured JSON", ok, f"{r.model} -> {json.dumps(s)}")

    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "row_count.png"
        render_row_count_chart(chart)
        schema = {
            "type": "object",
            "properties": {"drop_date_mm_dd": {"type": "string"}, "approx_drop_pct": {"type": "number"}},
            "required": ["drop_date_mm_dd", "approx_drop_pct"],
        }
        r = cli.run(
            "This chart shows a table's daily row count. On which date (MM-DD, as labelled on the x-axis) "
            "does the count first drop sharply, and by roughly what percent?",
            model=WORKER,
            system_prompt="You read data-quality charts precisely.",
            images=[chart],
            json_schema=schema,
        )
    llm_calls.append(("worker image", r))
    s = r.structured or {}
    ok = s.get("drop_date_mm_dd") == DROP_DAY.strftime("%m-%d") and 55 <= float(s.get("approx_drop_pct", 0)) <= 80
    check("worker reads chart image", ok, f"truth {DROP_DAY:%m-%d} ~69% -> {json.dumps(s)}")


def main() -> int:
    check_duckdb()
    check_dbt()
    check_dagster()
    if "--skip-llm" not in sys.argv:
        check_llm()
        print("\nLLM calls (api_equiv_usd is what the tokens would cost on the API, not a charge):")
        for name, r in llm_calls:
            print(
                f"  {name:<14} {r.model:<20} in={r.input_tokens:>6} cache_read={r.cache_read_tokens:>6} "
                f"cache_write={r.cache_write_tokens:>6} out={r.output_tokens:>5} "
                f"{r.duration_ms / 1000:>5.1f}s ${r.api_equiv_usd:.4f}"
            )
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
