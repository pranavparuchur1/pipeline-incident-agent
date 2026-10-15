"""The nightly Dagster job: ingest one day's partition -> dbt build -> data-quality checks.

Dagster records every run (status, steps, timings, events) in its own store under
`<sandbox>/dagster_home`; the job also writes the plain-text pipeline log. Those two records
are what the agent's investigation tools read later.
"""

# No `from __future__ import annotations`: Dagster reads the Config annotations at runtime.

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import dagster as dg

from .config import SandboxPaths
from .dbt_runner import dbt_build
from .dq import CheckResult, run_checks, store_results
from .ingest import load_partition
from .runlog import RunLog

JOB_NAME = "nightly"


class PipelineConfig(dg.Config):
    sandbox_root: str
    partition: str  # YYYY-MM-DD


def _ctx(context: dg.OpExecutionContext, config: PipelineConfig) -> tuple[SandboxPaths, date, RunLog]:
    paths = SandboxPaths(Path(config.sandbox_root))
    return paths, date.fromisoformat(config.partition), RunLog(paths.pipeline_log, context.run_id, config.partition)


@dg.op
def ingest(context: dg.OpExecutionContext, config: PipelineConfig) -> int:
    paths, day, log = _ctx(context, config)
    log.info("ingest", f"loading landing files for {day}")
    try:
        loaded = load_partition(paths, day, context.run_id)
    except Exception as e:
        log.error("ingest", f"{type(e).__name__}: {e}")
        raise dg.Failure(description=str(e)) from e
    for r in loaded:
        log.info("ingest", f"loaded raw.{r.table} rows={r.rows} file={r.source_file}")
    return sum(r.rows for r in loaded)


@dg.op
def transform(context: dg.OpExecutionContext, config: PipelineConfig, rows_loaded: int) -> dict:
    paths, day, log = _ctx(context, config)
    log.info("dbt_build", f"dbt build starting (rows_loaded={rows_loaded})")
    build = dbt_build(paths)
    for n in build.nodes:
        if n.status in ("fail", "error"):
            log.error("dbt_build", f"{n.resource_type} {n.unique_id} {n.status}: {n.message}")
        elif n.status == "skipped":
            log.warn("dbt_build", f"{n.resource_type} {n.unique_id} skipped (upstream failure)")
    if build.exception:
        log.error("dbt_build", build.exception)
    models = [n for n in build.nodes if n.resource_type == "model"]
    tests = [n for n in build.nodes if n.resource_type == "test"]
    summary = (
        f"dbt build finished success={build.success} models={len(models)} tests={len(tests)} "
        f"failed={len(build.failed)} skipped={len(build.skipped)}"
    )
    (log.info if build.success else log.error)("dbt_build", summary)
    # Model errors stop the run here. Test failures don't: the dq step reports them with the rest.
    if build.exception or any(n.resource_type == "model" and n.status == "error" for n in build.nodes):
        raise dg.Failure(description=summary)
    return {"nodes": [n.__dict__ for n in build.nodes], "success": build.success}


@dg.op
def quality_checks(context: dg.OpExecutionContext, config: PipelineConfig, build: dict) -> None:
    from .dbt_runner import DbtBuildResult, NodeResult

    paths, day, log = _ctx(context, config)
    result = DbtBuildResult(build["success"], [NodeResult(**n) for n in build["nodes"]])
    checks: list[CheckResult] = run_checks(paths, day, result)
    store_results(paths, context.run_id, day, checks)
    failed = [c for c in checks if c.status == "fail"]
    for c in failed:
        log.error("dq_checks", f"check {c.check_id} FAILED observed={c.observed} expected={c.expected}: {c.message}")
    log.info("dq_checks", f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        raise dg.Failure(
            description=f"{len(failed)} data-quality checks failed: " + ", ".join(c.check_id for c in failed[:5]),
            metadata={"failed_checks": [c.check_id for c in failed]},
        )


@dg.job(name=JOB_NAME)
def nightly():
    quality_checks(transform(ingest()))


@dataclass
class RunSummary:
    run_id: str
    partition: str
    success: bool
    failed_step: str | None
    error: str | None
    step_status: dict[str, str] = field(default_factory=dict)


def run_pipeline(paths: SandboxPaths, day: date) -> RunSummary:
    """Run the job in a fresh Python process, like a real scheduler would.

    dbt-duckdb keeps the warehouse file open for the life of the process, which would block
    snapshots and later repairs; a child process releases every handle when it exits.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline_incident_agent.sandbox.pipeline", str(paths.root), day.isoformat()],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith(_RESULT_MARKER)]
    if not marker:
        tail = (proc.stderr or proc.stdout)[-1500:]
        return RunSummary("none", day.isoformat(), False, None, f"pipeline process crashed: {tail}", {})
    return RunSummary(**json.loads(marker[-1].removeprefix(_RESULT_MARKER)))


_RESULT_MARKER = "PIA_RUN_RESULT "


def run_pipeline_in_process(paths: SandboxPaths, day: date) -> RunSummary:
    paths.ensure()
    run_config = {
        "ops": {
            name: {"config": {"sandbox_root": str(paths.root), "partition": day.isoformat()}}
            for name in ("ingest", "transform", "quality_checks")
        },
        "loggers": {"console": {"config": {"log_level": "CRITICAL"}}},
    }
    with dg.DagsterInstance.from_config(str(paths.dagster_home)) as instance:
        result = nightly.execute_in_process(
            run_config=run_config,
            instance=instance,
            raise_on_error=False,
            tags={"partition": day.isoformat()},
        )
        steps: dict[str, str] = {}
        failed_step, error = None, None
        for ev in result.all_events:
            if ev.step_key is None:
                continue
            if ev.event_type_value == "STEP_SUCCESS":
                steps[ev.step_key] = "success"
            elif ev.event_type_value == "STEP_FAILURE":
                steps[ev.step_key] = "failure"
                failed_step = failed_step or ev.step_key
                err = ev.step_failure_data.error
                error = error or (err.message.strip() if err else None)
            elif ev.event_type_value == "STEP_SKIPPED":
                steps[ev.step_key] = "skipped"
        if not result.success and failed_step is None:
            error = "run failed before any step completed"
    return RunSummary(result.run_id, day.isoformat(), result.success, failed_step, error, steps)


if __name__ == "__main__":
    summary = run_pipeline_in_process(SandboxPaths(Path(sys.argv[1])), date.fromisoformat(sys.argv[2]))
    print(_RESULT_MARKER + json.dumps(asdict(summary)), flush=True)
