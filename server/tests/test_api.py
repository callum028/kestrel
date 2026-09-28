"""The HTTP surface, including the hook path that makes supervision real.

The tick loop is not started here - create_app's lifespan owns it, and TestClient
is used without entering it so ticks stay explicit and time stays controllable.
"""

import time

import pytest
from fastapi.testclient import TestClient

from kestrel.api import create_app
from kestrel.config import Config
from kestrel.runtime import Runtime


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )


@pytest.fixture
def client(config):
    app = create_app(runtime=Runtime.build(config))
    return TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"})


@pytest.fixture
def anonymous(config):
    """No credentials - stands in for anything that finds the port."""
    return TestClient(create_app(runtime=Runtime.build(config)))


def make_task(client, handle="KES-31"):
    return client.post(
        "/tasks",
        json={
            "handle": handle,
            "goal": "Handle token refresh failure",
            "criteria": ["npm test passes"],
        },
    ).json()


def test_health_reports_something_useful(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["active_tasks"] == 0
    assert body["dev_lock"] is None


def test_creating_the_same_handle_twice_is_refused_not_duplicated(client):
    assert make_task(client)["status"] == "ok"
    second = make_task(client)
    assert second["status"] == "refused"
    assert "already exists" in second["reason"]


def test_an_unattributed_hook_is_recorded_rather_than_dropped(client):
    body = client.post(
        "/hooks/claude",
        json={"hook_event_name": "PreToolUse", "session_id": "unknown", "tool_name": "Bash"},
    ).json()

    assert body["status"] == "not_found"
    kinds = [e["kind"] for e in client.get("/events").json()]
    assert "system.hook_unattributed" in kinds


def test_bound_session_hooks_become_tool_call_events(client):
    make_task(client)
    bound = client.post(
        "/sessions/bind", json={"session_id": "sess-1", "task_handle": "KES-31"}
    ).json()
    assert bound["status"] == "ok"

    client.post(
        "/hooks/claude",
        json={
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "gh run watch"},
        },
    )

    events = client.get("/events").json()
    tool_calls = [e for e in events if e["kind"] == "session.tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["payload"] == {"tool": "Bash", "args": "gh run watch"}


def test_binding_an_unknown_task_says_so(client):
    body = client.post(
        "/sessions/bind", json={"session_id": "sess-1", "task_handle": "KES-999"}
    ).json()
    assert body["status"] == "not_found"
    assert body["searched_for"] == "KES-999"


def test_stop_is_recorded_as_a_claim_not_a_completion(client):
    make_task(client)
    client.post("/sessions/bind", json={"session_id": "sess-1", "task_handle": "KES-31"})
    client.post("/hooks/claude", json={"hook_event_name": "Stop", "session_id": "sess-1"})

    closed = [e for e in client.get("/events").json() if e["kind"] == "task.closed"]
    assert closed[0]["payload"] == {"claimed": "done"}

    # and the task is emphatically not done
    assert client.get("/tasks").json()[0]["state"] == "created"


def test_the_polling_loop_is_caught_through_the_hook_path(client):
    make_task(client)
    client.post("/sessions/bind", json={"session_id": "sess-1", "task_handle": "KES-31"})
    client.post("/tasks/KES-31/state/briefed")
    client.post("/tasks/KES-31/state/running")

    for _ in range(40):
        client.post(
            "/hooks/claude",
            json={
                "hook_event_name": "PreToolUse",
                "session_id": "sess-1",
                "tool_name": "Bash",
                "tool_input": {"command": "gh run watch"},
            },
        )

    report = client.post("/tick").json()
    assert report["quiet"] is False
    assert report["parked"] == ["KES-31"]

    # No executor is registered in this app, and the message must say *that*
    # rather than blaming the agent for a stall Kestrel could not act on.
    delivered = client.get("/deliveries").json()
    assert "needs a claude_code executor and none is registered" in delivered[0]["body"]
    assert "stalled" not in delivered[0]["body"]


def test_illegal_transitions_are_refused_with_a_reason(client):
    make_task(client)
    body = client.post("/tasks/KES-31/state/done").json()
    assert body["status"] == "refused"
    assert "created -> done" in body["reason"]


def test_signals_drive_presence_and_the_state_block(client):
    client.post(
        "/clients/signals",
        json={"app_open": True, "app_focused": True, "task_handle": "KES-31", "pane": "diff"},
    )
    state = client.get("/state").json()
    assert state["presence"] == "at_desk"
    assert state["focus"] == "KES-31 / diff"
    assert "now:" in state["block"]


def test_speech_is_suppressed_during_a_meeting(client):
    client.post("/clients/signals", json={"app_open": True, "calendar_busy": True})
    assert client.get("/state").json()["speech_suppressed"] is True


def test_acknowledging_an_unknown_delivery_says_so(client):
    body = client.post("/deliveries/nope/ack", json={"on": "desktop"}).json()
    assert body["status"] == "not_found"


# --- conversation -------------------------------------------------------
# Replies are generated in the background (see kestrel.conversation) - the
# TestClient's portal keeps running its event loop on its own thread, so a
# short poll on /conversation/status (backed by real wall-clock sleeps, not
# anything that depends on this thread's loop) is enough to wait for one out.


def wait_for_reply(client, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while client.get("/conversation/status").json()["thinking"]:
        if time.monotonic() > deadline:
            raise TimeoutError("brain reply did not land in time")
        time.sleep(0.01)


def test_posting_a_message_stores_it_and_returns_it(client):
    body = client.post("/conversation/messages", json={"text": "what's running?"}).json()
    assert body["status"] == "ok"
    assert body["role"] == "user"
    assert body["text"] == "what's running?"


def test_listing_messages_includes_the_stub_reply(client):
    client.post("/conversation/messages", json={"text": "what's running?"})
    wait_for_reply(client)
    messages = client.get("/conversation/messages").json()
    assert [m["role"] for m in messages] == ["user", "kestrel"]
    assert "brain isn't connected" in messages[1]["text"]


def test_listing_messages_after_a_cursor_only_returns_the_newer_ones(client):
    client.post("/conversation/messages", json={"text": "first"})
    wait_for_reply(client)
    cursor = client.get("/conversation/messages").json()[-1]["id"]
    client.post("/conversation/messages", json={"text": "second"})
    wait_for_reply(client)
    fresh = client.get(f"/conversation/messages?after={cursor}").json()
    assert fresh[0]["text"] == "second"
    assert [m["role"] for m in fresh] == ["user", "kestrel"]


def test_conversation_status_reflects_the_background_reply(client):
    client.post("/conversation/messages", json={"text": "hi"})
    wait_for_reply(client)
    assert client.get("/conversation/status").json() == {"thinking": False}


# --- web push -------------------------------------------------------------


def test_the_vapid_public_key_is_reachable(client):
    body = client.get("/push/vapid-public-key").json()
    assert isinstance(body["public_key"], str) and body["public_key"]


def test_subscribing_and_unsubscribing(client):
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "p", "auth": "a"}}
    assert client.post("/push/subscriptions", json=sub).json()["status"] == "ok"

    removed = client.request("DELETE", "/push/subscriptions", json={"endpoint": sub["endpoint"]})
    assert removed.json()["status"] == "ok"

    missing = client.request("DELETE", "/push/subscriptions", json={"endpoint": sub["endpoint"]})
    assert missing.json()["status"] == "not_found"


# --- authentication ---------------------------------------------------------
# POST /terminals spawns a process with Callum's keys and repos, so "who can
# reach the API" is the whole security model.


def test_nothing_is_readable_without_a_token(anonymous):
    for path in ("/health", "/state", "/tasks", "/events", "/deliveries"):
        assert anonymous.get(path).status_code == 401, path


def test_a_wrong_token_is_refused(anonymous):
    response = anonymous.get("/health", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401
    assert response.json()["status"] == "refused"


def test_spawning_a_process_without_a_token_is_refused(anonymous, tmp_path):
    response = anonymous.post("/terminals", json={"cwd": str(tmp_path)})
    assert response.status_code == 401


def test_a_web_page_cannot_use_the_port_even_with_the_token(client):
    """Loopback is not a boundary for a browser: any page you have open can
    reach localhost. Origin is what separates a client from a tab."""
    response = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert "origin not allowed" in response.json()["reason"]


def test_the_real_client_origin_is_allowed(client):
    assert client.get("/health", headers={"Origin": "http://localhost:5173"}).status_code == 200


def test_a_non_browser_caller_sends_no_origin_and_is_fine(client):
    assert client.get("/health").status_code == 200
