from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from kestrel_agent.host import default_socket_path

from .board import NOTION_API_VERSION
from .validation import DeployCheck, ProjectValidation, ValidationCommand


@dataclass(frozen=True)
class BoardConfig:
    """Everything about how Notion is wired up. Property names are Callum's
    naming choices on his board, not something to hardcode; the token and
    database id are secrets, so they come from the environment only."""

    token: str | None = None
    database_id: str | None = None
    api_version: str = NOTION_API_VERSION
    poll_interval_seconds: float = 60.0
    property_title: str = "Name"
    property_body: str = "Description"
    property_status: str = "Status"
    property_flag: str = "Kestrel flag"
    property_pr: str = "PR"
    property_handle: str = "ID"
    property_project: str = "Project"

    @property
    def configured(self) -> bool:
        return bool(self.token and self.database_id)

    @classmethod
    def from_env(cls) -> BoardConfig:
        def _get(name: str, default: str) -> str:
            return os.environ.get(f"KESTREL_NOTION_{name}", default)

        return cls(
            token=os.environ.get("KESTREL_NOTION_TOKEN") or None,
            database_id=os.environ.get("KESTREL_NOTION_DATABASE_ID") or None,
            api_version=_get("API_VERSION", NOTION_API_VERSION),
            poll_interval_seconds=float(_get("POLL_SECONDS", "60")),
            property_title=_get("PROPERTY_TITLE", "Name"),
            property_body=_get("PROPERTY_BODY", "Description"),
            property_status=_get("PROPERTY_STATUS", "Status"),
            property_flag=_get("PROPERTY_FLAG", "Kestrel flag"),
            property_pr=_get("PROPERTY_PR", "PR"),
            property_handle=_get("PROPERTY_HANDLE", "ID"),
            property_project=_get("PROPERTY_PROJECT", "Project"),
        )


DEFAULT_SERVER_URL = "http://127.0.0.1:8099"


def _validation_for_project(name: str) -> ProjectValidation | None:
    """Per-project validation config, entirely from the environment -
    `KESTREL_VALIDATION_<PROJECT>_*`, `<PROJECT>` being the same name used in
    `KESTREL_CLAUDE_PROJECTS` upper-cased. `None` when nothing at all is set
    for this project, which is exactly the "no validation configured" case
    (design §5 step 7d) - a visible pass-through, not silence, is produced
    downstream in the orchestrator, not here.
    """
    prefix = f"KESTREL_VALIDATION_{name.upper()}_"

    def _get(suffix: str) -> str | None:
        return os.environ.get(prefix + suffix) or None

    def _minutes(suffix: str, default: float) -> timedelta:
        raw = _get(suffix)
        return timedelta(minutes=float(raw)) if raw else timedelta(minutes=default)

    workflow = _get("DEPLOY_WORKFLOW")
    deploy_url = _get("DEPLOY_URL")
    command = _get("COMMAND")
    report_path = _get("REPORT_PATH")

    if not (workflow or deploy_url or command):
        return None

    return ProjectValidation(
        deploy=DeployCheck(
            workflow=workflow,
            url=deploy_url,
            url_match=_get("DEPLOY_URL_MATCH"),
            timeout=_minutes("DEPLOY_TIMEOUT_MINUTES", 15.0),
        ),
        command=(
            ValidationCommand(
                command=command,
                report_path=report_path or "report.json",
                timeout=_minutes("TIMEOUT_MINUTES", 20.0),
            )
            if command
            else None
        ),
        dev_url=_get("DEV_URL"),
    )


def _build_validation(project_names: list[str]) -> dict[str, ProjectValidation]:
    validation: dict[str, ProjectValidation] = {}
    for name in project_names:
        config = _validation_for_project(name)
        if config is not None:
            validation[name] = config
    return validation


