"""Workers: separate, cheaper Claude sessions the master delegates to.

A worker is its own `claude -p` process with its own MCP tool server in the read-only `worker`
role, so the permission boundary is enforced by process and server, not by a prompt. Its findings
come back to the master as a ToolResult with a `worker:<n>` evidence id whose text is the
findings; the ids the worker cites must already exist in the shared trace, or they are dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..llm.claude_cli import ClaudeCLI, ClaudeCLIError
from ..sandbox.config import SandboxPaths
from ..tools.investigate import Evidence, ToolError, ToolResult
from .config import AgentConfig
from .prompts import worker_prompt
from .schema import WORKER_SCHEMA, WORKERS


def record_llm_call(trace, agent: str, role: str, r, *, task: str | None = None) -> None:
    trace.append({
        "type": "llm_call", "agent": agent, "role": role, "model": r.model, "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens, "cache_read_tokens": r.cache_read_tokens,
        "cache_write_tokens": r.cache_write_tokens, "api_equiv_usd": r.api_equiv_usd, "duration_ms": r.duration_ms,
        "num_turns": r.num_turns, "tool_uses": [t["name"] for t in r.tool_uses()], "task": task,
        "final_text": r.text[:4000], "structured": r.structured,
    })  # fmt: skip


def write_mcp_config(paths: SandboxPaths, trace_dir: Path, role: str, agent: str) -> Path:
    from ..tools.mcp_server import mcp_config

    f = trace_dir / f"mcp_{agent}.json"
    f.write_text(json.dumps(mcp_config(role, paths.root, trace_dir, agent)), encoding="utf-8")
    return f


def allowed_tools(role: str, cfg: AgentConfig) -> list[str]:
    from ..tools.mcp_server import allowed_tool_names
    from ..tools.registry import ROLES

    return [
        n for n, short in zip(allowed_tool_names(role), ROLES[role], strict=True) if short not in cfg.disabled_for(role)
    ]


def run_worker(paths: SandboxPaths, trace, worker: str, task: str) -> ToolResult:
    if worker not in WORKERS:
        raise ToolError(f"unknown worker {worker!r}; choose one of {sorted(WORKERS)}")
    if not task or len(task) > 2000:
        raise ToolError("task must be a non-empty instruction under 2000 characters")
    cfg = AgentConfig.load(trace.dir)
    n = sum(1 for e in trace.events() if e.get("type") == "llm_call" and e.get("role") == "worker") + 1
    agent = f"worker{n}-{worker}"
    try:
        r = ClaudeCLI().run(
            task,
            model=cfg.worker_model,
            system_prompt=worker_prompt(worker),
            json_schema=WORKER_SCHEMA,
            effort=cfg.worker_effort,
            mcp_config=write_mcp_config(paths, trace.dir, "worker", agent),
            allowed_tools=allowed_tools("worker", cfg),
            max_turns=cfg.worker_max_turns,
            timeout_s=cfg.worker_timeout_s,
        )
    except (ClaudeCLIError, TimeoutError) as e:
        trace.append({"type": "worker_error", "agent": agent, "task": task, "error": str(e)[:1000]})
        return ToolResult(False, f"worker {agent} failed: {str(e)[:300]}")
    record_llm_call(trace, agent, "worker", r, task=task)
    out = r.structured or {"answer": r.text, "findings": [], "confidence": 0.0}
    seen = trace.evidence()
    dropped = []
    for f in out.get("findings", []):
        ok = [e for e in f.get("evidence_ids", []) if e in seen]
        dropped += [e for e in f.get("evidence_ids", []) if e not in seen]
        f["evidence_ids"] = ok
    if dropped:
        out["dropped_unverified_ids"] = dropped
    eid = f"worker:{n}"
    text = json.dumps({"worker": worker, "task": task, **out})
    summary = f"{agent}: {out.get('answer', '')[:600]}"
    return ToolResult(True, summary, out, [Evidence(eid, "worker", text)])
