import sys
from pathlib import Path

from kestrel.brain.ask_repo import READ_ONLY_TOOLS, ask_repo
from kestrel.brain.runner import BrainConfig, BrainRunner

FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"


async def test_ask_repo_runs_sonnet_with_read_only_tools_against_the_repo(tmp_path, monkeypatch):
    import json

    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_ARGV_LOG", str(log_path))
    response = tmp_path / "response.json"
    response.write_text(json.dumps({"result": "foo() reads the config file"}))
    monkeypatch.setenv("KESTREL_FAKE_CLAUDE_RESPONSE", str(response))

    repo = tmp_path / "repo"
    repo.mkdir()
    runner = BrainRunner(BrainConfig(binary=sys.executable, base_args=(str(FAKE_CLAUDE),)))

    answer = await ask_repo("what does foo() do?", runner=runner, repo_path=repo)

    assert answer == "foo() reads the config file"
    argv = json.loads(log_path.read_text().splitlines()[-1])
    assert argv[argv.index("--model") + 1] == "sonnet"
    i = argv.index("--tools")
    tools = []
    for tool in argv[i + 1 :]:
        if tool.startswith("--"):
            break
        tools.append(tool)
    assert set(tools) == set(READ_ONLY_TOOLS)
    assert argv[-1] == "what does foo() do?"
