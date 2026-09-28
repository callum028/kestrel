"""Request shapes against Notion's REST API, and the in-memory fake used
everywhere else. No test in this file calls the real API - `httpx.MockTransport`
stands in for the network in every NotionBoard test."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from kestrel.board import FakeBoard, NotionBoard

DATABASE_ID = "db-123"
DATA_SOURCE_ID = "ds-456"
PAGE_ID = "page-789"


def _database_response() -> dict:
    return {
        "object": "database",
        "id": DATABASE_ID,
        "data_sources": [{"id": DATA_SOURCE_ID, "name": "Tasks"}],
        "properties": {
            "Status": {"type": "status"},
            "Name": {"type": "title"},
        },
    }


def _page(lane: str = "To Do", flagged: bool = False) -> dict:
    return {
        "object": "page",
        "id": PAGE_ID,
        "url": f"https://notion.so/{PAGE_ID}",
        "last_edited_time": "2026-09-01T12:00:00.000Z",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Fix the thing"}]},
            "ID": {"type": "unique_id", "unique_id": {"prefix": "KES", "number": 31}},
            "Status": {"type": "status", "status": {"name": lane}},
            "Kestrel flag": {"type": "checkbox", "checkbox": flagged},
            "PR": {"type": "url", "url": None},
            "Project": {"type": "rich_text", "rich_text": [{"plain_text": "kestrel"}]},
        },
    }


class Recorder:
    """Captures every request the client makes, and answers with canned bodies
    keyed by method+path prefix."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, responses: dict[str, dict]):
        def _handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            for key, body in responses.items():
                method, _, path_prefix = key.partition(" ")
                if request.method == method and str(request.url.path).startswith(path_prefix):
                    return httpx.Response(200, json=body)
            return httpx.Response(404, json={"object": "error", "message": "not stubbed"})

        return _handle


def _client_for(recorder: Recorder, responses: dict[str, dict]) -> httpx.AsyncClient:
    transport = httpx.MockTransport(recorder.handler(responses))
    return httpx.AsyncClient(base_url="https://api.notion.com", transport=transport)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


async def test_create_page_request_shape(recorder):
    responses = {
        "GET /v1/databases/": _database_response(),
        "POST /v1/pages": _page(),
    }
    client = _client_for(recorder, responses)
    board = NotionBoard("secret-token", DATABASE_ID, client=client)

    ticket = await board.create("Fix the thing", body="do it", lane="To Do", project="kestrel")

    create_req = next(r for r in recorder.requests if r.method == "POST" and r.url.path == "/v1/pages")
    assert create_req.headers["authorization"] == "Bearer secret-token"
    assert create_req.headers["notion-version"]
    body = json.loads(create_req.content)
    assert body["parent"] == {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID}
    assert body["properties"]["Name"]["title"][0]["text"]["content"] == "Fix the thing"
    assert body["properties"]["Status"] == {"status": {"name": "To Do"}}
    assert body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "do it"
    assert ticket.handle == "KES-31"
    assert ticket.title == "Fix the thing"


async def test_set_lane_uses_the_detected_property_type(recorder):
    responses = {
        "GET /v1/databases/": _database_response(),
        "PATCH /v1/pages/": {},
    }
    client = _client_for(recorder, responses)
    board = NotionBoard("t", DATABASE_ID, client=client)

    await board.set_lane(PAGE_ID, "In Progress")

    patch_req = next(r for r in recorder.requests if r.method == "PATCH")
    body = json.loads(patch_req.content)
    assert body == {"properties": {"Status": {"status": {"name": "In Progress"}}}}


async def test_set_lane_uses_select_when_schema_says_select(recorder):
    db = _database_response()
    db["properties"]["Status"]["type"] = "select"
    responses = {"GET /v1/databases/": db, "PATCH /v1/pages/": {}}
    client = _client_for(recorder, responses)
    board = NotionBoard("t", DATABASE_ID, client=client)

    await board.set_lane(PAGE_ID, "Done")

    patch_req = next(r for r in recorder.requests if r.method == "PATCH")
    assert json.loads(patch_req.content) == {"properties": {"Status": {"select": {"name": "Done"}}}}


async def test_add_comment_request_shape(recorder):
    responses = {"POST /v1/comments": {}}
    client = _client_for(recorder, responses)
    board = NotionBoard("t", DATABASE_ID, client=client)

    await board.add_comment(PAGE_ID, "parked: stalled")

    comment_req = next(r for r in recorder.requests if r.url.path == "/v1/comments")
    body = json.loads(comment_req.content)
    assert body["parent"] == {"page_id": PAGE_ID}
    assert body["rich_text"][0]["text"]["content"] == "parked: stalled"


async def test_query_with_filter_for_changed_since(recorder):
    responses = {
        "GET /v1/databases/": _database_response(),
        "POST /v1/data_sources/": {"results": [], "has_more": False, "next_cursor": None},
    }
    client = _client_for(recorder, responses)
    board = NotionBoard("t", DATABASE_ID, client=client)

    since = datetime(2026, 9, 1, tzinfo=UTC)
    tickets = await board.changed_since(since)

    query_req = next(r for r in recorder.requests if r.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query")
    body = json.loads(query_req.content)
    assert body["filter"]["timestamp"] == "last_edited_time"
    assert body["filter"]["last_edited_time"]["after"].startswith("2026-09-01")
    assert tickets == []


async def test_get_reads_body_from_block_children(recorder):
    responses = {
        "GET /v1/pages/": _page(),
        "GET /v1/blocks/": {
            "results": [
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "line one"}]}},
                {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "a point"}]}},
            ],
            "has_more": False,
            "next_cursor": None,
        },
    }
    client = _client_for(recorder, responses)
    board = NotionBoard("t", DATABASE_ID, client=client)

    ticket = await board.get(PAGE_ID)

    assert ticket is not None
    assert ticket.body == "line one\n- a point"
    assert ticket.lane == "To Do"
    assert ticket.project == "kestrel"


async def test_get_returns_none_for_404(recorder):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"object": "error"})

    client = httpx.AsyncClient(
        base_url="https://api.notion.com", transport=httpx.MockTransport(handler)
    )
    board = NotionBoard("t", DATABASE_ID, client=client)

    assert await board.get("missing") is None


# -- FakeBoard --------------------------------------------------------------


async def test_fake_board_create_and_find_by_handle():
    board = FakeBoard()
    ticket = await board.create("Fix the thing", lane="To Do")

    assert await board.find_by_handle(ticket.handle) == ticket
    assert await board.find_by_handle("nope") is None


async def test_fake_board_set_lane_and_flag_update_last_edited_time():
    board = FakeBoard()
    ticket = await board.create("Fix the thing")
    before = ticket.last_edited_time

    await board.set_lane(ticket.id, "In Progress")
    updated = await board.get(ticket.id)

    assert updated.lane == "In Progress"
    assert updated.last_edited_time > before


async def test_fake_board_changed_since_filters_by_time():
    import asyncio

    board = FakeBoard()
    t1 = await board.create("One")
    await asyncio.sleep(0.001)
    cutoff = datetime.now(UTC)
    await asyncio.sleep(0.001)
    t2 = await board.create("Two")

    changed = await board.changed_since(cutoff)

    assert [t.id for t in changed] == [t2.id]
    assert {t.id for t in await board.changed_since(None)} == {t1.id, t2.id}
