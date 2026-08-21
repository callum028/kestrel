"""Observations - the product of looking past the brief.

Attention is unbounded, action is bounded. Staying in scope constrains what
Kestrel does, not what it looks at: adjacent bugs, dead code, a broken
convention, a missing test, an unnamed risk.

Two things stop this becoming a nagging system:

- dismissal is durable, keyed on a fingerprint, so the same thing is never
  raised twice unless the underlying facts change
- an observation is never urgent and can never reach a phone call

Trivial fixes may land without asking. The justification is NOT PR review - PRs
merge automatically on green CI, so a PR is not a reliable human gate. It rests
entirely on the bounds below being absolute rather than a judgement call, and on
disclosure being loud enough that an unrequested change is never discovered by
accident.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .db import Database
from .events import EventKind, EventLog
from .outcomes import Ok, Outcome, Refused

MAX_FIXES_IN_PLACE = 3
MAX_FIX_LINES = 12

# Categories that cannot change behaviour. Anything not on this list is an
# observation, and observations ask.
NON_BEHAVIOURAL = frozenset({"typo", "dead_import", "stale_comment", "formatting", "docstring"})

# Touching any of these makes a change externally visible or behavioural.
FORBIDDEN_PATHS = ("package.json", "requirements.txt", "pyproject.toml", "schema", "migrations")


class ObservationState(StrEnum):
    OPEN = "open"
    FIXED = "fixed"  # fixed in place, within bounds, disclosed
    PROMOTED = "promoted"  # became a ticket
    FOLDED = "folded"  # became real work on the current task
    DISMISSED = "dismissed"  # durable - never raised again


@dataclass(frozen=True)
class Observation:
    id: str
    what: str
    location: str | None
    why: str | None
    state: ObservationState
    fingerprint: str
    task_id: str | None
    created_at: datetime


def fingerprint(what: str, location: str | None) -> str:
    """Stable across runs so dismissal survives. Deliberately excludes the task
    that surfaced it - the same dead code noticed twice is the same observation."""
    raw = f"{what.strip().lower()}|{(location or '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ProposedFix:
    category: str
    files: list[str]
    lines_changed: int
    adds_dependency: bool = False
    changes_test_expectations: bool = False


class ObservationStore:
    def __init__(self, conn: Database, log: EventLog) -> None:
        self._conn = conn
        self._log = log

    def raise_(
        self,
        what: str,
        location: str | None = None,
        why: str | None = None,
        task_id: str | None = None,
    ) -> Observation | None:
        """Returns None if this has been seen before - already open, already
        actioned, or dismissed. Silence here is correct: re-raising is nagging."""
        fp = fingerprint(what, location)
        existing = self._conn.execute(
            "SELECT * FROM observations WHERE fingerprint = ?", (fp,)
        ).fetchone()
        if existing is not None:
            return None

        obs = Observation(
            id=uuid.uuid4().hex[:12],
            what=what,
            location=location,
            why=why,
            state=ObservationState.OPEN,
            fingerprint=fp,
            task_id=task_id,
            created_at=datetime.now(UTC),
        )
        self._conn.execute(
            """INSERT INTO observations
               (id, task_id, what, location, why, state, fingerprint, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (obs.id, task_id, what, location, why, str(obs.state), fp, obs.created_at.isoformat()),
        )
        self._log.append(
            EventKind.OBSERVATION_RAISED,
            "kestrel",
            {"id": obs.id, "what": what, "location": location},
            task_id=task_id,
        )
        return obs

    def resolve(self, obs_id: str, state: ObservationState) -> None:
        self._conn.execute("UPDATE observations SET state = ? WHERE id = ?", (str(state), obs_id))
        self._log.append(
            EventKind.OBSERVATION_RESOLVED, "kestrel", {"id": obs_id, "as": str(state)}
        )

    def open(self) -> list[Observation]:
        rows = self._conn.execute(
            "SELECT * FROM observations WHERE state = ? ORDER BY created_at",
            (str(ObservationState.OPEN),),
        ).fetchall()
        return [self._to_obs(r) for r in rows]

    @staticmethod
    def _to_obs(row: sqlite3.Row) -> Observation:
        return Observation(
            id=row["id"],
            what=row["what"],
            location=row["location"],
            why=row["why"],
            state=ObservationState(row["state"]),
            fingerprint=row["fingerprint"],
            task_id=row["task_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


def may_fix_in_place(fix: ProposedFix, task_files: set[str], fixes_already_made: int) -> Outcome:
    """All bounds must hold. Missing any one makes it an observation, and
    observations ask. No judgement is involved anywhere in this function."""
    if fix.category not in NON_BEHAVIOURAL:
        return Refused(reason=f"{fix.category} can change behaviour - raise it instead")

    outside = [f for f in fix.files if f not in task_files]
    if outside:
        return Refused(reason=f"outside the files this task touches: {', '.join(outside)}")

    if fix.lines_changed > MAX_FIX_LINES:
        return Refused(reason=f"{fix.lines_changed} lines is past the {MAX_FIX_LINES} line bound")

    if fix.adds_dependency:
        return Refused(reason="adds a dependency")

    if fix.changes_test_expectations:
        return Refused(reason="changes test expectations")

    forbidden = [f for f in fix.files if any(p in f for p in FORBIDDEN_PATHS)]
    if forbidden:
        return Refused(reason=f"touches config, schema or manifest: {', '.join(forbidden)}")

    if fixes_already_made >= MAX_FIXES_IN_PLACE:
        # Three unrelated small fixes is scope creep however small each one is.
        return Refused(reason=f"already made {fixes_already_made} fixes in place on this task")

    return Ok(value=fix)


def disclosure(fix: ProposedFix, what: str) -> str:
    """Disclosure is mandatory and goes to the PR description first - that is
    where the code is actually looked at. A Notion comment alone is skimmable."""
    return f"Fixed in place while here: {what} ({fix.category}, {', '.join(fix.files)})."
