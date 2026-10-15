"""MCP server exposing the investigation tools to a headless Claude session.

One server process per agent session, started by the Claude CLI from an --mcp-config file:

    python -m pipeline_incident_agent.tools.mcp_server --role worker --sandbox <dir> --trace <dir>

The role decides which tools are even listed: a worker's server does not advertise propose_fix,
and call_tool re-checks the role, so a worker cannot remediate even by guessing the tool name.
Charts come back as MCP image content, so the model sees the picture itself.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from ..sandbox.config import SandboxPaths
from .registry import ROLES, TOOLS, PermissionDenied, Trace, call_tool

SERVER_NAME = "pia"


def tool_definitions(role: str, disabled: set[str] = frozenset()) -> list[types.Tool]:
    out = []
    for name in ROLES[role]:
        if name in disabled:
            continue
        spec = TOOLS[name]
        schema = {"type": "object", "properties": spec.params, "required": list(spec.required)}
        out.append(types.Tool(name=name, description=spec.description, input_schema=schema))
    return out


def build_server(paths: SandboxPaths, trace: Trace, role: str, agent: str) -> Server:
    from ..agent.config import AgentConfig

    disabled = AgentConfig.load(trace.dir).disabled()

    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tool_definitions(role, disabled))

    async def call(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            result = await asyncio.to_thread(
                call_tool, paths, trace, role, params.name, dict(params.arguments or {}), agent=agent
            )
        except PermissionDenied as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"permission denied: {e}")],
                                        is_error=True)  # fmt: skip
        content: list = [types.TextContent(type="text", text=result.to_json())]
        for png in result.images:
            data = base64.b64encode(Path(png).read_bytes()).decode("ascii")
            content.append(types.ImageContent(type="image", data=data, mime_type="image/png"))
        return types.CallToolResult(content=content, is_error=not result.ok)

    return Server(SERVER_NAME, on_list_tools=list_tools, on_call_tool=call)


async def _serve(server: Server) -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def mcp_config(role: str, sandbox: Path, trace_dir: Path, agent: str) -> dict:
    """The --mcp-config JSON the Claude CLI uses to start this server."""
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": sys.executable,
                "args": [
                    "-m",
                    "pipeline_incident_agent.tools.mcp_server",
                    "--role",
                    role,
                    "--sandbox",
                    str(sandbox),
                    "--trace",
                    str(trace_dir),
                    "--agent",
                    agent,
                ],  # fmt: skip
            }
        }
    }


def allowed_tool_names(role: str) -> list[str]:
    """Tool names as the Claude CLI sees them (mcp__<server>__<tool>), for --allowedTools."""
    return [f"mcp__{SERVER_NAME}__{n}" for n in ROLES[role]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=sorted(ROLES), required=True)
    ap.add_argument("--sandbox", required=True)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--agent", default="agent")
    a = ap.parse_args()
    server = build_server(SandboxPaths(Path(a.sandbox)), Trace(Path(a.trace)), a.role, a.agent)
    asyncio.run(_serve(server))


if __name__ == "__main__":
    main()

__all__ = ["mcp_config", "allowed_tool_names", "tool_definitions"]
