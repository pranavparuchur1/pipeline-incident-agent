# pipeline-incident-agent

An AI incident engineer for data pipelines. It investigates a failed pipeline in a simulated
warehouse (Dagster jobs, dbt models, DuckDB tables, logs, data-quality checks), writes a
structured diagnosis, proposes a narrowly scoped fix, and only gets credit when the sandbox
reruns the pipeline and confirms the fix worked.

- **Master/worker models.** Opus reasons and decides; Sonnet workers gather and condense evidence.
- **Multimodal evidence.** Workers read rendered charts (row-count trends, run timelines) as well as logs and SQL.
- **Runs on a Claude subscription** through the Claude Code CLI in headless mode, not the API.

Status: Phase 0 of 7 (scaffold + smoke). See [docs/PLAN.md](docs/PLAN.md) and
[docs/results/phase0.md](docs/results/phase0.md).

## Quickstart

```bash
uv sync
claude auth login            # once; uses your Claude subscription
uv run python scripts/smoke.py
uv run pytest
```
