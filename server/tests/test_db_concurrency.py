"""Concurrent access, which is how the API actually behaves.

FastAPI runs sync endpoints in a threadpool while the tick loop runs on the
event loop. A single shared sqlite connection survives every single-threaded
test and then raises "bad parameter or other API misuse" the moment two
requests overlap - which is exactly what it did in the browser.
"""

import threading

from kestrel.db import connect
from kestrel.events import EventKind, EventLog
from kestrel.tasks import TaskStore

THREADS = 8
PER_THREAD = 25


def test_many_threads_can_read_and_write_at_once(tmp_path):
    db = connect(tmp_path / "busy.db")
    log = EventLog(db)
    tasks = TaskStore(db, log)
    tasks.create("KES-1", "goal", ["tests pass"], "claude_code")

    errors: list[Exception] = []
    barrier = threading.Barrier(THREADS)

    def hammer(worker: int) -> None:
        barrier.wait()  # maximise overlap rather than hoping for it
        try:
            for i in range(PER_THREAD):
                log.append(EventKind.TOOL_CALL, "claude", {"worker": worker, "i": i})
                tasks.active()
                tasks.by_handle("KES-1")
        except Exception as exc:  # noqa: BLE001 - the point is to catch anything
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    written = len(log.of_kind(EventKind.TOOL_CALL, limit=1000))
    assert written == THREADS * PER_THREAD
