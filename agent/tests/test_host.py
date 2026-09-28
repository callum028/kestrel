"""The wire protocol between the session host and its clients.

Exercised directly, without the server in the loop at all, because the point
of this boundary is that the server is just one possible client of it.
"""

import asyncio

import pytest

from kestrel_agent.host import SessionHost, SessionHostAlreadyRunning
from kestrel_agent.host_client import SessionHostClient, SessionHostUnavailable


async def drain_until(queue, needle: bytes, timeout: float = 5.0) -> bytes:
    buffer = b""
    async with asyncio.timeout(timeout):
        while needle not in buffer:
            chunk = await queue.get()
            if chunk is None:
                break
            buffer += chunk
    return buffer


@pytest.fixture
async def host(tmp_path):
    h = SessionHost(tmp_path / "session-host.sock")
    await h.start()
    yield h
    await h.stop()


@pytest.fixture
async def client(host):
    c = SessionHostClient(host.socket_path)
    yield c
    await c.disconnect()


async def test_create_and_list_round_trip(client, tmp_path):
    terminal = await client.create(cwd=tmp_path, command=["/bin/bash", "-c", "sleep 5"])
    listed = await client.list()
    assert [t.id for t in listed] == [terminal.id]
    await client.close(terminal.id)


async def test_a_command_runs_and_its_output_arrives(client, tmp_path):
    terminal = await client.create(
        cwd=tmp_path, command=["/bin/bash", "-c", "echo hello-over-the-wire"]
    )
    queue = await terminal.subscribe()
    output = await drain_until(queue, b"hello-over-the-wire")
    assert b"hello-over-the-wire" in output


async def test_write_reaches_the_pty(client, tmp_path):
    terminal = await client.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    queue = await terminal.subscribe()

    await terminal.write(b"echo from-the-client\n")
    output = await drain_until(queue, b"from-the-client")

    assert b"from-the-client" in output
    await client.close(terminal.id)


async def test_late_subscriber_gets_scrollback_replayed(client, tmp_path):
    terminal = await client.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    first = await terminal.subscribe()
    await terminal.write(b"echo earlier-output\n")
    await drain_until(first, b"earlier-output")

    second = await terminal.subscribe()
    replayed = await second.get()
    assert b"earlier-output" in replayed

    await terminal.write(b"echo later-output\n")
    assert b"later-output" in await drain_until(second, b"later-output")
    assert b"later-output" in await drain_until(first, b"later-output")

    await client.close(terminal.id)


async def test_resize_reaches_the_process(client, tmp_path):
    terminal = await client.create(
        cwd=tmp_path, command=["/bin/bash", "--norc", "-i"], rows=24, cols=80
    )
    queue = await terminal.subscribe()

    await terminal.resize(50, 132)
    await terminal.write(b"tput cols\n")
    output = await drain_until(queue, b"132")

    assert b"132" in output
    await client.close(terminal.id)


async def test_exit_is_announced_with_the_code(client, tmp_path):
    terminal = await client.create(cwd=tmp_path, command=["/bin/bash", "-c", "exit 3"])
    queue = await terminal.subscribe()

    async with asyncio.timeout(5):
        while await queue.get() is not None:
            pass

    assert terminal.exit_code == 3
    assert terminal.alive is False


async def test_closing_reports_honestly_the_second_time(client, tmp_path):
    terminal = await client.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    assert await client.close(terminal.id) is True
    assert await client.close(terminal.id) is False


async def test_a_missing_host_is_a_legible_failure(tmp_path):
    orphan = SessionHostClient(tmp_path / "nothing-is-listening-here.sock")
    with pytest.raises(SessionHostUnavailable):
        await orphan.list()


async def test_a_second_host_refuses_to_steal_a_live_socket(host, tmp_path):
    """The failure this whole design exists to prevent, one level down: a
    second host racing the first (a systemd restart, a stray manual run) must
    not take the socket path while the first is still serving live sessions -
    that would make them unreachable forever, not kill them, which is worse."""
    client = SessionHostClient(host.socket_path)
    terminal = await client.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    queue = await terminal.subscribe()
    await terminal.write(b"echo still-alive\n")
    await drain_until(queue, b"still-alive")

    challenger = SessionHost(host.socket_path)
    with pytest.raises(SessionHostAlreadyRunning):
        await challenger.start()

    # The original host and its terminal must be completely unaffected.
    still_reachable = SessionHostClient(host.socket_path)
    listed = await still_reachable.list()
    assert [t.id for t in listed] == [terminal.id]
    assert listed[0].alive is True

    await terminal.write(b"echo second-message\n")
    more = await drain_until(queue, b"second-message")
    assert b"second-message" in more

    await client.disconnect()
    await still_reachable.disconnect()


async def test_a_second_client_sees_what_the_first_created(host, tmp_path):
    """The property the whole task exists for, at the protocol level: a fresh
    client attaching to an already-running host finds what a previous one
    made, with output intact."""
    first = SessionHostClient(host.socket_path)
    terminal = await first.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    queue = await terminal.subscribe()
    await terminal.write(b"echo still-here\n")
    await drain_until(queue, b"still-here")
    await first.disconnect()

    second = SessionHostClient(host.socket_path)
    listed = await second.list()
    assert [t.id for t in listed] == [terminal.id]
    reattached = listed[0]
    assert reattached.alive is True

    queue2 = await reattached.subscribe()
    replayed = await queue2.get()
    assert b"still-here" in replayed
    await second.disconnect()
