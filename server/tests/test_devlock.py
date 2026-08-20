from datetime import UTC, datetime, timedelta

import pytest

from kestrel.devlock import DevLock

NOW = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)


@pytest.fixture
def lock(conn):
    return DevLock(conn)


def test_first_task_takes_it(lock):
    assert lock.acquire("t1", NOW) is True
    assert lock.holder().task_id == "t1"


def test_second_task_queues_rather_than_failing(lock):
    lock.acquire("t1", NOW)
    assert lock.acquire("t2", NOW) is False
    assert lock.queue() == ["t2"]
    assert lock.holder().task_id == "t1"  # still t1's environment


def test_reacquiring_is_idempotent(lock):
    lock.acquire("t1", NOW)
    assert lock.acquire("t1", NOW + timedelta(minutes=1)) is True


def test_release_hands_over_in_queue_order(lock):
    lock.acquire("t1", NOW)
    lock.acquire("t2", NOW + timedelta(seconds=1))
    lock.acquire("t3", NOW + timedelta(seconds=2))

    assert lock.release("t1") == "t2"
    assert lock.holder() is None
    assert lock.acquire("t2", NOW + timedelta(minutes=5)) is True
    assert lock.queue() == ["t3"]


def test_a_task_that_does_not_hold_it_cannot_release_it(lock):
    lock.acquire("t1", NOW)
    assert lock.release("t2") is None
    assert lock.holder().task_id == "t1"


def test_a_stuck_deploy_is_reportable_because_it_stalls_everything_behind_it(lock):
    lock.acquire("t1", NOW)
    lock.acquire("t2", NOW)

    assert lock.stuck(NOW + timedelta(minutes=5)) is None
    stuck = lock.stuck(NOW + timedelta(minutes=40))
    assert stuck is not None
    assert stuck.task_id == "t1"
    assert lock.queue() == ["t2"]
