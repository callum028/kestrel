"""The PTY chain, proven for real - actual processes, actual bytes.

This is spike 3 as an automated test rather than a throwaway: if these pass, a
terminal in the app is wiring rather than risk.
"""

import asyncio

import pytest

from kestrel_agent.terminals import TerminalManager


async def drain_until(queue, needle: bytes, timeout: float = 5.0) -> bytes:
    """Read until the needle appears. A PTY delivers in arbitrary chunks, so
    asserting on a single read is how you write a flaky test."""
    buffer = b""
    async with asyncio.timeout(timeout):
        while needle not in buffer:
            chunk = await queue.get()
            if chunk is None:
                break
            buffer += chunk
    return buffer


@pytest.fixture
async def manager():
    """Async so teardown still has a running loop to detach readers from."""
    m = TerminalManager()
    yield m
    m.close_all()


async def test_a_command_runs_and_its_output_arrives(manager, tmp_path):
    terminal = manager.create(cwd=tmp_path, command=["/bin/bash", "-c", "echo hello-kestrel"])
    output = await drain_until(terminal.subscribe(), b"hello-kestrel")
    assert b"hello-kestrel" in output


async def test_an_interactive_shell_takes_input(manager, tmp_path):
    terminal = manager.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    queue = terminal.subscribe()

    await terminal.write(b"echo from-stdin\n")
    output = await drain_until(queue, b"from-stdin")

    assert b"from-stdin" in output
    assert terminal.alive


async def test_it_starts_in_the_directory_it_was_given(manager, tmp_path):
    project = tmp_path / "gymfront-edge"
    project.mkdir()
    terminal = manager.create(cwd=project, command=["/bin/bash", "-c", "pwd"])
    output = await drain_until(terminal.subscribe(), b"gymfront-edge")
    assert b"gymfront-edge" in output


async def test_a_second_client_attaches_to_the_same_session(manager, tmp_path):
    """The phone picking up what the desktop was watching."""
    terminal = manager.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    first = terminal.subscribe()
    await terminal.write(b"echo earlier-output\n")
    await drain_until(first, b"earlier-output")

    # Attaching late must not mean attaching blind.
    second = terminal.subscribe()
    replayed = await second.get()
    assert b"earlier-output" in replayed

    await terminal.write(b"echo later-output\n")
    assert b"later-output" in await drain_until(second, b"later-output")
    assert b"later-output" in await drain_until(first, b"later-output")


async def test_kestrel_and_callum_share_one_session(manager, tmp_path):
    """Nudges go into the same PTY, not a parallel channel."""
    terminal = manager.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    queue = terminal.subscribe()

    await asyncio.gather(
        terminal.write(b"echo typed-by-callum\n"),
        terminal.write(b"echo written-by-kestrel\n"),
    )

    output = await drain_until(queue, b"written-by-kestrel")
    assert b"typed-by-callum" in output
    assert b"written-by-kestrel" in output


async def test_resize_reaches_the_process(manager, tmp_path):
    terminal = manager.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"], rows=24, cols=80)
    queue = terminal.subscribe()
    terminal.resize(50, 132)

    await terminal.write(b"tput cols\n")
    output = await drain_until(queue, b"132")
    assert b"132" in output


async def test_exit_is_announced_and_the_code_is_kept(manager, tmp_path):
    terminal = manager.create(cwd=tmp_path, command=["/bin/bash", "-c", "exit 3"])
    queue = terminal.subscribe()

    async with asyncio.timeout(5):
        while await queue.get() is not None:
            pass

    assert await terminal.wait() == 3
    assert terminal.alive is False


async def test_terminals_are_listed_and_can_be_scoped_to_a_task(manager, tmp_path):
    free = manager.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    bound = manager.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"], task_id="t1")

    assert {t.id for t in manager.list()} == {free.id, bound.id}
    assert [t.id for t in manager.list(task_id="t1")] == [bound.id]


async def test_closing_kills_the_process(manager, tmp_path):
    terminal = manager.create(cwd=tmp_path, command=["/bin/bash", "--norc", "-i"])
    assert manager.close(terminal.id) is True

    async with asyncio.timeout(5):
        while terminal.alive:
            await asyncio.sleep(0.05)

    assert manager.get(terminal.id) is None
    assert manager.close(terminal.id) is False
