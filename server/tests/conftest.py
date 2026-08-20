import pytest

from kestrel.db import connect
from kestrel.events import EventLog
from kestrel.tasks import TaskStore


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def log(conn):
    return EventLog(conn)


@pytest.fixture
def tasks(conn, log):
    return TaskStore(conn, log)
