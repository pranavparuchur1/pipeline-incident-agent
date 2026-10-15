"""Run `dbt build` in-process and return per-node results."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import DBT_PROJECT_DIR, SandboxPaths


@dataclass
class NodeResult:
    unique_id: str
    resource_type: str  # model | test
    status: str  # success | pass | fail | error | skipped | warn
    message: str
    failures: int | None


@dataclass
class DbtBuildResult:
    success: bool
    nodes: list[NodeResult]
    exception: str | None = None

    @property
    def failed(self) -> list[NodeResult]:
        return [n for n in self.nodes if n.status in ("fail", "error")]

    @property
    def skipped(self) -> list[NodeResult]:
        return [n for n in self.nodes if n.status == "skipped"]


def dbt_build(paths: SandboxPaths, select: str | None = None) -> DbtBuildResult:
    from dbt.cli.main import dbtRunner

    os.environ["PIA_WAREHOUSE"] = str(paths.warehouse)
    args = [
        "build",
        "--project-dir", str(DBT_PROJECT_DIR),
        "--profiles-dir", str(DBT_PROJECT_DIR),
        "--target-path", str(paths.dbt_target),
        "--log-path", str(paths.logs / "dbt"),
        "--quiet",
    ]  # fmt: skip
    if select:
        args += ["--select", select]
    res = dbtRunner().invoke(args)
    nodes = []
    for r in getattr(res.result, "results", None) or []:
        node = r.node
        nodes.append(
            NodeResult(
                unique_id=node.unique_id,
                resource_type=str(node.resource_type),
                status=str(r.status),
                message=(r.message or "").strip(),
                failures=r.failures,
            )
        )
    exc = f"{type(res.exception).__name__}: {res.exception}" if res.exception else None
    return DbtBuildResult(success=bool(res.success), nodes=nodes, exception=exc)
