"""Durable memory - markdown in a git repo, one fact per file.

Split from derived knowledge by where truth lives. Tickets, PRs and code change
without telling you, so they are indexed and read fresh, never copied here. This
store is only for things that exist nowhere else: who Callum is, his conventions,
and decisions whose reasoning was never written down.

Two properties do most of the work:

1. Four write sources, and none of them is inference. Nothing summarises
   conversation into beliefs behind his back - that is the mechanism that turns
   memory into sludge, and it is why "remember everything" would have failed.
2. Every write emits an event, so every durable memory has a visible moment of
   creation. If something was written, he saw it happen.

git gives history, diffs, blame, manual editing and grep for free.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .events import EventKind, EventLog

_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "if",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "is",
    "are",
    "was",
    "were",
    "be",
    "it",
    "that",
    "this",
    "i",
    "you",
    "my",
    "me",
    "we",
    "do",
    "does",
    "how",
    "what",
    "when",
    "should",
    "would",
}


class Source(StrEnum):
    """The only ways a durable memory comes into existence.

    Note what is absent: there is no INFERRED. Adding one would undo the design.
    """

    EXPLICIT = "explicit"  # "remember that..."
    CORRECTION = "correction"  # he said it was wrong
    DECISION = "decision"  # a what-decision made during task work
    PROXY_ANSWER = "proxy_answer"  # answered on his behalf; must stay visible


@dataclass
class MemoryEntry:
    id: str
    fact: str
    category: str
    source: Source
    scope: str = "global"  # global, or project:<name> - leaking between repos is hard to trace
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    task_ref: str | None = None
    core: bool = False  # always injected, subject to the cap in context.py
    superseded_by: str | None = None

    @property
    def live(self) -> bool:
        return self.superseded_by is None


class Retriever(Protocol):
    """Retrieval is automatic, never tool-triggered.

    A model does not know what it does not know, so it will not think to look up
    whether he has a dog before answering about pets. Swap this for embeddings
    later; the contract does not change.
    """

    def search(self, query: str, entries: list[MemoryEntry], k: int) -> list[MemoryEntry]: ...


class KeywordRetriever:
    """Token overlap. Deliberately dependency-free so retrieval works from day one."""

    def __init__(self, min_overlap: int = 1) -> None:
        self._min = min_overlap

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9_]+", text.lower()) if t not in _STOPWORDS}

    def search(self, query: str, entries: list[MemoryEntry], k: int = 5) -> list[MemoryEntry]:
        q = self._tokens(query)
        scored = []
        for e in entries:
            overlap = len(q & self._tokens(f"{e.fact} {e.category}"))
            if overlap >= self._min:
                scored.append((overlap, e))
        scored.sort(key=lambda pair: (-pair[0], pair[1].created_at))
        return [e for _, e in scored[:k]]


def _frontmatter(entry: MemoryEntry) -> str:
    lines = [
        "---",
        f"id: {entry.id}",
        f"category: {entry.category}",
        f"source: {entry.source}",
        f"scope: {entry.scope}",
        f"created_at: {entry.created_at.isoformat()}",
        f"core: {'true' if entry.core else 'false'}",
    ]
    if entry.task_ref:
        lines.append(f"task_ref: {entry.task_ref}")
    if entry.superseded_by:
        lines.append(f"superseded_by: {entry.superseded_by}")
    lines.append("---")
    return "\n".join(lines)


def _parse(text: str) -> MemoryEntry:
    _, fm, body = text.split("---", 2)
    meta: dict[str, str] = {}
    for line in fm.strip().splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return MemoryEntry(
        id=meta["id"],
        fact=body.strip(),
        category=meta["category"],
        source=Source(meta["source"]),
        scope=meta.get("scope", "global"),
        created_at=datetime.fromisoformat(meta["created_at"]),
        task_ref=meta.get("task_ref"),
        core=meta.get("core") == "true",
        superseded_by=meta.get("superseded_by"),
    )


class MemoryStore:
    def __init__(self, repo: Path, log: EventLog, retriever: Retriever | None = None) -> None:
        self.repo = repo
        self._log = log
        self._retriever = retriever or KeywordRetriever()
        self.repo.mkdir(parents=True, exist_ok=True)
        if not (self.repo / ".git").exists():
            self._git("init", "-q", "-b", "main")

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True)

    def _path(self, entry_id: str) -> Path:
        return self.repo / f"{entry_id}.md"

    def write(
        self,
        fact: str,
        category: str,
        source: Source,
        scope: str = "global",
        task_ref: str | None = None,
        core: bool = False,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            id=uuid.uuid4().hex[:12],
            fact=fact.strip(),
            category=category,
            source=source,
            scope=scope,
            task_ref=task_ref,
            core=core,
        )
        self._path(entry.id).write_text(f"{_frontmatter(entry)}\n\n{entry.fact}\n")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=kestrel",
            "-c",
            "user.email=kestrel@local",
            "commit",
            "-q",
            "-m",
            f"{source}: {fact[:60]}",
        )
        # Visible moment of creation. Without this, memory becomes something that
        # happens to him rather than something he watched happen.
        self._log.append(
            EventKind.MEMORY_WRITTEN,
            "kestrel",
            {"id": entry.id, "fact": entry.fact, "source": str(source), "scope": scope},
            task_id=task_ref,
        )
        return entry

    def supersede(self, old_id: str, fact: str, **kw: object) -> MemoryEntry:
        """Contradiction is never resolved by keeping both.

        The old entry stays on disk for audit with a pointer forward, but only the
        live one is ever read.
        """
        old = self.get(old_id)
        new = self.write(
            fact,
            category=str(kw.get("category", old.category)),
            source=Source(kw.get("source", Source.CORRECTION)),
            scope=str(kw.get("scope", old.scope)),
            task_ref=kw.get("task_ref") or old.task_ref,  # type: ignore[arg-type]
            core=bool(kw.get("core", old.core)),
        )
        old.superseded_by = new.id
        self._path(old.id).write_text(f"{_frontmatter(old)}\n\n{old.fact}\n")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=kestrel",
            "-c",
            "user.email=kestrel@local",
            "commit",
            "-q",
            "-m",
            f"supersede {old.id} -> {new.id}",
        )
        self._log.append(EventKind.MEMORY_SUPERSEDED, "kestrel", {"old": old.id, "new": new.id})
        return new

    def get(self, entry_id: str) -> MemoryEntry:
        return _parse(self._path(entry_id).read_text())

    def all(self, include_superseded: bool = False) -> list[MemoryEntry]:
        entries = [_parse(p.read_text()) for p in sorted(self.repo.glob("*.md"))]
        return entries if include_superseded else [e for e in entries if e.live]

    def scoped(self, project: str | None = None) -> list[MemoryEntry]:
        wanted = {"global"} | ({f"project:{project}"} if project else set())
        return [e for e in self.all() if e.scope in wanted]

    def core(self, project: str | None = None) -> list[str]:
        """The always-injected set: things that shape every response regardless of
        topic. Everything factual is retrieved instead, so this stays small."""
        return [e.fact for e in self.scoped(project) if e.core]

    def recall(self, message: str, project: str | None = None, k: int = 5) -> list[MemoryEntry]:
        candidates = [e for e in self.scoped(project) if not e.core]
        return self._retriever.search(message, candidates, k)

    def unused_since(self, cutoff: datetime, usage: dict[str, datetime]) -> list[MemoryEntry]:
        """The review surface. Nothing auto-expires - auto-expiry deletes true
        things - so stale entries are surfaced for a human decision instead."""
        return [e for e in self.all() if usage.get(e.id, e.created_at) < cutoff]
