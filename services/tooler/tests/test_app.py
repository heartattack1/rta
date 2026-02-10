import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app


def test_dummy_tool_run_lifecycle():
    client = app.test_client()

    create_response = client.post(
        "/tool-runs",
        json={
            "tool_name": "dummy",
            "input": {"message": "hello", "sleep_seconds": 0.2},
        },
    )

    assert create_response.status_code == 201
    created = create_response.get_json()
    assert created["tool_run_id"]
    assert created["status"] in {"QUEUED", "RUNNING"}

    run_id = created["tool_run_id"]

    final = None
    for _ in range(30):
        get_response = client.get(f"/tool-runs/{run_id}")
        assert get_response.status_code == 200
        payload = get_response.get_json()
        if payload["status"] in {"SUCCEEDED", "FAILED"}:
            final = payload
            break
        time.sleep(0.1)

    assert final is not None
    assert final["status"] == "SUCCEEDED"
    assert "start: hello" in final["stdout_tail"]
    assert "done" in final["stdout_tail"]
    assert final["stderr_tail"] == ""
    assert len(final["artifacts"]) == 2


def test_unknown_tool_rejected():
    client = app.test_client()

    response = client.post("/tool-runs", json={"tool_name": "rm-rf"})

    assert response.status_code == 400
    payload = response.get_json()
    assert "not allowed" in payload["message"]


def test_callback_sent_on_finish():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            received.update(json.loads(body.decode("utf-8")))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args, **kwargs):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client = app.test_client()
    callback_url = f"http://127.0.0.1:{server.server_port}/finished"

    create_response = client.post(
        "/tool-runs",
        json={
            "tool_name": "dummy",
            "input": {"sleep_seconds": 0.1},
            "callback_url": callback_url,
        },
    )
    run_id = create_response.get_json()["tool_run_id"]

    for _ in range(30):
        payload = client.get(f"/tool-runs/{run_id}").get_json()
        if payload["status"] in {"SUCCEEDED", "FAILED"} and received:
            break
        time.sleep(0.1)

    server.shutdown()

    assert received["tool_run_id"] == run_id
    assert received["status"] == "SUCCEEDED"
    assert isinstance(received["artifacts"], list)


def test_codex_tool_without_key_fails_gracefully(monkeypatch):
    import app as tooler_app

    monkeypatch.setattr(tooler_app, "CODEX_API_KEY", "")
    client = tooler_app.app.test_client()

    create_response = client.post(
        "/tool-runs",
        json={"tool_name": "codex", "input": {"prompt": "summarize this"}},
    )

    assert create_response.status_code == 201
    run_id = create_response.get_json()["tool_run_id"]

    final = None
    for _ in range(20):
        payload = client.get(f"/tool-runs/{run_id}").get_json()
        if payload["status"] in {"SUCCEEDED", "FAILED"}:
            final = payload
            break
        time.sleep(0.05)

    assert final is not None
    assert final["status"] == "FAILED"
    assert final["exit_code"] == -1
    assert "not configured" in final["stderr_tail"]


def test_codex_tool_requires_prompt(monkeypatch):
    import app as tooler_app

    monkeypatch.setattr(tooler_app, "CODEX_API_KEY", "test-key")
    client = tooler_app.app.test_client()

    response = client.post("/tool-runs", json={"tool_name": "codex", "input": {}})

    assert response.status_code == 400
    payload = response.get_json()
    assert "input.prompt" in payload["message"]
