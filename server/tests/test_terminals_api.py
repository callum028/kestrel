"""Terminals over HTTP and WebSocket - the surface the app actually talks to."""

import pytest
from fastapi.testclient import TestClient

from kestrel.api import _TextStreamer, create_app
from kestrel.config import Config
from kestrel.runtime import Runtime


def test_a_multibyte_character_split_across_chunks_is_not_corrupted():
    """The bug the naive per-chunk `decode(..., errors="replace")` has: a PTY
    read can end mid-character, and Claude Code's UI is full of multibyte
    spinner and box-drawing glyphs that would otherwise render as mangled
    replacement characters at every chunk boundary."""
    streamer = _TextStreamer()
    text = "spinner: ⠋⠙⠹ done — 100%"
    encoded = text.encode("utf-8")

    # Split inside the encoded form of a multibyte character (the first
    # braille glyph is three bytes), not at a character boundary.
    split_at = encoded.index("⠋".encode()) + 1
    first, second = encoded[:split_at], encoded[split_at:]
    assert 0 < split_at < len(encoded)

    assembled = streamer.feed(first) + streamer.feed(second)
    assert assembled == text


@pytest.fixture
def client(tmp_path, session_host):
    config = Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )
    rt = Runtime.build(config)
    app = create_app(runtime=rt)
    with TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"}) as c:
        c.token = app.state.token  # type: ignore[attr-defined]
        yield c
    # No explicit close_all here: every terminal is a real process under the
    # session_host subprocess, and that fixture's own teardown (SIGTERM to the
    # host) makes it close everything it holds - the same graceful shutdown a
    # real deploy relies on.


def ws_url(client, terminal_id: str) -> str:
    """The browser WebSocket API cannot set headers, so the token rides in the
    query string - the one place it does."""
    return f"/terminals/{terminal_id}/ws?token={client.token}"


def read_until(ws, needle: str, limit: int = 60) -> str:
    """A PTY delivers in arbitrary chunks; asserting on one frame is flaky."""
    buffer = ""
    for _ in range(limit):
        message = ws.receive()
        if message.get("type") == "websocket.close":
            break
        text = message.get("text") or ""
        buffer += text
        if needle in buffer:
            break
    return buffer


def test_opening_a_terminal_without_a_session_host_running_says_so(tmp_path):
    """No host listening must look like the outage it is, never like "zero
    terminals" - a silent empty list here would be exactly the kind of
    invisible failure the project rules out."""
    config = Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )
    rt = Runtime.build(config)
    app = create_app(runtime=rt)
    with TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"}) as c:
        body = c.post("/terminals", json={"cwd": str(tmp_path)}).json()
        assert body["status"] == "unavailable"
        assert "session host" in body["reason"]

        body = c.get("/terminals").json()
        assert body["status"] == "unavailable"


def test_opening_a_terminal_in_a_missing_directory_says_so(client, tmp_path):
    body = client.post("/terminals", json={"cwd": str(tmp_path / "nope")}).json()
    assert body["status"] == "not_found"
    assert body["looked_in"] == "filesystem"


def test_opening_against_an_unknown_task_says_so(client, tmp_path):
    body = client.post("/terminals", json={"cwd": str(tmp_path), "task_handle": "KES-999"}).json()
    assert body["status"] == "not_found"
    assert body["looked_in"] == "tasks"


def test_a_free_terminal_echoes_what_it_is_sent(client, tmp_path):
    opened = client.post(
        "/terminals", json={"cwd": str(tmp_path), "command": ["/bin/bash", "--norc", "-i"]}
    ).json()
    assert opened["status"] == "ok"

    with client.websocket_connect(ws_url(client, opened["id"])) as ws:
        ws.send_json({"type": "input", "data": "echo over-the-socket\n"})
        assert "over-the-socket" in read_until(ws, "over-the-socket")


def test_resize_travels_over_the_socket(client, tmp_path):
    opened = client.post(
        "/terminals", json={"cwd": str(tmp_path), "command": ["/bin/bash", "--norc", "-i"]}
    ).json()

    with client.websocket_connect(ws_url(client, opened["id"])) as ws:
        ws.send_json({"type": "resize", "rows": 50, "cols": 132})
        ws.send_json({"type": "input", "data": "tput cols\n"})
        assert "132" in read_until(ws, "132")


def test_a_terminal_bound_to_a_task_is_listed_against_it(client, tmp_path):
    client.post("/tasks", json={"handle": "KES-31", "goal": "auth", "criteria": []})
    opened = client.post(
        "/terminals",
        json={
            "cwd": str(tmp_path),
            "task_handle": "KES-31",
            "command": ["/bin/bash", "--norc", "-i"],
        },
    ).json()

    listed = client.get("/terminals").json()
    assert [t["id"] for t in listed] == [opened["id"]]
    assert listed[0]["task_id"] == opened["task_id"]
    assert listed[0]["alive"] is True


def test_the_session_survives_the_client_leaving(client, tmp_path):
    """Closing the app must not kill the work - and reattaching must not be blind."""
    opened = client.post(
        "/terminals", json={"cwd": str(tmp_path), "command": ["/bin/bash", "--norc", "-i"]}
    ).json()

    with client.websocket_connect(ws_url(client, opened["id"])) as ws:
        ws.send_json({"type": "input", "data": "echo before-disconnect\n"})
        read_until(ws, "before-disconnect")

    assert client.get("/terminals").json()[0]["alive"] is True

    with client.websocket_connect(ws_url(client, opened["id"])) as ws:
        replayed = ws.receive_text()
        assert "before-disconnect" in replayed


def test_connecting_to_a_terminal_that_does_not_exist_is_refused(client):
    # starlette raises on the 4404 close rather than yielding a socket
    with pytest.raises(Exception), client.websocket_connect(ws_url(client, "nope")) as ws:  # noqa: B017
        ws.receive_text()


def test_closing_a_terminal_twice_reports_the_second_honestly(client, tmp_path):
    opened = client.post(
        "/terminals", json={"cwd": str(tmp_path), "command": ["/bin/bash", "--norc", "-i"]}
    ).json()
    assert client.delete(f"/terminals/{opened['id']}").json()["status"] == "ok"
    assert client.delete(f"/terminals/{opened['id']}").json()["status"] == "not_found"


def test_a_socket_without_a_token_carries_no_keystrokes(client, tmp_path):
    """Middleware does not run for websockets, and this is the endpoint that
    carries keystrokes - so it checks the token itself or nothing does."""
    opened = client.post(
        "/terminals", json={"cwd": str(tmp_path), "command": ["/bin/bash", "--norc", "-i"]}
    ).json()

    # starlette raises on the 4401 close rather than yielding a socket
    for url in (
        f"/terminals/{opened['id']}/ws",
        f"/terminals/{opened['id']}/ws?token=wrong",
    ):
        with pytest.raises(Exception), client.websocket_connect(url) as ws:  # noqa: B017
            ws.receive_text()
