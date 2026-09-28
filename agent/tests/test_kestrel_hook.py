"""`kestrel-hook` - the process Claude Code spawns on every hook firing.

Runs against a tiny in-process HTTP server rather than a real Kestrel server:
what needs proving here is the *shape* of what gets posted and when
`/sessions/bind` is called, not `/hooks/claude`'s own behaviour (that belongs
to test_api.py).
"""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from kestrel_agent import kestrel_hook


class _Recorder(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict, dict]]] = []  # path, headers, json body

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Recorder.requests.append((self.path, dict(self.headers), json.loads(body or b"{}")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # silence the default stderr logging
        pass


@pytest.fixture
def server():
    _Recorder.requests = []
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    # `serve_forever`'s default 0.5s poll_interval is also how long
    # `httpd.shutdown()` can take to notice - a much shorter one makes
    # teardown near-instant instead of a flat 0.5s per test.
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _run_hook(monkeypatch, server, hook_payload, task_id=None, data_dir=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook_payload)))
    monkeypatch.setenv("KESTREL_SERVER_URL", server)
    if task_id is not None:
        monkeypatch.setenv("KESTREL_TASK_ID", task_id)
    else:
        monkeypatch.delenv("KESTREL_TASK_ID", raising=False)
    if data_dir is not None:
        monkeypatch.setenv("KESTREL_DATA", str(data_dir))
    return kestrel_hook.main()


def test_forwards_the_hook_payload_to_hooks_claude(monkeypatch, server):
    exit_code = _run_hook(
        monkeypatch, server, {"hook_event_name": "PreToolUse", "session_id": "sess-1"}
    )
    assert exit_code == 0
    [request] = _Recorder.requests
    path, _headers, body = request
    assert path == "/hooks/claude"
    assert body == {"hook_event_name": "PreToolUse", "session_id": "sess-1"}


def test_session_start_binds_before_forwarding_the_hook(monkeypatch, server):
    _run_hook(
        monkeypatch,
        server,
        {"hook_event_name": "SessionStart", "session_id": "sess-1"},
        task_id="KES-1",
    )
    paths = [r[0] for r in _Recorder.requests]
    assert paths == ["/sessions/bind", "/hooks/claude"]
    bind_body = _Recorder.requests[0][2]
    assert bind_body == {"session_id": "sess-1", "task_handle": "KES-1"}


def test_non_session_start_events_never_bind(monkeypatch, server):
    _run_hook(
        monkeypatch,
        server,
        {"hook_event_name": "Stop", "session_id": "sess-1"},
        task_id="KES-1",
    )
    paths = [r[0] for r in _Recorder.requests]
    assert paths == ["/hooks/claude"]


def test_without_a_task_id_it_still_forwards_but_never_binds(monkeypatch, server):
    _run_hook(monkeypatch, server, {"hook_event_name": "SessionStart", "session_id": "sess-1"})
    paths = [r[0] for r in _Recorder.requests]
    assert paths == ["/hooks/claude"]


def test_sends_the_token_as_a_bearer_header_when_one_exists(monkeypatch, server, tmp_path):
    (tmp_path / "token").write_text("s3cr3t")
    _run_hook(
        monkeypatch,
        server,
        {"hook_event_name": "Notification", "session_id": "sess-1"},
        data_dir=tmp_path,
    )
    _path, headers, _body = _Recorder.requests[0]
    assert headers["Authorization"] == "Bearer s3cr3t"


def test_a_malformed_payload_exits_zero_without_posting_anything(monkeypatch, server):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    monkeypatch.setenv("KESTREL_SERVER_URL", server)
    exit_code = kestrel_hook.main()
    assert exit_code == 0
    assert _Recorder.requests == []


def test_an_unreachable_server_exits_zero_rather_than_failing_the_tool_call(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"})))
    monkeypatch.setenv("KESTREL_SERVER_URL", "http://127.0.0.1:1")  # nothing listens here
    assert kestrel_hook.main() == 0
