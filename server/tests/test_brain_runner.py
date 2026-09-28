"""BrainRunner: the headless `claude -p` invocation.

Never runs the real `claude` binary - every test here goes through either the
fake binary at tests/fixtures/fake_claude.py (a real subprocess, so the
end-to-end plumbing - launching, timeout, exit code, stdout parsing - is
genuinely exercised) or a monkeypatched `asyncio.create_subprocess_exec` (for
pinning down exactly which flags get constructed, without caring what a real
process does with them).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kestrel.brain.runner import (
    DISALLOWED_BUILTIN_TOOLS,
    BrainConfig,
    BrainError,
    BrainRunner,
    MCPServerSpec,
)

FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def make_runner(tmp_path: Path, **config_kwargs) -> BrainRunner:
    config = BrainConfig(
        binary=sys.executable,
        base_args=(str(FAKE_CLAUDE),),
        work_dir=tmp_path / "brain-work",
        timeout_seconds=config_kwargs.pop("timeout_seconds", 5.0),
        **config_kwargs,
    )
    return BrainRunner(config)


# --- end-to-end against the fake binary -------------------------------------


async def test_a_successful_run_returns_the_parsed_result(tmp_path, monkeypatch):
    response = tmp_path / "response.json"
    response.write_text(json.dumps({"result": "hello from the brain"}))
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_RESPONSE", str(response))

    runner = make_runner(tmp_path)
    result = await runner.run(
        system_prompt="you are kestrel", user_message="what's running?", model="haiku"
    )
    assert result.text == "hello from the brain"
    assert result.model == "haiku"


async def test_a_bare_json_string_result_is_accepted(tmp_path, monkeypatch):
    response = tmp_path / "response.json"
    response.write_text(json.dumps("just a string"))
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_RESPONSE", str(response))

    runner = make_runner(tmp_path)
    result = await runner.run(system_prompt="s", user_message="q", model="haiku")
    assert result.text == "just a string"


async def test_a_nonzero_exit_raises_brainerror(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_MODE", "fail")
    runner = make_runner(tmp_path)
    with pytest.raises(BrainError, match="exited 1"):
        await runner.run(system_prompt="s", user_message="q", model="haiku")


async def test_unparseable_output_raises_brainerror(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_MODE", "garbage")
    runner = make_runner(tmp_path)
    with pytest.raises(BrainError, match="could not parse"):
        await runner.run(system_prompt="s", user_message="q", model="haiku")


async def test_a_run_that_outruns_its_timeout_raises_brainerror(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_MODE", "timeout")
    runner = make_runner(tmp_path, timeout_seconds=0.2)
    with pytest.raises(BrainError, match="timed out"):
        await runner.run(system_prompt="s", user_message="q", model="haiku")


async def test_a_missing_binary_raises_brainerror(tmp_path):
    config = BrainConfig(binary="definitely-not-a-real-binary", work_dir=tmp_path)
    runner = BrainRunner(config)
    with pytest.raises(BrainError, match="not found"):
        await runner.run(system_prompt="s", user_message="q", model="haiku")


async def test_no_work_dir_anywhere_is_a_programming_error(tmp_path):
    runner = BrainRunner(BrainConfig(binary=sys.executable))
    with pytest.raises(ValueError, match="work_dir"):
        await runner.run(system_prompt="s", user_message="q", model="haiku")


# --- argument construction ---------------------------------------------------


async def test_argv_carries_the_model_query_and_system_prompt(tmp_path, monkeypatch):
    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_ARGV_LOG", str(log_path))
    runner = make_runner(tmp_path)

    await runner.run(system_prompt="be terse", user_message="ping", model="sonnet")

    argv = json.loads(log_path.read_text().splitlines()[-1])
    assert "--print" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "be terse"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[-1] == "ping"  # the query is the final positional argument


async def test_disallowed_builtins_are_always_present(tmp_path, monkeypatch):
    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_ARGV_LOG", str(log_path))
    runner = make_runner(tmp_path)

    await runner.run(system_prompt="s", user_message="q", model="haiku")

    argv = json.loads(log_path.read_text().splitlines()[-1])
    i = argv.index("--disallowedTools")
    disallowed = set()
    for tool in argv[i + 1 :]:
        if tool.startswith("--"):
            break
        disallowed.add(tool)
    assert set(DISALLOWED_BUILTIN_TOOLS) <= disallowed
    for builtin in ("Bash", "Edit", "Write", "Read"):
        assert builtin in disallowed


async def test_allowed_tools_become_the_tools_flag(tmp_path, monkeypatch):
    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_ARGV_LOG", str(log_path))
    runner = make_runner(tmp_path)

    await runner.run(
        system_prompt="s", user_message="q", model="haiku", allowed_tools=["mcp__kestrel__*"]
    )

    argv = json.loads(log_path.read_text().splitlines()[-1])
    assert argv[argv.index("--tools") + 1] == "mcp__kestrel__*"


async def test_tools_flag_is_omitted_when_nothing_is_allowed(tmp_path, monkeypatch):
    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_ARGV_LOG", str(log_path))
    runner = make_runner(tmp_path)

    await runner.run(system_prompt="s", user_message="q", model="haiku")

    argv = json.loads(log_path.read_text().splitlines()[-1])
    assert "--tools" not in argv


async def test_strict_mcp_config_is_always_passed_even_with_no_servers(tmp_path, monkeypatch):
    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_ARGV_LOG", str(log_path))
    runner = make_runner(tmp_path)

    await runner.run(system_prompt="s", user_message="q", model="haiku")

    argv = json.loads(log_path.read_text().splitlines()[-1])
    assert "--strict-mcp-config" in argv
    mcp_config_path = Path(argv[argv.index("--mcp-config") + 1])
    assert json.loads(mcp_config_path.read_text()) == {"mcpServers": {}}


async def test_mcp_servers_are_written_into_the_config_file(tmp_path, monkeypatch):
    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_ARGV_LOG", str(log_path))
    runner = make_runner(tmp_path)

    spec = MCPServerSpec(name="kestrel", command="python", args=("-m", "kestrel.brain.mcp_server"))
    await runner.run(
        system_prompt="s",
        user_message="q",
        model="haiku",
        allowed_tools=["mcp__kestrel__*"],
        mcp_servers=[spec],
    )

    argv = json.loads(log_path.read_text().splitlines()[-1])
    mcp_config_path = Path(argv[argv.index("--mcp-config") + 1])
    written = json.loads(mcp_config_path.read_text())
    assert written == {
        "mcpServers": {
            "kestrel": {
                "type": "stdio",
                "command": "python",
                "args": ["-m", "kestrel.brain.mcp_server"],
            }
        }
    }


async def test_a_custom_cwd_overrides_the_configured_work_dir(tmp_path, monkeypatch):
    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_ARGV_LOG", str(log_path))
    runner = make_runner(tmp_path)
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()

    await runner.run(system_prompt="s", user_message="q", model="sonnet", cwd=other_dir)

    mcp_dir = other_dir / ".kestrel-mcp"
    assert mcp_dir.is_dir()
