# Phase 3 results (2026-10-15)

Tools, permissions, runbooks and the MCP bridge to headless Claude sessions.

## What exists

- **11 read-only tools** (`tools/investigate.py`): job status, log search, schema, landing file vs. contract,
  ingest config, data-quality checks, read-only SQL, dbt model + diff vs. deployed, changed models,
  row-count charts (returned as images), runbook search. Every result carries evidence ids
  (`log:243`, `landing:orders@2026-10-01`, `model:fct_orders`, `chart:orders_by_channel`, ...).
- **Roles** (`tools/registry.py`): `worker` = read-only tools; `master` = read-only + `delegate` + `propose_fix`.
  The role decides which tools the MCP server even lists, and every call is re-checked.
- **`propose_fix` changes nothing.** It validates actions against the allow-list and rejects any cited evidence
  id that no tool returned earlier in the same investigation, then records the proposal for the harness.
- **Trace**: every tool call (args, summary, full evidence text, timing) is appended to `trace.jsonl`.
- **Runbooks** (`runbooks/*.md`, 7 files): written by a Sonnet worker that was *not allowed to read the incident
  catalog or results* (to avoid teaching to the test); searched with a small BM25 index, one document per section.
- **MCP server** (`tools/mcp_server.py`, mcp 2.3 low-level `Server`): one process per agent session.

## Checks

| Check | Result |
|---|---|
| Unit tests (tools: S1/F1/R2 sandboxes, SQL guards, internals unreachable, roles, propose_fix rules, runbook search, trace) | 23 passed (99 total in repo) |
| SQL guard: `read_csv('<landing file>')`, `attach`, DDL/DML, multi-statement | refused / no file content returned |
| Official MCP client over stdio, role `worker` | lists 11 tools; `propose_fix` → "permission denied" |
| Live: Sonnet `log_analyst` on S1 | named the failed step and exact error; cited `run:f8d1c1fc`, `log:243` (all real) — 7 s, $0.027 API-equiv. |
| Live: Sonnet `data_profiler` on R2 (multimodal) | read the chart image: "web fell from ~242 on 09-30 to 0 on 10-01; app ~151 and marketplace ~84 normal" — 6 s, $0.016 |
| Live: worker told to call `propose_fix` | refused, tool not available; no proposal recorded |

Script: `uv run python scripts/phase3_live_check.py` (single run; model outputs vary run to run).

## Found along the way

- mcp 2.x removed the decorator-style low-level API (`@server.list_tools()`); handlers are now constructor
  arguments (`on_list_tools`, `on_call_tool`). The first live run failed with the server crashing at startup —
  and both workers *reported* that they had no tools instead of inventing an answer.
- In headless mode MCP tools must be allow-listed explicitly (`--allowedTools mcp__pia__<tool>`), which doubles as a
  second permission layer.
