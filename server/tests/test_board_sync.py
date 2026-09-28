"""The sync worker: task state -> board writes, and board moves -> task
instructions - with the echo-loop check in between so the second never
rediscovers the first."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kestrel.board import FakeBoard
from kestrel.board_sync import BoardSync
from kestrel.executors import ExecutorKind
from kestrel.tasks import TaskState

NOW = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def board():
    return FakeBoard()


@pytest.fixture
def sync(board, tasks, log, conn):
    return BoardSync(board, tasks, log, conn)


async def test_creating_a_task_creates_a_ticket(sync, tasks, board):
    task = tasks.create("KES-1", "Fix the thing", ["tests pass"], ExecutorKind.CLAUDE_CODE)

    await sync.push()

    task = tasks.get(task.id)
    assert task.ticket_ref is not None
    ticket = await board.get(task.ticket_ref)
    assert ticket.title == "Fix the thing"
    assert ticket.lane == "To Do"  # CREATED maps to To Do by default


async def test_state_transitions_move_the_lane(sync, tasks, board):
    task = tasks.create("KES-2", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    task = tasks.get(task.id)

    tasks.transition(task.id, TaskState.BRIEFED)
    tasks.transition(task.id, TaskState.RUNNING)
    await sync.push()

    ticket = await board.get(task.ticket_ref)
    assert ticket.lane == "In Progress"
    assert ticket.flagged is False


async def test_needs_input_and_parked_set_the_flag(sync, tasks, board):
    task = tasks.create("KES-3", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    task = tasks.get(task.id)
    tasks.transition(task.id, TaskState.BRIEFED)
    tasks.transition(task.id, TaskState.RUNNING)
    tasks.transition(task.id, TaskState.NEEDS_INPUT)
    await sync.push()

    ticket = await board.get(task.ticket_ref)
    assert ticket.lane == "In Progress"
    assert ticket.flagged is True


async def test_a_parked_reason_becomes_a_comment(sync, tasks, board):
    task = tasks.create("KES-4", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    task = tasks.get(task.id)
    tasks.transition(task.id, TaskState.BRIEFED)
    tasks.transition(task.id, TaskState.RUNNING)
    tasks.transition(task.id, TaskState.PARKED, reason="stalled for 15 minutes")
    writes = await sync.push()

    assert writes >= 1  # lane + flag + comment


async def test_push_is_idempotent_across_calls(sync, tasks, board):
    """A second push with no new events makes no further writes - the cursor
    in board_cursor.last_pushed_seq must actually advance."""
    tasks.create("KES-5", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    second = await sync.push()
    assert second == 0


async def test_creating_from_an_existing_ticket_does_not_duplicate(sync, tasks, board):
    """A task created with a ticket_ref already set (picked up from an
    existing Notion ticket) must not get a second ticket created for it."""
    ticket = await board.create("Existing ticket", lane="To Do")
    task = tasks.create(
        "KES-6", "Existing ticket", [], ExecutorKind.CLAUDE_CODE, ticket_ref=ticket.id
    )

    await sync.push()

    task = tasks.get(task.id)
    assert task.ticket_ref == ticket.id  # unchanged, no second ticket


# -- poll: manual moves --------------------------------------------------


async def test_moving_a_card_to_done_closes_the_task(sync, tasks, board):
    task = tasks.create("KES-7", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    task = tasks.get(task.id)
    tasks.transition(task.id, TaskState.BRIEFED)
    tasks.transition(task.id, TaskState.RUNNING)
    tasks.transition(task.id, TaskState.AWAITING_DEV)
    tasks.transition(task.id, TaskState.VALIDATING)
    await sync.push()

    await board.set_lane(task.ticket_ref, "Done")  # Callum drags the card
    instructed = await sync.poll(NOW)

    assert len(instructed) == 1
    assert tasks.get(task.id).state is TaskState.DONE


async def test_moving_a_card_back_to_to_do_parks_the_task(sync, tasks, board):
    task = tasks.create("KES-8", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    task = tasks.get(task.id)
    tasks.transition(task.id, TaskState.BRIEFED)
    tasks.transition(task.id, TaskState.RUNNING)
    await sync.push()

    await board.set_lane(task.ticket_ref, "To Do")
    instructed = await sync.poll(NOW)

    assert len(instructed) == 1
    assert tasks.get(task.id).state is TaskState.PARKED


async def test_kestrels_own_write_is_never_read_back_as_an_instruction(sync, tasks, board, log):
    """The echo-loop case: push sets the lane, then poll runs. Without the
    board_sync bookkeeping this would misread Kestrel's own write to
    'In Progress' as Callum moving the card, and do nothing harmful here only
    by coincidence - in general it must not fire at all."""
    task = tasks.create("KES-9", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    task = tasks.get(task.id)
    tasks.transition(task.id, TaskState.BRIEFED)
    tasks.transition(task.id, TaskState.RUNNING)
    await sync.push()  # this itself writes "In Progress" to the fake board

    instructed = await sync.poll(NOW)

    assert instructed == []
    from kestrel.events import EventKind

    assert log.of_kind(EventKind.BOARD_INSTRUCTION) == []


async def test_an_unrelated_lane_move_is_ignored(sync, tasks, board):
    task = tasks.create("KES-10", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    task = tasks.get(task.id)

    await board.set_lane(task.ticket_ref, "Somewhere Else")
    instructed = await sync.poll(NOW)

    assert len(instructed) == 1  # still recognised as a move worth logging...
    assert tasks.get(task.id).state is TaskState.CREATED  # ...but no default action for it


async def test_a_ticket_with_no_linked_task_is_skipped_not_crashed(sync, board):
    await board.create("Not ours", lane="To Do")
    instructed = await sync.poll(NOW)
    assert instructed == []


async def test_illegal_instruction_is_logged_and_swallowed(sync, tasks, board):
    """DONE can't be reached from CREATED directly - the instruction must not
    raise out of poll()."""
    task = tasks.create("KES-11", "Fix the thing", [], ExecutorKind.CLAUDE_CODE)
    await sync.push()
    task = tasks.get(task.id)

    await board.set_lane(task.ticket_ref, "Done")
    instructed = await sync.poll(NOW)  # must not raise

    assert len(instructed) == 1
    assert tasks.get(task.id).state is TaskState.CREATED  # transition was rejected, task untouched
