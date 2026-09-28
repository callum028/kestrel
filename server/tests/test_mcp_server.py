"""kestrel-mcp's tools, driven in-process against the real FastAPI app.

`httpx.ASGITransport` puts the real `api.py` behind the same `httpx.AsyncClient`
`build_server` is given, so this exercises the genuine HTTP boundary (auth
header, request/response shapes, the new brain-step endpoints) without a real
socket or a hand-rolled fake of the API - the "fake MCP round-trip" the step
brief asks for, minus only the stdio framing itself (covered by the `mcp` SDK,
not this code).
"""

from __future__ import annotations

import json

import httpx
import pytest

from kestrel.api import create_app
from kestrel.brain.mcp_server import build_server
from kestrel.config import Config
from kestrel.events import EventKind
from kestrel.runtime import Runtime


@pytest.fixture
def rt_and_client(tmp_path):
    config = Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )
    rt = Runtime.build(config)
    app = create_app(runtime=rt)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {app.state.token}"},
    )
    return rt, client


@pytest.fixture
async def server(rt_and_client):
    rt, client = rt_and_client
    s = build_server(base_url="http://test", token="unused", client=client)
    yield s, rt
    await client.aclose()


async def call(server, name: str, **kwargs):
    """`FastMCP.call_tool` returns `(content, structured)` for a tool typed to
    return a concrete shape (our `dict[str, Any]` tools), and a bare content
    list - one block per list element - for a tool typed `-> Any` (our
    "list everything" tools, since their payload is a JSON list). Both are
    unwrapped back into plain Python here so the tests read the way the real
    MCP client would after JSON-decoding a tool result."""
    result = await server.call_tool(name, kwargs)
    if isinstance(result, tuple):
        _, structured = result
        return structured
    # A tool typed `-> Any` returning a Python list comes back as one content
    # block per element, however many there are - including one.
    return [json.loads(item.text) for item in result]


# --- tasks -------------------------------------------------------------------


async def test_start_task_creates_a_ticket_from_free_text_then_a_task(server):
    s, rt = server
    result = await call(s, "start_task", goal="fix the auth bug")
    assert result["status"] == "ok"
    handle = result["handle"]
    assert rt.tasks.by_handle(handle) is not None


async def test_start_task_with_an_existing_handle_skips_ticket_creation(server):
    s, _ = server
    result = await call(s, "start_task", handle="KES-31", goal="fix it")
    assert result["status"] == "ok"
    assert result["handle"] == "KES-31"


async def test_start_task_without_handle_or_goal_is_refused(server):
    s, _ = server
    result = await call(s, "start_task")
    assert result["status"] == "refused"


async def test_list_tasks_and_task_detail(server):
    s, _rt = server
    await call(s, "start_task", handle="KES-31", goal="fix it")

    listed = await call(s, "list_tasks")
    assert any(t["handle"] == "KES-31" for t in listed)

    detail = await call(s, "task_detail", handle="KES-31")
    assert detail["status"] == "ok"
    assert detail["state"] == "created"
    assert detail["pending_question"] is None


async def test_task_detail_surfaces_a_pending_question_verbatim(server):
    s, rt = server
    task = rt.tasks.create(handle="KES-31", goal="fix it", criteria=[], executor="claude_code")
    rt.log.append(
        EventKind.TASK_QUESTION,
        "claude",
        {"message": "Is the retry limit per-request or per-session?"},
        task_id=task.id,
    )

    detail = await call(s, "task_detail", handle="KES-31")
    assert detail["pending_question"] == "Is the retry limit per-request or per-session?"


async def test_task_detail_surfaces_the_final_report_and_ci_state(server):
    s, rt = server
    task = rt.tasks.create(handle="KES-31", goal="fix it", criteria=[], executor="claude_code")
    rt.log.append(
        EventKind.CI_CHECKED,
        "kestrel",
        {"pr_number": 3, "state": "success", "summary": "all green"},
        task_id=task.id,
    )
    rt.log.append(
        EventKind.TASK_CLOSED,
        "claude",
        {
            "claimed": "done",
            "last_assistant_message": "Done.",
            "report": "Done.",
            "stop_reason": "end_turn",
        },
        task_id=task.id,
    )

    detail = await call(s, "task_detail", handle="KES-31")
    assert detail["final_report"] == "Done."
    assert detail["ci"] == {"pr_number": 3, "state": "success", "summary": "all green"}
    assert detail["validation"] is None


async def test_reply_to_task_without_a_registered_executor_is_refused(server):
    s, rt = server
    rt.tasks.create(handle="KES-31", goal="fix it", criteria=[], executor="claude_code")
    result = await call(s, "reply_to_task", handle="KES-31", text="use exponential backoff")
    assert result["status"] == "refused"


async def test_stop_and_retry_a_task_not_found(server):
    s, _ = server
    assert (await call(s, "stop_task", handle="NOPE"))["status"] == "not_found"
    assert (await call(s, "retry_task", handle="NOPE"))["status"] == "not_found"


# --- board ---------------------------------------------------------------


async def test_create_and_find_ticket(server):
    s, _ = server
    created = await call(s, "create_ticket", title="Fix the thing")
    found = await call(s, "find_ticket", handle=created["handle"])
    assert found["title"] == "Fix the thing"


async def test_list_board_includes_created_tickets(server):
    s, _ = server
    await call(s, "create_ticket", title="One")
    board = await call(s, "list_board")
    assert any(t["title"] == "One" for t in board)


# --- mail ------------------------------------------------------------------


async def test_check_email_lists_the_fake_fixture(server):
    s, _ = server
    recent = await call(s, "check_email")
    assert len(recent) == 1
    assert "fake mail reader" in recent[0]["subject"]


async def test_read_email_is_wrapped_as_untrusted(server):
    s, _ = server
    recent = await call(s, "check_email")
    message = await call(s, "read_email", message_id=recent[0]["id"])
    assert message["status"] == "ok"
    assert "UNTRUSTED DATA" in message["untrusted"]


async def test_email_to_task_creates_both_a_ticket_and_a_task(server):
    s, rt = server
    recent = await call(s, "check_email")
    result = await call(s, "email_to_task", message_id=recent[0]["id"])
    assert result["status"] == "ok"
    assert rt.tasks.by_handle(result["handle"]) is not None


# --- memory ------------------------------------------------------------------


async def test_remember_and_list_memories(server):
    s, _rt = server
    written = await call(
        s, "remember", fact="prefers integration tests for auth", category="convention"
    )
    assert written["status"] == "ok"
    memories = await call(s, "list_memories")
    assert any(m["id"] == written["id"] for m in memories)


async def test_forget_removes_a_memory(server):
    s, _rt = server
    written = await call(s, "remember", fact="temporary note", category="misc")
    result = await call(s, "forget", entry_id=written["id"], reason="no longer true")
    assert result["status"] == "ok"
    memories = await call(s, "list_memories")
    assert all(m["id"] != written["id"] for m in memories)


async def test_add_rule_is_always_core(server):
    s, rt = server
    result = await call(s, "add_rule", rule="Never merge without green CI")
    entry = rt.memory.get(result["id"])
    assert entry.core is True
    assert entry.category == "rule"


# --- ask_repo ------------------------------------------------------------


async def test_ask_repo_refuses_an_unknown_project(rt_and_client):
    _rt, client = rt_and_client
    s = build_server(base_url="http://test", token="unused", client=client, projects={})
    result = await call(s, "ask_repo", question="what does foo() do?", project="unknown")
    assert result["status"] == "refused"
    await client.aclose()
