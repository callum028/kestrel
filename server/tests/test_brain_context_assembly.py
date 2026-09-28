from datetime import UTC, datetime

from kestrel.brain.context_assembly import build_prompt, render_for_headless, task_index
from kestrel.memory import MemoryStore, Source
from kestrel.tasks import Task, TaskState


def make_identity_dir(tmp_path):
    d = tmp_path / "identity"
    d.mkdir()
    (d / "identity.md").write_text("You are Kestrel.")
    (d / "examples.md").write_text("> Done.")
    return d


def make_task(handle="KES-31", state=TaskState.RUNNING, nudges=0):
    now = datetime.now(UTC)
    return Task(
        id=handle,
        handle=handle,
        goal="fix the auth bug",
        criteria=[],
        executor="claude_code",
        state=state,
        created_at=now,
        updated_at=now,
        nudges=nudges,
    )


def test_task_index_carries_handle_goal_state_and_nudges():
    lines = task_index([make_task(nudges=2)])
    assert lines == ["KES-31: fix the auth bug (running, 2 nudges)"]


def test_build_prompt_folds_memory_state_and_tasks_in(tmp_path, log):
    identity_dir = make_identity_dir(tmp_path)
    memory = MemoryStore(tmp_path / "memory", log)
    memory.write("prefers integration tests for auth", "convention", Source.EXPLICIT, core=True)

    bundle = build_prompt(
        identity_dir=identity_dir,
        memory=memory,
        attention_block="presence: AT_DESK",
        active_tasks=[make_task()],
        history=[],
        message="what's running?",
    )

    assert "You are Kestrel." in bundle.system
    assert "prefers integration tests for auth" in bundle.system
    assert "presence: AT_DESK" in bundle.system
    assert "KES-31: fix the auth bug" in bundle.system
    assert bundle.messages[-1].content == "what's running?"


def test_build_prompt_retrieves_relevant_memory_by_keyword(tmp_path, log):
    identity_dir = make_identity_dir(tmp_path)
    memory = MemoryStore(tmp_path / "memory", log)
    memory.write("backoff should be exponential on retries", "convention", Source.DECISION)
    memory.write("prefers tabs over spaces", "style", Source.EXPLICIT)

    bundle = build_prompt(
        identity_dir=identity_dir,
        memory=memory,
        attention_block="",
        active_tasks=[],
        history=[],
        message="what's the retry backoff convention?",
    )

    assert "backoff should be exponential on retries" in bundle.system
    assert "prefers tabs over spaces" not in bundle.system


def test_render_for_headless_folds_history_into_system_and_keeps_the_query_separate():
    from kestrel.context import assemble
    from kestrel.models import Message

    bundle = assemble(
        identity="ID",
        examples="EX",
        memory_core=[],
        attention_block="state",
        task_index=[],
        conversation=[
            Message(role="user", content="first"),
            Message(role="assistant", content="ack"),
        ],
    )
    system, query = render_for_headless(bundle)
    assert "Conversation so far" in system
    assert "first" in system
    assert "ack" not in system  # the last message is the query, not history
    assert query == "ack"


def test_render_for_headless_with_a_single_message_has_no_history_section():
    from kestrel.context import assemble
    from kestrel.models import Message

    bundle = assemble(
        identity="ID",
        examples="EX",
        memory_core=[],
        attention_block="state",
        task_index=[],
        conversation=[Message(role="user", content="hello")],
    )
    system, query = render_for_headless(bundle)
    assert "Conversation so far" not in system
    assert query == "hello"
