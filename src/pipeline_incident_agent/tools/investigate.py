"""Read-only investigation tools.

Every tool returns a ToolResult whose `evidence` items carry stable ids (log:412,
landing:orders@2026-10-01, model:fct_orders, ...). Diagnoses must cite these ids, and the
trace keeps the full text behind each one, so every claim can be traced back to what was seen.

What the tools can see is deliberately limited to what an on-call engineer could see: the
warehouse, the pipeline log, Dagster run history, landing files, the dbt project, the ingest
config and the runbooks. The simulator's internals (upstream change list, snapshots, the
incident catalog, reference fingerprints) are not reachable through any tool.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import duckdb

from ..sandbox import dq
from ..sandbox.config import DBT_PROJECT_DIR, EVENT_TABLES, SandboxPaths
from ..sandbox.ingest import RAW_CONTRACT, load_ingest_config
from .runbooks import RunbookIndex

MAX_ROWS = 50
MAX_CELL = 200


@dataclass
class Evidence:
    id: str
    kind: str  # run | log | schema | landing | check | sql | model | config | chart | runbook
    text: str  # the full text an evidence id stands for (kept in the trace)


@dataclass
class ToolResult:
    ok: bool
    summary: str  # what the model reads first
    data: dict | list | None = None
    evidence: list[Evidence] = field(default_factory=list)
    images: list[str] = field(default_factory=list)  # PNG paths returned to the model as images

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("images")
        return json.dumps(d, default=str)


class ToolError(ValueError):
    pass


def _ro(paths: SandboxPaths) -> duckdb.DuckDBPyConnection:
    """Read-only warehouse connection with no file or network access (no read_csv, no ATTACH)."""
    if not paths.warehouse.exists():
        raise ToolError("warehouse not found")
    return duckdb.connect(str(paths.warehouse), read_only=True, config={"enable_external_access": False})


def _cell(v) -> str | int | float | None:
    if v is None or isinstance(v, int | float):
        return v
    s = str(v)
    return s if len(s) <= MAX_CELL else s[:MAX_CELL] + "..."


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise ToolError(f"day must be YYYY-MM-DD, got {value!r}") from e


# ------------------------------------------------------------------ run history


def get_job_status(paths: SandboxPaths, limit: int = 5) -> ToolResult:
    """Most recent runs of the nightly job, newest first, with per-step status and failure messages."""
    import dagster as dg

    limit = max(1, min(int(limit), 20))
    runs, evidence = [], []
    with dg.DagsterInstance.from_config(str(paths.dagster_home)) as inst:
        for r in inst.get_runs(limit=limit):
            steps, failures = {}, []
            for rec in inst.all_logs(r.run_id):
                ev = rec.dagster_event
                if ev is None or ev.step_key is None:
                    continue
                kind = ev.event_type_value
                if kind == "STEP_SUCCESS":
                    steps[ev.step_key] = "success"
                elif kind == "STEP_FAILURE":
                    steps[ev.step_key] = "failure"
                    err = ev.step_failure_data.error
                    failures.append(f"{ev.step_key}: {(err.message.strip() if err else '')[:500]}")
                elif kind == "STEP_SKIPPED":
                    steps[ev.step_key] = "skipped"
            stats = inst.get_run_stats(r.run_id)
            row = {
                "run_id": r.run_id[:8],
                "partition": r.tags.get("partition"),
                "status": r.status.value,
                "steps": steps,
                "failures": failures,
                "duration_s": round(stats.end_time - stats.start_time, 1) if stats.end_time else None,
            }
            runs.append(row)
            evidence.append(Evidence(f"run:{row['run_id']}", "run", json.dumps(row)))
    failed = [r for r in runs if r["status"] != "SUCCESS"]
    summary = f"{len(runs)} most recent runs; {len(failed)} not successful"
    if failed:
        f = failed[0]
        summary += f"; latest failure run {f['run_id']} partition {f['partition']}: {'; '.join(f['failures'])[:300]}"
    return ToolResult(True, summary, runs, evidence)


def search_logs(
    paths: SandboxPaths, query: str = "", run_id: str | None = None, level: str | None = None, limit: int = 40
) -> ToolResult:
    """Lines of the pipeline log containing every query term (case-insensitive). Ids are line numbers."""
    if not paths.pipeline_log.exists():
        return ToolResult(False, "pipeline log not found")
    terms = [t.lower() for t in query.split()]
    limit = max(1, min(int(limit), 200))
    hits = []
    for n, line in enumerate(paths.pipeline_log.read_text(encoding="utf-8").splitlines(), start=1):
        low = line.lower()
        if run_id and f"[run={run_id[:8]} " not in line:
            continue
        if level and f" {level.upper()} " not in line[:40]:
            continue
        if all(t in low for t in terms):
            hits.append((n, line))
    shown = hits[-limit:]  # most recent matches
    evidence = [Evidence(f"log:{n}", "log", line) for n, line in shown]
    data = [{"id": f"log:{n}", "line": line[:600]} for n, line in shown]
    return ToolResult(True, f"{len(hits)} matching lines; showing the last {len(shown)}", data, evidence)


# ------------------------------------------------------------------ schemas and files


def inspect_schema(paths: SandboxPaths, relation: str) -> ToolResult:
    """Columns and row count of a warehouse table/view (schema.table); for raw tables also the
    contract and per-partition row counts for the last 10 partitions."""
    m = re.fullmatch(r"(raw|staging|marts|ops)\.([a-z_][a-z0-9_]*)", relation or "")
    if not m:
        raise ToolError("relation must look like raw.orders / staging.stg_orders / marts.fct_orders")
    schema, table = m.groups()
    with _ro(paths) as con:
        cols = con.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = ? and table_name = ? order by ordinal_position",
            [schema, table],
        ).fetchall()
        if not cols:
            return ToolResult(False, f"{relation} does not exist")
        data: dict = {"relation": relation, "columns": [{"name": c, "type": t} for c, t in cols]}
        try:
            data["row_count"] = con.execute(f'select count(*) from "{schema}"."{table}"').fetchone()[0]
        except duckdb.Error as e:
            data["row_count_error"] = str(e).splitlines()[0]
        if schema == "raw" and table in RAW_CONTRACT:
            data["contract"] = RAW_CONTRACT[table]
            if table in EVENT_TABLES:
                data["partitions"] = [
                    {"partition": str(d), "rows": n}
                    for d, n in con.execute(
                        f"select _dt, count(*) from raw.{table} group by 1 order by 1 desc limit 10"
                    ).fetchall()
                ]
    text = json.dumps(data, default=str)
    return ToolResult(True, f"{relation}: {len(cols)} columns, {data.get('row_count')} rows", data,
                      [Evidence(f"schema:{relation}", "schema", text)])  # fmt: skip


def inspect_landing_file(paths: SandboxPaths, table: str, day: str) -> ToolResult:
    """What upstream delivered for one table and day: existence, header vs. the raw contract
    (after the current ingest config), row count, a few raw lines, and CSV parse problems."""
    if table not in EVENT_TABLES:
        raise ToolError(f"table must be one of {list(EVENT_TABLES)}")
    d = _day(day)
    path = paths.landing_file(table, d)
    eid = f"landing:{table}@{d}"
    if not path.exists():
        text = f"no file at landing/{table}/dt={d}/{table}.csv"
        siblings = sorted(p.name for p in path.parent.parent.glob("dt=*"))[-5:] if path.parent.parent.exists() else []
        return ToolResult(True, f"{eid}: FILE MISSING", {"exists": False, "recent_partitions": siblings},
                          [Evidence(eid, "landing", text + f"; recent partitions present: {siblings}")])  # fmt: skip
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    header = lines[0].split(",") if lines else []
    cfg = load_ingest_config(paths).get(table, {})
    rename, ignore = cfg.get("rename", {}), set(cfg.get("ignore_columns", []))
    mapped = {rename.get(c, c) for c in header if c not in ignore}
    contract = list(RAW_CONTRACT[table])
    problems = []
    width = len(header)
    for n, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if n > 1 and len(row) != width:
            problems.append(f"line {n}: {len(row)} fields, header has {width}")
    data = {
        "exists": True,
        "bytes": len(raw),
        "ends_with_newline": raw.endswith(b"\n"),
        "header": header,
        "data_rows": max(0, len(lines) - 1),
        "missing_contract_columns": [c for c in contract if c not in mapped],
        "columns_not_in_contract": [c for c in header if c not in ignore and rename.get(c, c) not in contract],
        "ingest_config": cfg,
        "first_lines": lines[1:4],
        "last_line": lines[-1] if len(lines) > 1 else None,
        "parse_problems": problems[:5],
    }
    summary = f"{eid}: {data['data_rows']} data rows, header {header}"
    if data["missing_contract_columns"] or data["columns_not_in_contract"]:
        summary += (f"; CONTRACT MISMATCH missing={data['missing_contract_columns']} "
                    f"extra={data['columns_not_in_contract']}")  # fmt: skip
    if problems:
        summary += f"; {len(problems)} malformed lines"
    return ToolResult(True, summary, data, [Evidence(eid, "landing", json.dumps(data))])


def get_ingest_config(paths: SandboxPaths) -> ToolResult:
    cfg = load_ingest_config(paths)
    text = json.dumps(cfg, sort_keys=True)
    return ToolResult(True, "ingest config: " + (text if cfg else "{} (defaults)"), cfg,
                      [Evidence("config:ingest", "config", text)])  # fmt: skip


# ------------------------------------------------------------------ checks and SQL


def run_data_quality_checks(paths: SandboxPaths, day: str) -> ToolResult:
    """Recompute the volume/completeness/freshness checks for a partition now (read-only), and
    return the dbt test + check results stored by the latest run of that partition."""
    d = _day(day)
    with _ro(paths) as con:
        live = dq.partition_present(con, d) + dq.row_count_trend(con, d) + dq.freshness(con, d)
        live += dq.reconciliation(con, d)
        try:
            stored = con.execute(
                "select run_id, check_id, status, observed, expected, message from ops.dq_results "
                "where partition_date = ? and run_id = (select run_id from ops.dq_results where partition_date = ? "
                "order by checked_at desc limit 1) order by status, check_id",
                [d, d],
            ).fetchall()
        except duckdb.Error:
            stored = []
    evidence, data = [], {"live": [], "latest_run": []}
    for c in live:
        evidence.append(Evidence(f"check:{c.check_id}@live", "check", json.dumps(asdict(c))))
        data["live"].append(asdict(c))
    for run, cid, status, obs, exp, msg in stored:
        row = {"run_id": run[:8], "check_id": cid, "status": status, "observed": obs, "expected": exp,
               "message": _cell(msg)}  # fmt: skip
        evidence.append(Evidence(f"check:{cid}@{run[:8]}", "check", json.dumps(row)))
        data["latest_run"].append(row)
    live_failed = [c.check_id for c in live if c.status == "fail"]
    run_failed = [r["check_id"] for r in data["latest_run"] if r["status"] == "fail"]
    summary = f"live checks failing: {live_failed or 'none'}; latest stored run failing: {run_failed or 'none'}"
    if not stored:
        summary += " (no stored results for this partition: the run may have failed before quality_checks)"
    return ToolResult(True, summary, data, evidence)


_FORBIDDEN_SQL = re.compile(r"\b(attach|copy|export|import|install|load|pragma|set|call|create|insert|update|delete|"
                            r"drop|alter|checkpoint|vacuum)\b", re.I)  # fmt: skip


def query_table(paths: SandboxPaths, sql: str) -> ToolResult:
    """Run one read-only SELECT (or WITH ... SELECT) against the warehouse; at most 50 rows returned."""
    stmt = (sql or "").strip().rstrip(";")
    if ";" in stmt:
        raise ToolError("one statement only")
    if not re.match(r"^(select|with)\b", stmt, re.I):
        raise ToolError("only SELECT / WITH queries are allowed")
    if _FORBIDDEN_SQL.search(stmt):
        raise ToolError("statement contains a forbidden keyword")
    eid = "sql:" + hashlib.sha1(stmt.encode()).hexdigest()[:8]
    with _ro(paths) as con:
        try:
            cur = con.execute(f"select * from ({stmt}) limit {MAX_ROWS + 1}")
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        except duckdb.Error as e:
            msg = str(e).splitlines()[0]
            return ToolResult(False, f"query error: {msg}", None, [Evidence(eid, "sql", f"{stmt}\n-> error: {msg}")])
    truncated = len(rows) > MAX_ROWS
    rows = [[_cell(v) for v in r] for r in rows[:MAX_ROWS]]
    data = {"columns": cols, "rows": rows, "truncated": truncated}
    text = f"{stmt}\n-> {json.dumps(data, default=str)}"
    return ToolResult(True, f"{len(rows)} rows" + (" (truncated)" if truncated else ""), data,
                      [Evidence(eid, "sql", text)])  # fmt: skip


# ------------------------------------------------------------------ dbt project


def get_dbt_model(paths: SandboxPaths, model: str) -> ToolResult:
    """Current SQL of a dbt model, its diff against the deployed (version-controlled) copy, and its refs."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", model or ""):
        raise ToolError("model must be a dbt model name like fct_orders")
    deployed = list(DBT_PROJECT_DIR.glob(f"models/**/{model}.sql"))
    if len(deployed) != 1:
        names = sorted(p.stem for p in DBT_PROJECT_DIR.glob("models/**/*.sql"))
        raise ToolError(f"unknown model {model!r}; models: {names}")
    rel = deployed[0].relative_to(DBT_PROJECT_DIR)
    current = paths.dbt_project / rel
    cur_sql = current.read_text(encoding="utf-8") if current.exists() else ""
    dep_sql = deployed[0].read_text(encoding="utf-8")
    diff = "".join(difflib.unified_diff(dep_sql.splitlines(True), cur_sql.splitlines(True),
                                        "deployed/" + rel.as_posix(), "running/" + rel.as_posix()))  # fmt: skip
    refs = sorted(set(re.findall(r"ref\('([a-z0-9_]+)'\)", cur_sql)))
    children = sorted(
        p.stem for p in (paths.dbt_project / "models").glob("**/*.sql") if f"ref('{model}')" in p.read_text("utf-8")
    )
    data = {"path": rel.as_posix(), "sql": cur_sql, "changed_vs_deployed": bool(diff), "diff": diff,
            "upstream_refs": refs, "downstream_models": children}  # fmt: skip
    summary = f"model {model}: {'CHANGED vs deployed' if diff else 'matches deployed'}; refs {refs}; used by {children}"
    return ToolResult(True, summary, data, [Evidence(f"model:{model}", "model", json.dumps(data))])


