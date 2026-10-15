"""Tool registry, role permissions, and the trace every call is written to.

Roles (least privilege):
  worker  read-only investigation tools
  master  the same, plus propose_fix

propose_fix does not touch the sandbox. It validates the proposal against the remediation
allow-list, checks that every cited evidence id was actually returned by a tool earlier in this
investigation, and records it. The harness applies and verifies it afterwards (Phase 5), so the
agent can never mark its own fix as working.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..sandbox.config import SandboxPaths
from ..sandbox.remediation import ACTIONS
from . import investigate as inv
from .investigate import Evidence, ToolError, ToolResult


class PermissionDenied(PermissionError):
    pass


class Trace:
    """Append-only JSONL record of one investigation. Shared by the master's and workers' tool servers."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "trace.jsonl"

    def append(self, event: dict) -> None:
        event = {"ts": time.time(), **event}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(ln) for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def evidence(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for e in self.events():
            for ev in e.get("evidence", []):
                out[ev["id"]] = ev["text"]
        return out


@dataclass(frozen=True)
class ToolSpec:
    name: str
    scope: str  # read | remediate
    fn: Callable[..., ToolResult]
    description: str
    params: dict  # JSON-schema "properties"
    required: tuple[str, ...] = ()


def _s(desc: str, **extra) -> dict:
    return {"type": "string", "description": desc, **extra}


def _i(desc: str, default: int) -> dict:
    return {"type": "integer", "description": desc, "default": default}


DAY = _s("partition date, YYYY-MM-DD")
EVENT_TABLE = _s("event table", enum=["customers", "orders", "order_items", "payments"])


def propose_fix(paths: SandboxPaths, trace: Trace, actions: list, rationale: str, evidence_ids: list) -> ToolResult:
    if not isinstance(actions, list) or not actions:
        raise ToolError("actions must be a non-empty list of {action, params}")
    for a in actions:
        if not isinstance(a, dict) or a.get("action") not in ACTIONS:
            raise ToolError(f"unknown action in {a!r}; allowed: {sorted(ACTIONS)}")
        expected = set(ACTIONS[a["action"]].params)
        got = set(a.get("params") or {})
        if got != expected:
            raise ToolError(f"{a['action']} needs params {sorted(expected)}, got {sorted(got)}")
    seen = trace.evidence()
    unknown = [e for e in evidence_ids or [] if e not in seen]
    if not evidence_ids:
        raise ToolError("cite at least one evidence id returned by an earlier tool call")
    if unknown:
        raise ToolError(f"these evidence ids were never returned by a tool: {unknown}")
    proposal = {"actions": actions, "rationale": rationale, "evidence_ids": evidence_ids}
    trace.append({"type": "proposal", **proposal})
    return ToolResult(True, "proposal recorded. It will be applied in the sandbox and verified by rerunning the "
                            "pipeline after you finish; calling propose_fix again replaces it.", proposal)  # fmt: skip


TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in (
        ToolSpec("get_job_status", "read", inv.get_job_status,
                 "Recent runs of the nightly pipeline job with per-step status and failure messages.",
                 {"limit": _i("how many recent runs", 5)}),
        ToolSpec("search_logs", "read", inv.search_logs,
                 "Search the pipeline log. Returns lines containing every query term, with ids log:<line>.",
                 {"query": _s("space-separated terms, e.g. 'ERROR orders'"),
                  "run_id": _s("optional 8-char run id"), "level": _s("optional INFO|WARN|ERROR"),
                  "limit": _i("max lines", 40)}, ("query",)),
        ToolSpec("inspect_schema", "read", inv.inspect_schema,
                 "Columns, types and row count of a warehouse relation; raw tables also show the contract "
                 "and recent per-partition row counts.",
                 {"relation": _s("schema.table, e.g. raw.orders or marts.fct_orders")}, ("relation",)),
        ToolSpec("inspect_landing_file", "read", inv.inspect_landing_file,
                 "Inspect the file upstream delivered for a table and day: header vs contract, rows, malformed lines.",
                 {"table": EVENT_TABLE, "day": DAY}, ("table", "day")),
        ToolSpec("get_ingest_config", "read", inv.get_ingest_config,
                 "Current ingest settings (column mappings, parsers, ignored columns, dedupe keys).", {}),
        ToolSpec("run_data_quality_checks", "read", inv.run_data_quality_checks,
                 "Recompute volume/completeness/freshness checks for a partition and show the latest stored "
                 "dbt test and check results.", {"day": DAY}, ("day",)),
        ToolSpec("query_table", "read", inv.query_table,
                 "Run one read-only SELECT against the warehouse (schemas raw, staging, marts, ops); max 50 rows.",
                 {"sql": _s("a single SELECT or WITH query")}, ("sql",)),
        ToolSpec("get_dbt_model", "read", inv.get_dbt_model,
                 "SQL of a dbt model, its diff vs the deployed version, and its upstream/downstream models.",
                 {"model": _s("dbt model name, e.g. fct_orders")}, ("model",)),
        ToolSpec("list_changed_models", "read", inv.list_changed_models,
                 "Which dbt models differ from the deployed version.", {}),
        ToolSpec("render_chart", "read", inv.render_chart,
                 "Chart of daily row counts for a raw table (optionally orders split by channel or hour), "
                 "returned as an image.",
                 {"table": EVENT_TABLE, "by": _s("optional: channel or hour (orders only)"),
                  "days": _i("partitions to show", 21)}, ("table",)),
        ToolSpec("search_runbooks", "read", inv.search_runbooks,
                 "Keyword search over the team's runbooks and architecture docs.",
                 {"query": _s("what you are looking for"), "k": _i("sections to return", 3)}, ("query",)),
        ToolSpec("propose_fix", "remediate", propose_fix,
                 "Propose the remediation (a sequence of allow-listed actions) with rationale and the evidence ids "
                 "that support it. Does not apply anything; the harness applies and verifies it afterwards.",
                 {"actions": {"type": "array", "description": "[{action, params}] from the remediation allow-list",
                              "items": {"type": "object"}},
                  "rationale": _s("why this addresses the root cause"),
                  "evidence_ids": {"type": "array", "items": {"type": "string"},
                                   "description": "ids returned by earlier tool calls"}},
                 ("actions", "rationale", "evidence_ids")),
    )
}  # fmt: skip