def _parse_projects(raw: str | None) -> dict[str, Path]:
    """`name=path,name2=path2` - deliberately not JSON, so it is one
    comfortable line in a systemd unit or a `.env` file rather than a quoted
    blob."""
    if not raw:
        return {}
    projects: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, path = entry.partition("=")
        if not path:
            raise ValueError(f"KESTREL_CLAUDE_PROJECTS entry missing '=path': {entry!r}")
        projects[name.strip()] = Path(path.strip()).expanduser()
    return projects


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    memory_repo: Path
    identity_dir: Path
    # Mail (Microsoft Graph). Both come from the app registration - see the
    # "App registration Callum needs to create" note in graph_mail.py. Either
    # left unset (the common case until that registration exists) means
    # Runtime falls back to the fake reader rather than failing to start.
    mail_tenant_id: str | None = None
    mail_client_id: str | None = None
    board: BoardConfig = field(default_factory=BoardConfig)

    # Claude Code executor: which repos it may open a worktree in, and how it
    # invokes the CLI. Empty by default - a Config built by hand (as most
    # tests do) opts out of the executor entirely rather than needing to know
    # about it, which is what keeps Runtime.build backward compatible.
    claude_projects: dict[str, Path] = field(default_factory=dict)
    claude_worktrees_root: Path | None = None
    claude_binary: str = "claude"
    claude_base_args: tuple[str, ...] = ("--dangerously-skip-permissions",)
    server_url: str = DEFAULT_SERVER_URL

    # GitHub (github.py): PR discovery, CI status for waits, merge-on-green.
    # Token from the environment only (never config), same as the Notion
    # token - `GH_TOKEN` is what `gh auth token` and most CI runners already
    # populate, so that is checked first if `GITHUB_TOKEN` is unset.
    github_token: str | None = None
    github_repo: str | None = None  # "owner/name"

    # Validation (validation.py): per-project deploy confirmation and the
    # authoritative test command, keyed by the same project name as
    # `claude_projects`. Empty by default, same reasoning as `claude_projects`
    # - a project absent here gets the explicit "no validation configured"
    # pass-through rather than an error.
    validation: dict[str, ProjectValidation] = field(default_factory=dict)

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token"

    @property
    def vapid_key_path(self) -> Path:
        # Mirrors token_path: one secret per install, 0600, loaded-or-created
        # by the module that owns it (kestrel.push) rather than here.
        return self.data_dir / "vapid_private_key.pem"

    @property
    def mail_token_path(self) -> Path:
        return self.data_dir / "mail_token"

    @property
    def session_host_socket(self) -> Path:
        # The server never spawns this - it is a client of whatever session
        # host is already running, sharing only the data directory with it.
        return default_socket_path(self.data_dir)

    @property
    def worktrees_root(self) -> Path:
        return self.claude_worktrees_root or self.data_dir / "worktrees"

    @classmethod
    def from_env(cls) -> Config:
        root = Path(os.environ.get("KESTREL_DATA", str(Path.home() / ".kestrel")))
        worktrees_root = os.environ.get("KESTREL_WORKTREES_ROOT")
        claude_projects = _parse_projects(os.environ.get("KESTREL_CLAUDE_PROJECTS"))
        return cls(
            data_dir=root,
            db_path=root / "kestrel.db",
            memory_repo=root / "memory",
            identity_dir=Path(__file__).parent / "identity",
            mail_tenant_id=os.environ.get("KESTREL_MAIL_TENANT_ID"),
            mail_client_id=os.environ.get("KESTREL_MAIL_CLIENT_ID"),
            board=BoardConfig.from_env(),
            claude_projects=claude_projects,
            claude_worktrees_root=Path(worktrees_root).expanduser() if worktrees_root else None,
            claude_binary=os.environ.get("KESTREL_CLAUDE_BINARY", "claude"),
            server_url=os.environ.get("KESTREL_SERVER_URL", DEFAULT_SERVER_URL),
            github_token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
            github_repo=os.environ.get("KESTREL_GITHUB_REPO"),
            validation=_build_validation(list(claude_projects)),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_repo.mkdir(parents=True, exist_ok=True)
