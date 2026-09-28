from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from kestrel_agent.host import default_socket_path


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
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_repo.mkdir(parents=True, exist_ok=True)
