"""The master: one Opus session per attempt, with the master tool server (investigate, delegate, propose)."""

from __future__ import annotations

import os

from ..llm.claude_cli import ClaudeCLI, LLMResult
from ..sandbox.config import SandboxPaths
from ..tools.registry import Trace
from .config import AgentConfig
from .prompts import master_prompt
from .schema import DIAGNOSIS_SCHEMA
from .workers import allowed_tools, record_llm_call, write_mcp_config


def run_master(paths: SandboxPaths, trace: Trace, cfg: AgentConfig, prompt: str, attempt: int) -> LLMResult:
    # Delegation runs a whole worker session inside one tool call, so tool calls may take minutes.
    os.environ.setdefault("MCP_TOOL_TIMEOUT", str(int(cfg.worker_timeout_s * 1000) + 60_000))
    os.environ.setdefault("MCP_TIMEOUT", "60000")
    agent = f"master-attempt{attempt}"
    r = ClaudeCLI().run(
        prompt,
        model=cfg.master_model,
        system_prompt=master_prompt(cfg.use_workers),
        json_schema=DIAGNOSIS_SCHEMA,
        effort=cfg.master_effort,
        mcp_config=write_mcp_config(paths, trace.dir, "master", agent),
        allowed_tools=allowed_tools("master", cfg),
        max_turns=cfg.master_max_turns,
        timeout_s=cfg.master_timeout_s,
    )
    record_llm_call(trace, agent, "master", r)
    return r
