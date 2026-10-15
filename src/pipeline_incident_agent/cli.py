import json
import shutil
import time
from datetime import date, timedelta

import typer

app = typer.Typer(help="Pipeline incident agent", no_args_is_help=True)
sandbox_app = typer.Typer(help="Build, run, snapshot and restore the sandbox pipeline", no_args_is_help=True)
app.add_typer(sandbox_app, name="sandbox")


@app.callback()
def main() -> None:
    """Pipeline incident agent."""


@app.command()
def version() -> None:
    """Print the package version."""
    from importlib.metadata import version as v

    print(v("pipeline-incident-agent"))


def _print_run(s) -> None:
    status = "SUCCESS" if s.success else f"FAILED at {s.failed_step}"
    steps = " ".join(f"{k}={v}" for k, v in s.step_status.items())
    print(f"{s.partition} run={s.run_id[:8]} {status}  [{steps}]")
    if s.error:
        print(f"    error: {s.error[:300]}")


@sandbox_app.command("init")
def sandbox_init(days: int = typer.Option(None, help="history days (default 30)")) -> None:
    """Wipe the sandbox, generate history, run the pipeline for every day, save snapshot 'baseline'."""
    from pipeline_incident_agent.sandbox.config import HISTORY_DAYS, HISTORY_START, INCIDENT_DAY, default_paths
    from pipeline_incident_agent.sandbox.generator import generate_day, generate_products
    from pipeline_incident_agent.sandbox.pipeline import run_pipeline
    from pipeline_incident_agent.sandbox.snapshot import snapshot

    paths = default_paths()
    days = days or HISTORY_DAYS
    if paths.root.exists():
        shutil.rmtree(paths.root)
    paths.ensure()
    generate_products(paths)
    t0 = time.monotonic()
    for i in range(days):
        d = HISTORY_START + timedelta(days=i)
        generate_day(paths, d, HISTORY_START)
        s = run_pipeline(paths, d)
        _print_run(s)
        if not s.success:
            raise typer.Exit(1)
    # The next day's files are delivered but not yet processed: that's where incidents get injected.
    next_day = HISTORY_START + timedelta(days=days)
    generate_day(paths, next_day, HISTORY_START)
    print(
        f"history built in {time.monotonic() - t0:.0f}s; landing files ready for {next_day}"
        + ("" if next_day == INCIDENT_DAY else f" (note: INCIDENT_DAY is {INCIDENT_DAY})")
    )
    print("snapshot:", snapshot(paths, "baseline"))


@sandbox_app.command("run")
def sandbox_run(day: str = typer.Argument(..., help="partition date YYYY-MM-DD")) -> None:
    """Run the nightly job for one partition."""
    from pipeline_incident_agent.sandbox.config import default_paths
    from pipeline_incident_agent.sandbox.pipeline import run_pipeline

    s = run_pipeline(default_paths(), date.fromisoformat(day))
    _print_run(s)
    raise typer.Exit(0 if s.success else 1)


@sandbox_app.command("snapshot")
def sandbox_snapshot(name: str) -> None:
    """Save the current sandbox under NAME."""
    from pipeline_incident_agent.sandbox.config import default_paths
    from pipeline_incident_agent.sandbox.snapshot import snapshot

    print(snapshot(default_paths(), name))


@sandbox_app.command("restore")
def sandbox_restore(name: str = typer.Argument("baseline")) -> None:
    """Replace the sandbox with snapshot NAME."""
    from pipeline_incident_agent.sandbox.config import default_paths
    from pipeline_incident_agent.sandbox.snapshot import restore

    restore(default_paths(), name)
    print(f"restored {name}")


@sandbox_app.command("fingerprint")
def sandbox_fingerprint(as_json: bool = typer.Option(False, "--json")) -> None:
    """Content hash of every raw/staging/marts table."""
    from pipeline_incident_agent.sandbox.config import default_paths
    from pipeline_incident_agent.sandbox.snapshot import fingerprint

    fp = fingerprint(default_paths())
    print(json.dumps(fp, indent=2) if as_json else "\n".join(f"{k:<28} {v}" for k, v in fp.items()))


