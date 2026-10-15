"""Data-quality checks that dbt's per-row tests can't express: volume, completeness, freshness.

dbt tests answer "is each row valid?". These answer "did the right amount of data arrive, and
did it reach the marts?", which is how the silent incidents (missing partition, row-count drop)
get caught. Every result is stored in `ops.dq_results` so investigations can cite it.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from datetime import date

import duckdb

from .config import EVENT_TABLES, SandboxPaths
from .dbt_runner import DbtBuildResult

TREND_WINDOW = 7
TREND_LOW, TREND_HIGH = 0.6, 1.6  # ratio of today's rows to the trailing median that counts as normal
TREND_TABLES = ("orders", "order_items", "payments")


@dataclass
class CheckResult:
    check_id: str  # stable name, e.g. row_count_trend:raw.orders
    kind: str  # dbt_test | partition_present | row_count_trend | freshness | reconciliation
    status: str  # pass | fail
    observed: str
    expected: str
    message: str


def _partition_rows(con: duckdb.DuckDBPyConnection, table: str, day: date) -> int:
    return con.execute(f"select count(*) from raw.{table} where _dt = ?", [day]).fetchone()[0]


def partition_present(con, day: date) -> list[CheckResult]:
    out = []
    for t in EVENT_TABLES:
        n = _partition_rows(con, t, day)
        out.append(
            CheckResult(
                f"partition_present:raw.{t}",
                "partition_present",
                "pass" if n > 0 else "fail",
                f"{n} rows",
                "> 0 rows",
                f"raw.{t} partition {day} has {n} rows",
            )
        )
    return out


def row_count_trend(con, day: date) -> list[CheckResult]:
    out = []
    for t in TREND_TABLES:
        history = [
            r[1]
            for r in con.execute(
                f"select _dt, count(*) from raw.{t} where _dt < ? group by _dt order by _dt desc limit ?",
                [day, TREND_WINDOW],
            ).fetchall()
        ]
        today = _partition_rows(con, t, day)
        if len(history) < 3:
            out.append(CheckResult(f"row_count_trend:raw.{t}", "row_count_trend", "pass", f"{today}", "n/a",
                                   f"only {len(history)} prior partitions; trend check skipped"))  # fmt: skip
            continue
        median = statistics.median(history)
        ratio = today / median if median else 0.0
        ok = TREND_LOW <= ratio <= TREND_HIGH
        out.append(
            CheckResult(
                f"row_count_trend:raw.{t}",
                "row_count_trend",
                "pass" if ok else "fail",
                f"{today} rows ({ratio:.0%} of trailing median)",
                f"{TREND_LOW:.0%}-{TREND_HIGH:.0%} of trailing {TREND_WINDOW}-partition median {median:.0f}",
                f"raw.{t} partition {day}: {today} rows vs median {median:.0f}",
            )
        )
    return out


def freshness(con, day: date) -> list[CheckResult]:
    try:
        latest = con.execute("select max(cast(ordered_at as date)) from marts.fct_orders").fetchone()[0]
    except duckdb.Error as e:
        latest, err = None, str(e).splitlines()[0]
    else:
        err = None
    ok = latest is not None and latest >= day
    return [
        CheckResult(
            "freshness:marts.fct_orders",
            "freshness",
            "pass" if ok else "fail",
            str(latest) if err is None else f"error: {err}",
            f">= {day}",
            f"latest order date in marts.fct_orders is {latest}",
        )
    ]


def reconciliation(con, day: date) -> list[CheckResult]:
    """Every order loaded to raw for the day should appear exactly once in the mart."""
    try:
        raw_n = con.execute("select count(distinct order_id) from raw.orders where _dt = ?", [day]).fetchone()[0]
        mart_n = con.execute("select count(*) from marts.fct_orders where partition_date = ?", [day]).fetchone()[0]
    except duckdb.Error as e:
        return [CheckResult("reconciliation:raw.orders->marts.fct_orders", "reconciliation", "fail",
                            f"error: {str(e).splitlines()[0]}", "equal counts", "could not reconcile")]  # fmt: skip
    return [
        CheckResult(
            "reconciliation:raw.orders->marts.fct_orders",
            "reconciliation",
            "pass" if raw_n == mart_n else "fail",
            f"raw distinct={raw_n}, mart={mart_n}",
            "equal",
            f"partition {day}: {raw_n} distinct raw orders vs {mart_n} rows in fct_orders",
        )
    ]


def dbt_test_checks(build: DbtBuildResult) -> list[CheckResult]:
    out = []
    for n in build.nodes:
        if n.resource_type != "test":
            continue
        status = "pass" if n.status == "pass" else "fail"
        out.append(
            CheckResult(
                n.unique_id.removeprefix("test.larkspur."),
                "dbt_test",
                status,
                f"{n.status}" + (f", {n.failures} failing rows" if n.failures else ""),
                "pass",
                n.message or n.status,
            )
        )
    return out


def run_checks(paths: SandboxPaths, day: date, build: DbtBuildResult) -> list[CheckResult]:
    with duckdb.connect(str(paths.warehouse)) as con:
        results = dbt_test_checks(build)
        results += partition_present(con, day) + row_count_trend(con, day) + freshness(con, day)
        results += reconciliation(con, day)
    return results


def store_results(paths: SandboxPaths, run_id: str, day: date, results: list[CheckResult]) -> None:
    with duckdb.connect(str(paths.warehouse)) as con:
        con.execute("create schema if not exists ops")
        con.execute(
            "create table if not exists ops.dq_results (run_id varchar, partition_date date, check_id varchar, "
            "kind varchar, status varchar, observed varchar, expected varchar, message varchar, "
            "checked_at timestamp default current_timestamp)"
        )
        con.executemany(
            "insert into ops.dq_results (run_id, partition_date, check_id, kind, status, observed, expected, message)"
            " values (?, ?, ?, ?, ?, ?, ?, ?)",
            [[run_id, day, r.check_id, r.kind, r.status, r.observed, r.expected, r.message] for r in results],
        )


def to_json(results: list[CheckResult]) -> str:
    return json.dumps([asdict(r) for r in results], indent=2)
