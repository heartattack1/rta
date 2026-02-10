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


def test_callback_sent_on_finish(monkeypatch):
    import app as tooler_app

    received: dict[str, object] = {}

    def fake_send_callback(run):
        if run.callback_url:
            received.update(
                {
                    "tool_run_id": run.id,
                    "status": run.status,
                    "artifacts": run.artifacts,
                }
            )
            run.callback_sent = True

    monkeypatch.setattr(tooler_app, "_send_callback", fake_send_callback)

    client = tooler_app.app.test_client()

    create_response = client.post(
        "/tool-runs",
        json={
            "tool_name": "dummy",
            "input": {"sleep_seconds": 0.1},
            "callback_url": "http://callback.local/finished",
        },
    )
    run_id = create_response.get_json()["tool_run_id"]

    for _ in range(30):
        payload = client.get(f"/tool-runs/{run_id}").get_json()
        if payload["status"] in {"SUCCEEDED", "FAILED"} and received:
            break
        time.sleep(0.1)

    assert received
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

def test_git_autocommit_creates_branch_and_commit(tmp_path, monkeypatch):
    import subprocess

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "tooler@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Tooler Test"],
        check=True,
        capture_output=True,
        text=True,
    )

    (repo_dir / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )

    (repo_dir / "README.md").write_text("changed\n", encoding="utf-8")

    monkeypatch.setenv("GIT_PUSH", "false")
    client = app.test_client()

    create_response = client.post(
        "/tool-runs",
        json={
            "tool_name": "git-autocommit",
            "input": {
                "workdir": str(repo_dir),
                "subject": "feat: autobot update",
            },
        },
    )

    assert create_response.status_code == 201
    run_id = create_response.get_json()["tool_run_id"]

    final = None
    for _ in range(40):
        payload = client.get(f"/tool-runs/{run_id}").get_json()
        if payload["status"] in {"SUCCEEDED", "FAILED"}:
            final = payload
            break
        time.sleep(0.1)

    assert final is not None
    assert final["status"] == "SUCCEEDED"
    assert final["branch"]
    assert final["branch"].startswith("autobot/")
    assert final["commit_hash"]
    assert any(a.startswith("branch:") for a in final["artifacts"])
    assert any(a.startswith("commit_hash:") for a in final["artifacts"])

    current_branch = subprocess.run(
        ["git", "-C", str(repo_dir), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == final["branch"]


def test_git_autocommit_requires_git_repo(tmp_path):
    non_repo = tmp_path / "non_repo"
    non_repo.mkdir()

    client = app.test_client()

    create_response = client.post(
        "/tool-runs",
        json={
            "tool_name": "git-autocommit",
            "input": {"workdir": str(non_repo), "subject": "feat: autobot update"},
        },
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
    assert "not a git repository" in final["stderr_tail"]
