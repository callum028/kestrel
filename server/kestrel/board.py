"""The board - Notion is the source of truth for tasks.

Every task is a Notion ticket, and Kestrel has full control of the board,
including moving cards to Done. Claude never touches it: board updates are
deterministic side-effects of task state transitions (see `board_sync.py`),
never a model decision.

This module is the boundary. `Board` is the interface `board_sync.py` and the
rest of the server code against; `NotionBoard` is the real thing over Notion's
REST API; `FakeBoard` is an in-memory stand-in used by tests and by dev mode
when no token is configured.

Lane names are config, not code - Callum's board has extra lanes beyond the
rough mapping in the design doc, and the mapping must flex without a deploy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from .tasks import TaskState

# Notion's stable, GA API version as of this writing. The 2025-09-03 release
# split "databases" (the container) from "data sources" (what actually gets
# queried/filtered) to support multi-source databases; this client always
# resolves a database id to its first data source before querying it, which is
# the only shape that works from this version onward. Later versions exist in
# preview (2026-03-11 at the time of writing) but are not pinned here - moving
# to one is a deliberate config change, not a silent drift.
NOTION_API_VERSION = "2025-09-03"
NOTION_BASE_URL = "https://api.notion.com"

# Rough lane mapping from the design doc (v1 spec ->
# {TaskState -> Notion lane}), expressed against the actual TaskState values
# in tasks.py rather than inventing a parallel vocabulary. Overridable per
# board via BoardConfig.lane_map, because Callum's board has extra lanes.
DEFAULT_LANE_MAP: dict[TaskState, str] = {
    TaskState.CREATED: "To Do",
    TaskState.BRIEFED: "To Do",
    TaskState.RUNNING: "In Progress",
    TaskState.NEEDS_INPUT: "In Progress",
    TaskState.AWAITING_DEV: "In Progress",
    TaskState.VALIDATING: "In Review",
    TaskState.PARKED: "In Progress",
    TaskState.DONE: "Done",
    TaskState.FAILED: "Stopped",
}

# Needs you and Stuck both land in "In Progress plus a visible flag" per the
# design doc - the flag is what makes them findable on the board without a
# dedicated lane for each.
DEFAULT_FLAG_STATES: frozenset[TaskState] = frozenset({TaskState.NEEDS_INPUT, TaskState.PARKED})


@dataclass
class Ticket:
    """A board card, in Kestrel's terms rather than Notion's."""

    id: str
    handle: str
    title: str
    body: str
    lane: str
    url: str
    project: str | None = None
    flagged: bool = False
    pr_url: str | None = None
    last_edited_time: datetime | None = None


@runtime_checkable
class Board(Protocol):
    """What v1 needs, and no more. Async because the real implementation is a
    network call; the fake pays no such cost but keeps the same shape so the
    sync worker never branches on which it is talking to."""

    async def get(self, ticket_id: str) -> Ticket | None:
        """Fetch by Notion page id."""
        ...

    async def find_by_handle(self, handle: str) -> Ticket | None:
        """Fetch by the human handle (e.g. KES-31) - how intake resolves
        'pick up KES-31' to a ticket."""
        ...

    async def create(
        self,
        title: str,
        body: str = "",
        lane: str | None = None,
        project: str | None = None,
    ) -> Ticket:
        """Anything asked of Kestrel that is not already a ticket gets one
        created first."""
        ...

    async def set_lane(self, ticket_id: str, lane: str) -> None: ...

    async def set_flag(self, ticket_id: str, flagged: bool) -> None: ...

    async def set_pr_link(self, ticket_id: str, pr_url: str | None) -> None: ...

    async def add_comment(self, ticket_id: str, text: str) -> None: ...

    async def changed_since(self, since: datetime | None) -> list[Ticket]:
        """Tickets whose last_edited_time is after `since`. `None` means
        everything - used only for the very first poll."""
        ...


def _rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[:2000]}}] if text else []


def _plain_text(rich_text: list[dict[str, Any]]) -> str:
    return "".join(chunk.get("plain_text", "") for chunk in rich_text)


def _block_text(block: dict[str, Any]) -> str:
    kind = block.get("type")
    body = block.get(kind, {}) if kind else {}
    rich = body.get("rich_text")
    if rich is None:
        return ""
    text = _plain_text(rich)
    if kind in ("bulleted_list_item", "numbered_list_item"):
        return f"- {text}"
    return text


class NotionBoard:
    """The real thing, over Notion's REST API via httpx.

    Property names (status, flag, PR link, handle, project) are config, with
    sensible defaults, because they are Callum's naming choices on his board,
    not something this code should hardcode.
    """

    def __init__(
        self,
        token: str,
        database_id: str,
        *,
        api_version: str = NOTION_API_VERSION,
        property_title: str = "Name",
        property_body: str = "Description",
        property_status: str = "Status",
        property_flag: str = "Kestrel flag",
        property_pr: str = "PR",
        property_handle: str = "ID",
        property_project: str = "Project",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._database_id = database_id
        self._property_title = property_title
        self._property_body = property_body
        self._property_status = property_status
        self._property_flag = property_flag
        self._property_pr = property_pr
        self._property_handle = property_handle
        self._property_project = property_project
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": api_version,
            "Content-Type": "application/json",
        }
        self._client = client or httpx.AsyncClient(base_url=NOTION_BASE_URL, headers=headers)
        if client is not None:
            # A client can be injected (tests use `httpx.MockTransport` this
            # way), but the auth/version headers are still this class's
            # responsibility - they must not depend on how the client was
            # constructed.
            self._client.headers.update(headers)
        self._data_source_id: str | None = None
        self._status_property_type: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- schema resolution ---------------------------------------------------

    async def _resolve_data_source(self) -> str:
        """The database id names the container; querying and filtering happen
        against the data source underneath it. Resolved once and cached - it
        does not change for the lifetime of a process."""
        if self._data_source_id is not None:
            return self._data_source_id
        resp = await self._client.get(f"/v1/databases/{self._database_id}")
        resp.raise_for_status()
        data = resp.json()
        sources = data.get("data_sources") or []
        if not sources:
            raise RuntimeError(f"database {self._database_id} has no data sources")
        self._data_source_id = sources[0]["id"]
        properties = data.get("properties", {})
        status_prop = properties.get(self._property_status)
        # "status" and "select" render the same in the UI but take different
        # payload shapes on write. Detected once so callers never guess.
        self._status_property_type = status_prop.get("type", "select") if status_prop else "select"
        return self._data_source_id

    # -- reading --------------------------------------------------------------

    async def _read_body(self, page_id: str) -> str:
        texts: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = await self._client.get(f"/v1/blocks/{page_id}/children", params=params)
            resp.raise_for_status()
            data = resp.json()
            texts.extend(_block_text(b) for b in data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return "\n".join(t for t in texts if t)

    def _to_ticket(self, page: dict[str, Any], body: str) -> Ticket:
        props = page.get("properties", {})

        title_prop = props.get(self._property_title, {})
        title = _plain_text(title_prop.get("title", []))

        handle_prop = props.get(self._property_handle, {})
        handle = self._handle_from_property(handle_prop) or page["id"]

        status_prop = props.get(self._property_status, {})
        lane = ((status_prop.get("status") or status_prop.get("select") or {}) or {}).get(
            "name", ""
        )

        flag_prop = props.get(self._property_flag, {})
        flagged = bool(flag_prop.get("checkbox", False))

        pr_prop = props.get(self._property_pr, {})
        pr_url = pr_prop.get("url")

        project_prop = props.get(self._property_project, {})
        project = None
        if project_prop.get("select"):
            project = project_prop["select"]["name"]
        elif "rich_text" in project_prop:
            project = _plain_text(project_prop["rich_text"]) or None

        return Ticket(
            id=page["id"],
            handle=str(handle),
            title=title,
            body=body,
            lane=lane,
            url=page.get("url", ""),
            project=project,
            flagged=flagged,
            pr_url=pr_url,
            last_edited_time=datetime.fromisoformat(page["last_edited_time"]),
        )

    @staticmethod
    def _handle_from_property(prop: dict[str, Any]) -> str | None:
        kind = prop.get("type")
        if kind == "unique_id":
            uid = prop.get("unique_id") or {}
            prefix = uid.get("prefix")
            number = uid.get("number")
            if number is None:
                return None
            return f"{prefix}-{number}" if prefix else str(number)
        if kind == "rich_text":
            return _plain_text(prop.get("rich_text", [])) or None
        if kind == "title":
            return _plain_text(prop.get("title", [])) or None
        return None

    async def get(self, ticket_id: str) -> Ticket | None:
        resp = await self._client.get(f"/v1/pages/{ticket_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        page = resp.json()
        body = await self._read_body(ticket_id)
        return self._to_ticket(page, body)

    async def find_by_handle(self, handle: str) -> Ticket | None:
        data_source_id = await self._resolve_data_source()
        resp = await self._client.post(
            f"/v1/data_sources/{data_source_id}/query",
            json={
                "filter": {
                    "property": self._property_handle,
                    "rich_text": {"equals": handle},
                }
            },
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        page = results[0]
        body = await self._read_body(page["id"])
        return self._to_ticket(page, body)

    # -- writing ---------------------------------------------------------------

    async def create(
        self,
        title: str,
        body: str = "",
        lane: str | None = None,
        project: str | None = None,
    ) -> Ticket:
        data_source_id = await self._resolve_data_source()
        properties: dict[str, Any] = {
            self._property_title: {"title": _rich_text(title) or [{"text": {"content": title}}]}
        }
        if lane:
            properties[self._property_status] = self._status_payload(lane)
        if project:
            properties[self._property_project] = {"rich_text": _rich_text(project)}

        payload: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        if body:
            payload["children"] = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _rich_text(body)},
                }
            ]

        resp = await self._client.post("/v1/pages", json=payload)
        resp.raise_for_status()
        page = resp.json()
        return self._to_ticket(page, body)

    def _status_payload(self, lane: str) -> dict[str, Any]:
        kind = self._status_property_type or "select"
        return {kind: {"name": lane}}

    async def set_lane(self, ticket_id: str, lane: str) -> None:
        await self._resolve_data_source()  # ensures the property type is known
        resp = await self._client.patch(
            f"/v1/pages/{ticket_id}",
            json={"properties": {self._property_status: self._status_payload(lane)}},
        )
        resp.raise_for_status()

    async def set_flag(self, ticket_id: str, flagged: bool) -> None:
        resp = await self._client.patch(
            f"/v1/pages/{ticket_id}",
            json={"properties": {self._property_flag: {"checkbox": flagged}}},
        )
        resp.raise_for_status()

    async def set_pr_link(self, ticket_id: str, pr_url: str | None) -> None:
        resp = await self._client.patch(
            f"/v1/pages/{ticket_id}",
            json={"properties": {self._property_pr: {"url": pr_url}}},
        )
        resp.raise_for_status()

    async def add_comment(self, ticket_id: str, text: str) -> None:
        resp = await self._client.post(
            "/v1/comments",
            json={"parent": {"page_id": ticket_id}, "rich_text": _rich_text(text)},
        )
        resp.raise_for_status()

    async def changed_since(self, since: datetime | None) -> list[Ticket]:
        data_source_id = await self._resolve_data_source()
        filter_: dict[str, Any] | None = None
        if since is not None:
            filter_ = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"after": since.astimezone(UTC).isoformat()},
            }
        body: dict[str, Any] = {
            "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}]
        }
        if filter_:
            body["filter"] = filter_

        tickets: list[Ticket] = []
        cursor: str | None = None
        while True:
            request_body = dict(body)
            if cursor:
                request_body["start_cursor"] = cursor
            resp = await self._client.post(
                f"/v1/data_sources/{data_source_id}/query", json=request_body
            )
            resp.raise_for_status()
            data = resp.json()
            for page in data.get("results", []):
                page_body = await self._read_body(page["id"])
                tickets.append(self._to_ticket(page, page_body))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return tickets


@dataclass
class FakeBoard:
    """In-memory stand-in with the same interface. Used by tests and by dev
    mode when no Notion token is configured - a visible log line, not a silent
    stand-in, is what tells you it's in use (see `board_sync.py` / runtime
    wiring)."""

    tickets: dict[str, Ticket] = field(default_factory=dict)
    _next_id: int = 0

    def _new_id(self) -> str:
        self._next_id += 1
        return f"fake-{self._next_id}"

    async def get(self, ticket_id: str) -> Ticket | None:
        return self.tickets.get(ticket_id)

    async def find_by_handle(self, handle: str) -> Ticket | None:
        for t in self.tickets.values():
            if t.handle == handle:
                return t
        return None

    async def create(
        self,
        title: str,
        body: str = "",
        lane: str | None = None,
        project: str | None = None,
    ) -> Ticket:
        ticket_id = self._new_id()
        ticket = Ticket(
            id=ticket_id,
            handle=ticket_id,
            title=title,
            body=body,
            lane=lane or "To Do",
            url=f"https://notion.so/{ticket_id}",
            project=project,
            last_edited_time=datetime.now(UTC),
        )
        self.tickets[ticket_id] = ticket
        return ticket

    def _replace(self, ticket_id: str, **changes: Any) -> None:
        self.tickets[ticket_id] = replace(
            self.tickets[ticket_id], last_edited_time=datetime.now(UTC), **changes
        )

    async def set_lane(self, ticket_id: str, lane: str) -> None:
        self._replace(ticket_id, lane=lane)

    async def set_flag(self, ticket_id: str, flagged: bool) -> None:
        self._replace(ticket_id, flagged=flagged)

    async def set_pr_link(self, ticket_id: str, pr_url: str | None) -> None:
        self._replace(ticket_id, pr_url=pr_url)

    async def add_comment(self, ticket_id: str, text: str) -> None:
        # Fidelity here isn't needed for v1 tests; existence of the ticket is
        # still enforced so a bad id fails the same way it would for real.
        if ticket_id not in self.tickets:
            raise KeyError(ticket_id)

    async def changed_since(self, since: datetime | None) -> list[Ticket]:
        if since is None:
            return list(self.tickets.values())
        return [
            t for t in self.tickets.values() if t.last_edited_time and t.last_edited_time > since
        ]


def build_board(config: Any) -> Board:
    """`config` is a `kestrel.config.BoardConfig` - typed loosely to dodge the
    import cycle (config.py needs NOTION_API_VERSION from this module).

    No token configured is not an error: dev mode runs against `FakeBoard`
    instead. That is a visible failure, not a silent one - it always logs at
    warning level, because a board that quietly isn't Notion is exactly the
    kind of wrongness the design doc says must be loud.
    """
    if config.configured:
        return NotionBoard(
            token=config.token,
            database_id=config.database_id,
            api_version=config.api_version,
            property_title=config.property_title,
            property_body=config.property_body,
            property_status=config.property_status,
            property_flag=config.property_flag,
            property_pr=config.property_pr,
            property_handle=config.property_handle,
            property_project=config.property_project,
        )
    import logging

    logging.getLogger("kestrel.board").warning(
        "no Notion token/database configured (KESTREL_NOTION_TOKEN / "
        "KESTREL_NOTION_DATABASE_ID) - using an in-memory fake board. "
        "Nothing written here reaches Notion."
    )
    return FakeBoard()
