"""`Config.from_env` - the environment-variable surface deploy/kestrel.env.example
documents. Narrow, targeted at the pieces this change touches: the origin
allowlist, where the built web app is looked for, and the brain model/args
wiring that used to sit dead in the dataclass (never read from the
environment despite `runtime.py` already threading the fields through)."""

import pytest

from kestrel.auth import DEFAULT_ALLOWED_ORIGINS
from kestrel.brain.routing import ModelNames, Turn, decide_model
from kestrel.config import Config

ENV_KEYS = (
    "KESTREL_ALLOWED_ORIGINS",
    "KESTREL_WEB_DIST",
    "KESTREL_BRAIN_HAIKU_MODEL",
    "KESTREL_BRAIN_SONNET_MODEL",
    "KESTREL_BRAIN_BASE_ARGS",
    "KESTREL_BRAIN_MCP_ARGS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_default_allowed_origins_is_just_the_dev_tauri_set():
    assert Config.from_env().allowed_origins == DEFAULT_ALLOWED_ORIGINS


def test_extra_allowed_origins_from_env_are_additive(monkeypatch):
    monkeypatch.setenv("KESTREL_ALLOWED_ORIGINS", "https://kestrel-pi.tailnet.ts.net")
    origins = Config.from_env().allowed_origins
    assert "https://kestrel-pi.tailnet.ts.net" in origins
    assert DEFAULT_ALLOWED_ORIGINS <= origins


def test_default_web_dist_dir_points_at_the_repo_checkout():
    import kestrel.config as config_module

    repo_root = __import__("pathlib").Path(config_module.__file__).resolve().parents[2]
    assert Config.from_env().web_dist_dir == repo_root / "web" / "dist"


def test_web_dist_dir_is_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("KESTREL_WEB_DIST", str(tmp_path / "custom-dist"))
    assert Config.from_env().web_dist_dir == tmp_path / "custom-dist"


def test_brain_model_names_default_to_the_cli_aliases():
    config = Config.from_env()
    assert config.brain_haiku_model == "haiku"
    assert config.brain_sonnet_model == "sonnet"


def test_brain_model_names_are_configurable(monkeypatch):
    monkeypatch.setenv("KESTREL_BRAIN_HAIKU_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("KESTREL_BRAIN_SONNET_MODEL", "claude-sonnet-4-5")
    config = Config.from_env()
    assert config.brain_haiku_model == "claude-haiku-4-5"
    assert config.brain_sonnet_model == "claude-sonnet-4-5"


def test_configured_model_names_are_what_routing_actually_returns():
    """The point of wiring `brain_haiku_model`/`brain_sonnet_model` through:
    `decide_model` must hand back the configured strings, not the literal
    `"haiku"`/`"sonnet"` it used to hardcode."""
    models = ModelNames(haiku="claude-haiku-4-5", sonnet="claude-sonnet-4-5")
    assert decide_model(Turn(text="hi"), models) == "claude-haiku-4-5"
    assert decide_model(Turn(text="why does this happen"), models) == "claude-sonnet-4-5"
    assert decide_model(Turn(text="x", summarising_task_report=True), models) == (
        "claude-sonnet-4-5"
    )


def test_brain_base_args_are_parsed_shell_style(monkeypatch):
    monkeypatch.setenv("KESTREL_BRAIN_BASE_ARGS", "--flag-one --flag-two value")
    config = Config.from_env()
    assert config.brain_claude_base_args == ("--flag-one", "--flag-two", "value")


def test_brain_mcp_args_are_parsed_shell_style(monkeypatch):
    monkeypatch.setenv("KESTREL_BRAIN_MCP_ARGS", "-m kestrel.brain.mcp_server")
    config = Config.from_env()
    assert config.brain_mcp_server_args == ("-m", "kestrel.brain.mcp_server")


def test_brain_args_default_to_empty():
    config = Config.from_env()
    assert config.brain_claude_base_args == ()
    assert config.brain_mcp_server_args == ()
