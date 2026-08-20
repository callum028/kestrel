from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    memory_repo: Path
    identity_dir: Path

    @classmethod
    def from_env(cls) -> Config:
        root = Path(os.environ.get("KESTREL_DATA", str(Path.home() / ".kestrel")))
        return cls(
            data_dir=root,
            db_path=root / "kestrel.db",
            memory_repo=root / "memory",
            identity_dir=Path(__file__).parent / "identity",
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_repo.mkdir(parents=True, exist_ok=True)