def list_changed_models(paths: SandboxPaths) -> ToolResult:
    """Which dbt models in the running project differ from the deployed version."""
    changed = []
    for dep in sorted(DBT_PROJECT_DIR.glob("models/**/*.sql")):
        cur = paths.dbt_project / dep.relative_to(DBT_PROJECT_DIR)
        if not cur.exists() or cur.read_bytes() != dep.read_bytes():
            changed.append(dep.stem)
    text = json.dumps({"changed_models": changed})
    return ToolResult(True, f"changed models: {changed or 'none'}", {"changed_models": changed},
                      [Evidence("model:changes", "model", text)])  # fmt: skip


# ------------------------------------------------------------------ charts (multimodal evidence)


def render_chart(paths: SandboxPaths, table: str, by: str | None = None, days: int = 21, out_dir: Path | None = None):
    """PNG of daily row counts for a raw table over the last `days` partitions, optionally split by
    `channel` or `hour` (orders only). Returned to the model as an image."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if table not in EVENT_TABLES:
        raise ToolError(f"table must be one of {list(EVENT_TABLES)}")
    if by not in (None, "channel", "hour"):
        raise ToolError("by must be null, 'channel' or 'hour'")
    if by and table != "orders":
        raise ToolError("splitting by channel/hour is only available for orders")
    days = max(7, min(int(days), 60))
    with _ro(paths) as con:
        if by is None:
            rows = con.execute(
                f"select _dt, 'all' as k, count(*) from raw.{table} where _dt >= "
                f"(select max(_dt) from raw.{table}) - interval {days - 1} day group by 1, 2 order by 1"
            ).fetchall()
        else:
            key = "channel" if by == "channel" else "lpad(cast(hour(order_ts) // 6 * 6 as varchar), 2, '0') || 'h+'"
            rows = con.execute(
                f"select _dt, {key} as k, count(*) from raw.orders where _dt >= "
                f"(select max(_dt) from raw.orders) - interval {days - 1} day group by 1, 2 order by 1, 2"
            ).fetchall()
    series: dict[str, dict] = {}
    for d, k, n in rows:
        series.setdefault(str(k), {})[d] = n
    all_days = sorted({d for d, _, _ in rows})
    fig, ax = plt.subplots(figsize=(9, 4), dpi=100)
    for k, pts in sorted(series.items()):
        ax.plot(all_days, [pts.get(d, 0) for d in all_days], marker="o", label=k)
    title = f"raw.{table} rows per partition" + (f" by {by}" if by else "")
    ax.set_title(title)
    ax.set_xticks(all_days)
    ax.set_xticklabels([d.strftime("%m-%d") for d in all_days], rotation=60)
    ax.set_ylim(bottom=0)
    ax.grid(alpha=0.3)
    if by:
        ax.legend()
    fig.tight_layout()
    out_dir = out_dir or (paths.root / "charts")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{table}{'_by_' + by if by else ''}"
    png = out_dir / f"{name}.png"
    fig.savefig(png)
    plt.close(fig)
    eid = f"chart:{name}"
    table_text = json.dumps({k: {str(d): n for d, n in pts.items()} for k, pts in series.items()})
    return ToolResult(True, f"{title}, {len(all_days)} partitions (image attached)", None,
                      [Evidence(eid, "chart", table_text)], [str(png)])  # fmt: skip


# ------------------------------------------------------------------ runbooks


_INDEX: RunbookIndex | None = None


def search_runbooks(paths: SandboxPaths, query: str, k: int = 3) -> ToolResult:
    """Keyword (BM25) search over the team's runbooks and architecture docs; returns whole sections."""
    global _INDEX
    if _INDEX is None:
        _INDEX = RunbookIndex.load()
    hits = _INDEX.search(query, k=max(1, min(int(k), 6)))
    evidence = [Evidence(h.id, "runbook", h.text) for h in hits]
    data = [{"id": h.id, "title": h.title, "score": round(h.score, 2), "text": h.text[:2500]} for h in hits]
    return ToolResult(True, f"{len(hits)} sections: {[h.id for h in hits]}", data, evidence)