ROLES = {
    "worker": tuple(n for n, t in TOOLS.items() if t.scope == "read"),
    "master": tuple(TOOLS),
}


def call_tool(paths: SandboxPaths, trace: Trace, role: str, name: str, args: dict, **extra) -> ToolResult:
    if role not in ROLES:
        raise PermissionDenied(f"unknown role {role!r}")
    if name not in ROLES[role]:
        trace.append({"type": "denied", "role": role, "tool": name, "args": args})
        raise PermissionDenied(f"role {role!r} may not call {name!r}")
    spec = TOOLS[name]
    t0 = time.monotonic()
    try:
        if name == "propose_fix":
            result = spec.fn(paths, trace, **args)
        elif name == "render_chart":
            result = spec.fn(paths, **args, out_dir=trace.dir / "charts")
        else:
            result = spec.fn(paths, **args)
    except (ToolError, TypeError) as e:
        result = ToolResult(False, f"error: {e}")
    trace.append({
        "type": "tool_call", "role": role, "tool": name, "args": args, "ok": result.ok, "summary": result.summary,
        "evidence": [{"id": e.id, "kind": e.kind, "text": e.text} for e in result.evidence],
        "images": result.images, "ms": round((time.monotonic() - t0) * 1000), **extra,
    })  # fmt: skip
    return result


__all__ = ["TOOLS", "ROLES", "Trace", "call_tool", "PermissionDenied", "Evidence"]