@sandbox_app.command("status")
def sandbox_status(last: int = 5) -> None:
    """Recent runs and the latest data-quality results."""
    import dagster as dg
    import duckdb

    from pipeline_incident_agent.sandbox.config import default_paths
    from pipeline_incident_agent.sandbox.snapshot import list_snapshots

    paths = default_paths()
    with dg.DagsterInstance.from_config(str(paths.dagster_home)) as inst:
        for r in inst.get_runs(limit=last):
            print(f"{r.tags.get('partition')} run={r.run_id[:8]} {r.status.value}")
    with duckdb.connect(str(paths.warehouse), read_only=True) as con:
        print(con.sql(
            "select partition_date, count(*) checks, count(*) filter (where status='fail') failed "
            "from ops.dq_results group by 1 order by 1 desc limit 3"
        ))  # fmt: skip
    print("snapshots:", ", ".join(list_snapshots(paths)) or "none")


incidents_app = typer.Typer(help="Inject incidents and prove the catalog", no_args_is_help=True)
app.add_typer(incidents_app, name="incidents")


@incidents_app.command("list")
def incidents_list() -> None:
    """Show the incident catalog."""
    from pipeline_incident_agent.incidents.catalog import INCIDENTS

    for i in INCIDENTS:
        print(f"{i.id:<3} {i.family:<18} {i.outcome:<8} {i.title}")


@incidents_app.command("inject")
def incidents_inject(incident_id: str) -> None:
    """Restore the baseline and inject one incident into the incident day (does not run the pipeline)."""
    from pipeline_incident_agent.incidents.catalog import BY_ID
    from pipeline_incident_agent.incidents.harness import inject
    from pipeline_incident_agent.sandbox.config import INCIDENT_DAY, default_paths

    inject(default_paths(), BY_ID[incident_id])
    print(f"injected {incident_id} into {INCIDENT_DAY}; run `pia sandbox run {INCIDENT_DAY}` to see it fail")


@incidents_app.command("gate")
def incidents_gate(
    ids: str = typer.Option(None, help="comma-separated incident ids (default: all)"),
    out: str = typer.Option(None, help="write the JSON report here"),
) -> None:
    """For each incident: prove it breaks, its true fixes recover, and its decoys don't."""
    from pathlib import Path

    from pipeline_incident_agent.incidents.harness import run_gate
    from pipeline_incident_agent.sandbox.config import default_paths

    t0 = time.monotonic()
    reports = run_gate(default_paths(), ids.split(",") if ids else None, Path(out) if out else None)
    trials = [t for r in reports for t in r.trials]
    print(f"\n{sum(r.passed for r in reports)}/{len(reports)} incidents passed; "
          f"{sum(t.passed for t in trials)}/{len(trials)} trials; {time.monotonic() - t0:.0f}s")  # fmt: skip
    raise typer.Exit(0 if all(r.passed for r in reports) else 1)


@sandbox_app.command("fix")
def sandbox_fix(action: str, params: str = typer.Argument("{}", help="JSON object of action params")) -> None:
    """Apply one allow-listed remediation action (does not rerun the pipeline)."""
    from pipeline_incident_agent.sandbox.config import default_paths
    from pipeline_incident_agent.sandbox.remediation import apply_action

    print(apply_action(default_paths(), action, json.loads(params)))


agent_app = typer.Typer(help="Run the incident agent", no_args_is_help=True)
app.add_typer(agent_app, name="agent")


@agent_app.command("run")
def agent_run(
    incidents: str = typer.Argument(..., help="comma-separated incident ids, e.g. S1,F1"),
    config: str = typer.Option("opus+sonnet", help="agent preset: opus+sonnet | sonnet-only | opus-solo | ..."),
) -> None:
    """Inject each incident, page the agent, apply and verify its fix, grade, and save the trace under runs/."""
    from pipeline_incident_agent.agent.config import PRESETS
    from pipeline_incident_agent.agent.episode import run_ids
    from pipeline_incident_agent.sandbox.config import default_paths

    run_ids(incidents.split(","), PRESETS[config], default_paths())
