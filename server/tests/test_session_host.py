"""Proves the requirement this boundary exists for.

`session_host` (see conftest.py) is a real `python -m kestrel_agent`
subprocess - not a child of the server, not torn down alongside it. This test
throws away the server-side `Runtime` (the client) and builds a new one
against the same running host, which is exactly the shape a redeploy takes:
the process holding the connection is replaced, the process holding the PTY
is not. If this passes, a server restart cannot take a session with it.
"""

import asyncio

from kestrel.config import Config
from kestrel.runtime import Runtime


async def drain_until(queue: asyncio.Queue, needle: bytes, timeout: float = 5.0) -> bytes:
    """A PTY delivers in arbitrary chunks, so asserting on one read is flaky."""
    buffer = b""
    async with asyncio.timeout(timeout):
        while needle not in buffer:
            chunk = await queue.get()
            if chunk is None:
                break
            buffer += chunk
    return buffer


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )


async def test_a_rebuilt_runtime_reattaches_to_a_running_session(session_host, tmp_path):
    config = _config(tmp_path)
    rt = Runtime.build(config)

    terminal = await rt.terminals.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    queue = await terminal.subscribe()
    await terminal.write(b"echo before-the-restart\n")
    await drain_until(queue, b"before-the-restart")
    await terminal.unsubscribe(queue)

    # The server process goes away here, not the session host - `disconnect`
    # drops only this client's socket, then `Runtime.build` stands up a
    # brand-new one with no memory of anything above.
    await rt.terminals.disconnect()
    rt2 = Runtime.build(config)

    listed = await rt2.terminals.list()
    assert [t.id for t in listed] == [terminal.id]
    reattached = listed[0]
    assert reattached.alive is True

    queue2 = await reattached.subscribe()
    replayed = await queue2.get()
    assert b"before-the-restart" in replayed

    await reattached.write(b"echo after-the-restart\n")
    after = await drain_until(queue2, b"after-the-restart")
    assert b"after-the-restart" in after
    assert reattached.alive is True

    await reattached.unsubscribe(queue2)
    await rt2.terminals.disconnect()
