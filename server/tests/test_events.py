from kestrel.events import EventKind


def test_append_returns_persisted_event(log):
    e = log.append(EventKind.TASK_CREATED, "kestrel", {"handle": "KES-1"}, task_id="t1")
    assert e.seq == 1
    assert e.kind is EventKind.TASK_CREATED
    assert e.payload == {"handle": "KES-1"}
    assert e.task_id == "t1"


def test_ordering_is_stable(log):
    for i in range(5):
        log.append(EventKind.TASK_PROGRESS, "agent", {"i": i}, task_id="t1")
    assert [e.payload["i"] for e in log.replay()] == [0, 1, 2, 3, 4]


def test_task_scoped_read_excludes_other_tasks(log):
    log.append(EventKind.TASK_PROGRESS, "agent", task_id="t1")
    log.append(EventKind.TASK_PROGRESS, "agent", task_id="t2")
    log.append(EventKind.TASK_PROGRESS, "agent", task_id="t1")
    assert len(log.for_task("t1")) == 2


def test_since_is_a_cursor(log):
    log.append(EventKind.TASK_CREATED, "kestrel")
    second = log.append(EventKind.TASK_BRIEFED, "kestrel")
    assert [e.seq for e in log.since(second.seq - 1)] == [second.seq]
