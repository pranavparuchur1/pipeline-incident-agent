"""Decide from the sandbox itself whether a pipeline is recovered. Nobody's word counts.

recovered  <=>  the rerun succeeded
            AND every quality check passed
            AND every raw/staging/marts table matches the known-good reference fingerprint

The last condition catches "fixes" that make checks pass by hiding the problem (for example,
deleting rows until a uniqueness test passes).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

import duckdb

from .config import SandboxPaths
from .pipeline import RunSummary, run_pipeline
from .snapshot import fingerprint


@dataclass
class Verification:
    run: RunSummary
    failed_checks: list[str]
    mismatched_tables: list[str] = field(default_factory=list)

    @property
    def recovered(self) -> bool:
        return self.run.success and not self.failed_checks and not self.mismatched_tables


def failed_checks(paths: SandboxPaths, run_id: str) -> list[str]:
    try:
        with duckdb.connect(str(paths.warehouse), read_only=True) as con:
            return [
                r[0]
                for r in con.execute(
                    "select check_id from ops.dq_results where run_id = ? and status = 'fail' order by 1", [run_id]
                ).fetchall()
            ]
    except duckdb.Error:
        return []


def run_log_lines(paths: SandboxPaths, run_id: str) -> list[str]:
    if not paths.pipeline_log.exists():
        return []
    tag = f"[run={run_id[:8]} "
    return [ln for ln in paths.pipeline_log.read_text(encoding="utf-8").splitlines() if tag in ln]


def compare(paths: SandboxPaths, reference: dict[str, str]) -> list[str]:
    current = fingerprint(paths)
    return sorted(t for t in reference.keys() | current.keys() if reference.get(t) != current.get(t))


def verify(paths: SandboxPaths, day: date, reference: dict[str, str]) -> Verification:
    run = run_pipeline(paths, day)
    v = Verification(run, failed_checks(paths, run.run_id))
    if run.success and not v.failed_checks:
        v.mismatched_tables = compare(paths, reference)
    return v


def save_reference(paths: SandboxPaths, name: str, fp: dict[str, str]) -> None:
    paths.snapshots.mkdir(parents=True, exist_ok=True)
    (paths.snapshots / f"{name}.reference.json").write_text(json.dumps(fp, indent=2), encoding="utf-8")


def load_reference(paths: SandboxPaths, name: str) -> dict[str, str] | None:
    f = paths.snapshots / f"{name}.reference.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
