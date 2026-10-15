import base64
import json

import pytest

from pipeline_incident_agent.llm.claude_cli import (
    ClaudeCLI,
    ClaudeCLIError,
    build_user_message,
    parse_result_event,
    to_llm_result,
)

RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "ok",
    "structured_output": {"a": 1},
    "usage": {"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 11},
    "modelUsage": {"claude-sonnet-5-5": {}},
    "total_cost_usd": 0.01,
    "duration_ms": 1234,
    "num_turns": 1,
}


def test_build_user_message_puts_images_before_text(tmp_path):
    img = tmp_path / "c.png"
    img.write_bytes(b"\x89PNG fake")
    msg = build_user_message("look", [img])
    blocks = msg["message"]["content"]
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(blocks[0]["source"]["data"]) == b"\x89PNG fake"


def test_build_user_message_rejects_unknown_image_type(tmp_path):
    f = tmp_path / "x.bmp"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        build_user_message("look", [f])


def test_parse_result_event_takes_last_result_and_skips_noise():
    stdout = "\n".join(["not json", json.dumps({"type": "system"}), json.dumps(RESULT)])
    assert parse_result_event(stdout)["result"] == "ok"


def test_parse_result_event_without_result_raises():
    with pytest.raises(ClaudeCLIError):
        parse_result_event(json.dumps({"type": "assistant"}))


def test_to_llm_result_maps_usage():
    r = to_llm_result(RESULT, "sonnet")
    assert (r.text, r.structured, r.model) == ("ok", {"a": 1}, "claude-sonnet-5-5")
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == (3, 5, 7, 11)
    assert r.api_equiv_usd == 0.01


def test_to_llm_result_raises_on_error_event():
    with pytest.raises(ClaudeCLIError):
        to_llm_result({**RESULT, "is_error": True, "result": "Failed to authenticate"}, "opus")


def test_command_disables_tools_and_settings(tmp_path):
    cli = ClaudeCLI(binary="claude", workdir=tmp_path)
    cmd = cli.command("opus", "sys", {"type": "object"}, "low")
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}
    assert cmd[cmd.index("--effort") + 1] == "low"


def test_api_key_is_stripped_from_child_env(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])

        class P:
            returncode = 0
            stdout = json.dumps(RESULT)
            stderr = ""

        return P()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr("subprocess.run", fake_run)
    ClaudeCLI(binary="claude", workdir=tmp_path).run("hi")
    assert "ANTHROPIC_API_KEY" not in seen
