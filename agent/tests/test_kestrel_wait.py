"""`kestrel-wait` - how a task registers a wait instead of polling for it.

Same style as test_kestrel_hook.py: a tiny in-process HTTP server records what
was posted, rather than a real Kestrel server - `/waits`'s own behaviour
belongs to test_api.py.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from kestrel_agent import kestrel_wait


class _Recorder(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict, dict]]] = []  # path, headers, json body
    response: ClassVar[dict] = {"status": "ok"}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Recorder.requests.append((self.path, dict(self.headers), json.loads(body or b"{}")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(_Recorder.response).encode())

    def log_message(self, *args):  # silence the default stderr logging
        pass


@pytest.fixture
def server():
    _Recorder.requests = []
    _Recorder.response = {"status": "ok"}
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


def _run(monkeypatch, server, argv, task_id="KES-1", data_dir=None):
    monkeypatch.setenv("KESTREL_SERVER_URL", server)
    if task_id is not None:
        monkeypatch.setenv("KESTREL_TASK_ID", task_id)
    else:
        monkeypatch.delenv("KESTREL_TASK_ID", raising=False)
    if data_dir is not None:
        monkeypatch.setenv("KESTREL_DATA", str(data_dir))
    return kestrel_wait.main(argv)


def test_ci_wait_posts_the_branch_and_task_handle(monkeypatch, server):
    exit_code = _run(monkeypatch, server, ["ci", "--branch", "kestrel/KES-1", "--timeout", "30"])
    assert exit_code == 0
    [request] = _Recorder.requests
    path, _headers, body = request
    assert path == "/waits"
    assert body == {
        "task_handle": "KES-1",
        "kind": "ci",
        "params": {"branch": "kestrel/KES-1"},
        "timeout_minutes": 30.0,
    }


def test_url_wait_posts_the_url_and_expected_status(monkeypatch, server):
    _run(monkeypatch, server, ["url", "https://dev.example/health", "--expect", "204"])
    [(_path, _headers, body)] = _Recorder.requests
    assert body["kind"] == "url"
    assert body["params"] == {"url": "https://dev.example/health", "expect": 204}


def test_deadline_wait_posts_the_reason_and_uses_minutes_as_the_timeout(monkeypatch, server):
    _run(monkeypatch, server, ["deadline", "--minutes", "15", "--reason", "check back later"])
    [(_path, _headers, body)] = _Recorder.requests
    assert body["kind"] == "deadline"
    assert body["params"] == {"reason": "check back later"}
    assert body["timeout_minutes"] == 15.0


def test_without_a_task_id_it_refuses_and_posts_nothing(monkeypatch, server):
    exit_code = _run(monkeypatch, server, ["ci", "--branch", "x"], task_id=None)
    assert exit_code == 1
    assert _Recorder.requests == []


def test_sends_the_token_as_a_bearer_header_when_one_exists(monkeypatch, server, tmp_path):
    (tmp_path / "token").write_text("s3cr3t")
    _run(monkeypatch, server, ["ci", "--branch", "x"], data_dir=tmp_path)
    _path, headers, _body = _Recorder.requests[0]
    assert headers["Authorization"] == "Bearer s3cr3t"


def test_a_refusal_from_the_server_exits_non_zero(monkeypatch, server):
    _Recorder.response = {"status": "not_found", "searched_for": "KES-1"}
    exit_code = _run(monkeypatch, server, ["ci", "--branch", "x"])
    assert exit_code == 1


def test_an_unreachable_server_exits_non_zero(monkeypatch):
    monkeypatch.setenv("KESTREL_SERVER_URL", "http://127.0.0.1:1")  # nothing listens here
    monkeypatch.setenv("KESTREL_TASK_ID", "KES-1")
    assert kestrel_wait.main(["ci", "--branch", "x"]) == 1
