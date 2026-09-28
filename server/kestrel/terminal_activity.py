"""Human-in-the-session tracking.

Kestrel must never type into a session Callum is actively in - the desk
experience (§7) is that the terminal can be typed into directly at any time,
and an assistant that stomps on a keystroke because it also wanted to nudge
the same session would make that promise worthless.

The signal is deliberately narrow: the *server's own terminal websocket* is
the only path a human's keystrokes take (the desktop and phone clients relay
into `/terminals/{id}/ws`), so recording a timestamp there and nowhere else
is sufficient - an executor writing into the same PTY through
`SessionHostClient` directly never touches this tracker, so it cannot mark
itself active by mistake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

DEFAULT_WINDOW = timedelta(minutes=2)


@dataclass
class HumanActivityTracker:
    window: timedelta = DEFAULT_WINDOW
    _last_keystroke: dict[str, datetime] = field(default_factory=dict)

    def mark(self, terminal_id: str, at: datetime | None = None) -> None:
        self._last_keystroke[terminal_id] = at or datetime.now(UTC)

    def active(self, terminal_id: str, now: datetime | None = None) -> bool:
        """True if a human typed into this terminal within the window."""
        last = self._last_keystroke.get(terminal_id)
        if last is None:
            return False
        return (now or datetime.now(UTC)) - last < self.window
