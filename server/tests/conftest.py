import subprocess
import sys
import time

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


@pytest.fixture
def session_host(tmp_path):
    """A real `python -m kestrel_agent`, as a subprocess - not an in-process
    fake. The property under test elsewhere in this suite is the client/server
    boundary itself, which an in-process stand-in would paper over. Config
    derives the socket path from `data_dir`, so a test's `Config(data_dir=
    tmp_path, ...)` finds this one without being told about it explicitly.
    """
    socket_path = tmp_path / "session-host.sock"
    proc = subprocess.Popen([sys.executable, "-m", "kestrel_agent", "--socket", str(socket_path)])
    try:
        deadline = time.monotonic() + 5
        while not socket_path.exists():
            if proc.poll() is not None:
                raise RuntimeError("session host exited before it started listening")
            if time.monotonic() > deadline:
                raise TimeoutError("session host did not start listening in time")
            time.sleep(0.02)
        yield socket_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
