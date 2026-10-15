"""Agent configuration, shared by the runner and the tool-server processes through the trace directory."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# What the master can call itself when it has workers. Everything else (landing files, SQL, charts, model
# diffs, runbooks) is read by workers, so the expensive model spends its tokens on judgement, not reading.
MASTER_TRIAGE_TOOLS = {"get_job_status", "search_logs", "run_data_quality_checks", "list_changed_models",
                       "get_ingest_config", "delegate", "propose_fix"}  # fmt: skip


@dataclass
class AgentConfig:
    name: str = "opus+sonnet"
    master_model: str = "opus"
    worker_model: str = "sonnet"
    use_workers: bool = True
    allow_images: bool = True
    master_max_turns: int = 40
    worker_max_turns: int = 12
    master_effort: str | None = None
    worker_effort: str | None = None
    master_timeout_s: float = 1500
    worker_timeout_s: float = 420
    disabled_tools: list[str] = field(default_factory=list)
    master_tools: str = "triage"  # triage (needs workers) | all

    def disabled(self) -> set[str]:
        off = set(self.disabled_tools)
        if not self.use_workers:
            off.add("delegate")
        if not self.allow_images:
            off.add("render_chart")
        return off

    def disabled_for(self, role: str) -> set[str]:
        off = self.disabled()
        if role == "master" and self.master_tools == "triage":
            from ..tools.registry import TOOLS

            off |= set(TOOLS) - MASTER_TRIAGE_TOOLS
        return off

    def save(self, trace_dir: Path) -> None:
        (trace_dir / "agent_config.json").write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, trace_dir: Path) -> AgentConfig:
        f = trace_dir / "agent_config.json"
        return cls(**json.loads(f.read_text(encoding="utf-8"))) if f.exists() else cls()


PRESETS = {
    "opus+sonnet": AgentConfig(),
    "sonnet-only": AgentConfig(name="sonnet-only", master_model="sonnet"),
    "opus-solo": AgentConfig(name="opus-solo", use_workers=False, master_tools="all"),
    "opus+sonnet-noimg": AgentConfig(name="opus+sonnet-noimg", allow_images=False),
}
