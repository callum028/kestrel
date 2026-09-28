from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from kestrel_agent.host import default_socket_path

from .board import NOTION_API_VERSION


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

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token"

    @property
    def mail_token_path(self) -> Path:
        return self.data_dir / "mail_token"

    @property
    def session_host_socket(self) -> Path:
        # The server never spawns this - it is a client of whatever session
        # host is already running, sharing only the data directory with it.
        return default_socket_path(self.data_dir)

    @classmethod
    def from_env(cls) -> Config:
        root = Path(os.environ.get("KESTREL_DATA", str(Path.home() / ".kestrel")))
        return cls(
            data_dir=root,
            db_path=root / "kestrel.db",
            memory_repo=root / "memory",
            identity_dir=Path(__file__).parent / "identity",
            mail_tenant_id=os.environ.get("KESTREL_MAIL_TENANT_ID"),
            mail_client_id=os.environ.get("KESTREL_MAIL_CLIENT_ID"),
            board=BoardConfig.from_env(),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_repo.mkdir(parents=True, exist_ok=True)
