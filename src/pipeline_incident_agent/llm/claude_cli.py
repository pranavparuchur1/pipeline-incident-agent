"""Call Claude through the local Claude Code CLI in headless mode (`claude -p`).

Why the CLI and not the Anthropic SDK: this project runs on the owner's Claude
subscription, not on API billing. The CLI is signed in with `claude auth login`;
we strip ANTHROPIC_API_KEY from the child environment so a stray key can never
switch it to API billing.

Every call is stateless: built-in tools are disabled, a short custom system
prompt replaces Claude Code's default one, and no session is persisted. Input
and output both use stream-json so text and images go through one code path.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Model tiers. The master does open-ended reasoning (root cause, remediation
# decisions); workers do narrow, repeated jobs (summarize logs, diff schemas).
MASTER = "opus"
WORKER = "sonnet"

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class ClaudeCLIError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    structured: Any | None
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    api_equiv_usd: float  # what the same tokens would cost on the API; NOT a real charge
    duration_ms: int
    num_turns: int
    raw: dict = field(repr=False, default_factory=dict)
    events: list[dict] = field(repr=False, default_factory=list)  # full stream-json transcript

    def tool_uses(self) -> list[dict]:
        """Every tool call the model made during this session, in order."""
        out = []
        for ev in self.events:
            if ev.get("type") != "assistant":
                continue
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    out.append({"name": block.get("name"), "input": block.get("input")})
        return out


def find_claude_binary() -> str:
    """Resolve the native claude.exe, skipping the npm .cmd shim (cmd.exe mangles long arguments)."""
    if env := os.environ.get("CLAUDE_BIN"):
        return env
    shim = shutil.which("claude")
    if shim:
        native = Path(shim).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.exists():
            return str(native)
        return shim
    raise ClaudeCLIError("claude CLI not found; install @anthropic-ai/claude-code and run `claude auth login`")


def _image_block(path: Path) -> dict:
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise ValueError(f"unsupported image type: {path}")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def build_user_message(prompt: str, images: list[Path] | None = None) -> dict:
    content: list[dict] = [_image_block(Path(p)) for p in images or []]
    content.append({"type": "text", "text": prompt})
    return {"type": "user", "message": {"role": "user", "content": content}}


def parse_result_event(stdout: str) -> dict:
    """Return the final `result` event from a stream-json transcript."""
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        event = json.loads(line)
        if event.get("type") == "result":
            result = event
    if result is None:
        raise ClaudeCLIError(f"no result event in CLI output: {stdout[-500:]!r}")
    return result


def to_llm_result(event: dict, requested_model: str) -> LLMResult:
    if event.get("is_error") or event.get("subtype") not in (None, "success"):
        raise ClaudeCLIError(f"CLI returned an error: {event.get('result') or event.get('subtype')}")
    usage = event.get("usage") or {}
    models = list((event.get("modelUsage") or {}).keys())
    return LLMResult(
        text=event.get("result") or "",
        structured=event.get("structured_output"),
        model=models[0] if models else requested_model,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
        api_equiv_usd=float(event.get("total_cost_usd") or 0.0),
        duration_ms=int(event.get("duration_ms") or 0),
        num_turns=int(event.get("num_turns") or 0),
        raw=event,
    )


class ClaudeCLI:
    def __init__(self, binary: str | None = None, workdir: Path | None = None, timeout_s: float = 300):
        self.binary = binary or find_claude_binary()
        # Run from an empty directory so no CLAUDE.md or project settings leak into the prompt.
        self.workdir = workdir or Path(os.environ.get("PIA_LLM_WORKDIR", Path.home() / ".pia-llm"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s

    def command(
        self,
        model: str,
        system_prompt: str,
        json_schema: dict | None,
        effort: str | None,
        mcp_config: Path | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
    ) -> list[str]:
        cmd = [
            self.binary, "-p",
            "--model", model,
            "--system-prompt", system_prompt,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]  # fmt: skip
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema)]
        if effort:
            cmd += ["--effort", effort]
        if mcp_config is not None:
            # --tools "" disables Claude Code's built-in tools; MCP tools still need explicit permission
            # in headless mode, so exactly the role's tools are allow-listed and nothing else.
            cmd += ["--mcp-config", str(mcp_config)]
            cmd += ["--allowedTools", ",".join(allowed_tools or [])]
        if max_turns:
            cmd += ["--max-turns", str(max_turns)]
        return cmd

    def run(
        self,
        prompt: str,
        *,
        model: str = WORKER,
        system_prompt: str = "You are a precise assistant.",
        images: list[Path] | None = None,
        json_schema: dict | None = None,
        effort: str | None = None,
        mcp_config: Path | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        stdin = json.dumps(build_user_message(prompt, images)) + "\n"
        started = time.monotonic()
        proc = subprocess.run(
            self.command(model, system_prompt, json_schema, effort, mcp_config, allowed_tools, max_turns),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=self.workdir,
            timeout=timeout_s or self.timeout_s,
        )
        if proc.returncode != 0 and '"type":"result"' not in proc.stdout:
            raise ClaudeCLIError(f"claude exited {proc.returncode}: {proc.stderr[-800:] or proc.stdout[-800:]}")
        result = to_llm_result(parse_result_event(proc.stdout), model)
        result.events = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
        if not result.duration_ms:
            result.duration_ms = int((time.monotonic() - started) * 1000)
        return result
