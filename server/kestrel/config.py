from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from kestrel_agent.host import default_socket_path

DEFAULT_SERVER_URL = "http://127.0.0.1:8099"


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

    # Claude Code executor: which repos it may open a worktree in, and how it
    # invokes the CLI. Empty by default - a Config built by hand (as most
    # tests do) opts out of the executor entirely rather than needing to know
    # about it, which is what keeps Runtime.build backward compatible.
    claude_projects: dict[str, Path] = field(default_factory=dict)
    claude_worktrees_root: Path | None = None
    claude_binary: str = "claude"
    claude_base_args: tuple[str, ...] = ("--dangerously-skip-permissions",)
    server_url: str = DEFAULT_SERVER_URL

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token"

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
        return cls(
            data_dir=root,
            db_path=root / "kestrel.db",
            memory_repo=root / "memory",
            identity_dir=Path(__file__).parent / "identity",
            claude_projects=_parse_projects(os.environ.get("KESTREL_CLAUDE_PROJECTS")),
            claude_worktrees_root=Path(worktrees_root).expanduser() if worktrees_root else None,
            claude_binary=os.environ.get("KESTREL_CLAUDE_BINARY", "claude"),
            server_url=os.environ.get("KESTREL_SERVER_URL", DEFAULT_SERVER_URL),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_repo.mkdir(parents=True, exist_ok=True)
