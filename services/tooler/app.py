import json
import os
import pwd
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request

app = Flask(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "service")
ARTIFACTS_DIR = Path(os.getenv("TOOLER_ARTIFACTS_DIR", "/tmp/tooler-artifacts"))
RUN_AS_USER = os.getenv("TOOLER_RUN_AS_USER", "").strip()
TAIL_LINES = int(os.getenv("TOOLER_TAIL_LINES", "40"))
CODEX_HOME = Path(os.getenv("CODEX_HOME", "/codex-home")).expanduser()
TOOLER_CODEX_MODE = os.getenv("TOOLER_CODEX_MODE", "readonly").strip().lower() or "readonly"
TOOLER_CODEX_MODEL = os.getenv("TOOLER_CODEX_MODEL", "").strip()
TOOLER_CODEX_MOCK = os.getenv("TOOLER_CODEX_MOCK", "").strip().lower() in {"1", "true", "yes"}


@dataclass
class ToolRun:
    id: str
    tool_name: str
    status: str
    stdout_path: Path
    stderr_path: Path
    artifacts: list[str]
    callback_url: str | None = None
    pid: int | None = None
    process: subprocess.Popen | None = None
    exit_code: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    callback_sent: bool = False
    startup_error: str | None = None
    branch: str | None = None
    commit_hash: str | None = None


@dataclass(frozen=True)
class ToolAdapterResult:
    command: list[str] | None = None
    startup_error: str | None = None


def _build_git_autocommit_adapter(payload: dict[str, Any]) -> ToolAdapterResult:
    workdir_raw = str(payload.get("workdir", os.getcwd())).strip()
    if not workdir_raw:
        abort(400, description="Field 'input.workdir' must be a non-empty string")

    workdir = Path(workdir_raw).expanduser().resolve()
    if not workdir.exists() or not workdir.is_dir():
        abort(400, description="Field 'input.workdir' must point to an existing directory")

    if not (workdir / ".git").exists():
        return ToolAdapterResult(startup_error=f"Directory '{workdir}' is not a git repository")

    subject = str(payload.get("subject", "chore: autobot update")).strip()
    if not subject:
        abort(400, description="Field 'input.subject' must be a non-empty string")

    today = datetime.now().strftime("%Y-%m-%d")
    branch = f"autobot/{today}"
    push_enabled = os.getenv("GIT_PUSH", "false").strip().lower() in {"1", "true", "yes"}

    script = """
import subprocess
import sys

workdir, branch, subject, push_enabled = sys.argv[1:5]

def run(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", workdir, *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()

run("checkout", "-B", branch)
run("add", "-A")

has_staged_changes = subprocess.run(
    ["git", "-C", workdir, "diff", "--cached", "--quiet"],
    check=False,
).returncode != 0

if has_staged_changes:
    run("commit", "-m", subject)

commit_hash = run("rev-parse", "HEAD")
print(f"__BRANCH__={branch}")
print(f"__COMMIT_HASH__={commit_hash}")

if push_enabled == "1":
    run("push", "origin", branch)
""".strip()

    return ToolAdapterResult(
        command=[
            "python",
            "-c",
            script,
            str(workdir),
            branch,
            subject,
            "1" if push_enabled else "0",
        ]
    )


def _build_dummy_adapter(payload: dict[str, Any]) -> ToolAdapterResult:
    return ToolAdapterResult(command=_build_dummy_command(payload))


def _build_codex_adapter(payload: dict[str, Any]) -> ToolAdapterResult:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        abort(400, description="Field 'input.prompt' is required for tool 'codex'")

    workdir_raw = str(payload.get("workdir", os.getcwd())).strip()
    if not workdir_raw:
        abort(400, description="Field 'input.workdir' must be a non-empty string")

    workdir = Path(workdir_raw).expanduser().resolve()
    if not workdir.exists() or not workdir.is_dir():
        abort(400, description="Field 'input.workdir' must point to an existing directory")

    skip_git_repo_check = bool(payload.get("skip_git_repo_check", False))
    if not skip_git_repo_check and not (workdir / ".git").exists():
        return ToolAdapterResult(
            startup_error=(
                "Codex requires a Git repository workdir. "
                "Point 'input.workdir' to a Git repo, or explicitly opt-in to skip with "
                "'input.skip_git_repo_check=true'."
            )
        )

    if TOOLER_CODEX_MOCK:
        return ToolAdapterResult(
            command=[
                "bash",
                "-lc",
                "echo 'codex-mock: deterministic output'; echo 'progress: mock runner' >&2",
            ]
        )

    if shutil.which("codex") is None:
        return ToolAdapterResult(
            startup_error=(
                "Codex CLI binary is not available in tooler container. "
                "Install @openai/codex so 'codex exec' can run."
            )
        )

    auth_file = CODEX_HOME / "auth.json"
    if not auth_file.exists():
        return ToolAdapterResult(
            startup_error=(
                "Codex is not authenticated. Run `codex login` on the host and mount ~/.codex "
                "into tooler (CODEX_HOME)."
            )
        )

    mode = str(payload.get("mode") or TOOLER_CODEX_MODE).strip().lower() or "readonly"
    if mode not in {"readonly", "full-auto"}:
        abort(400, description="Field 'input.mode' must be one of: readonly, full-auto")

    model = str(payload.get("model") or TOOLER_CODEX_MODEL).strip()
    approval_policy = str(payload.get("approval_policy", "")).strip()
    json_output = bool(payload.get("json", False))

    command = ["codex", "exec", "--cd", str(workdir)]
    if model:
        command.extend(["--model", model])
    if approval_policy:
        command.extend(["--approval-policy", approval_policy])

    if mode == "full-auto":
        command.append("--full-auto")
    else:
        command.extend(["--sandbox", "read-only"])

    if skip_git_repo_check:
        command.append("--skip-git-repo-check")
    if json_output:
        command.append("--json")

    command.append(prompt)
    return ToolAdapterResult(command=command)


TOOL_ADAPTERS: dict[str, Any] = {
    "dummy": _build_dummy_adapter,
    "codex": _build_codex_adapter,
    "git-autocommit": _build_git_autocommit_adapter,
}


_tool_runs: dict[str, ToolRun] = {}
_tool_runs_lock = threading.Lock()


def _tail_lines(path: Path, line_count: int) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-line_count:])


def _build_dummy_command(payload: dict[str, Any]) -> list[str]:
    message = str(payload.get("message", "dummy tool started")).replace("\n", " ").strip()
    sleep_seconds = payload.get("sleep_seconds", 1)
    try:
        sleep_value = max(0.0, min(30.0, float(sleep_seconds)))
    except (TypeError, ValueError):
        abort(400, description="Field 'input.sleep_seconds' must be numeric")

    script = (
        "echo \"start: {message}\"; "
        "echo \"working...\"; "
        "sleep {sleep_value}; "
        "echo \"done\""
    ).format(message=message.replace('"', "'"), sleep_value=sleep_value)
    return ["bash", "-c", script]


def _resolve_tool_adapter(tool_name: str, payload: dict[str, Any]) -> ToolAdapterResult:
    adapter = TOOL_ADAPTERS.get(tool_name)
    if adapter is None:
        allowed = ", ".join(sorted(TOOL_ADAPTERS))
        abort(400, description=f"Tool '{tool_name}' is not allowed. Allowed tools: {allowed}")

    result = adapter(payload)
    if result.command is None and result.startup_error is None:
        abort(500, description=f"Tool '{tool_name}' adapter returned invalid configuration")

    if result.command is not None and result.startup_error is not None:
        abort(500, description=f"Tool '{tool_name}' adapter returned ambiguous configuration")

    return result


def _run_sync_tool(tool_name: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    adapter_result = _resolve_tool_adapter(tool_name, input_payload)
    if adapter_result.startup_error:
        abort(400, description=adapter_result.startup_error)

    command = adapter_result.command
    if command is None:
        abort(500, description=f"Tool '{tool_name}' command is not configured")

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(CODEX_HOME)},
    )
    stdout_text = completed.stdout or ""
    stderr_text = completed.stderr or ""

    response: dict[str, Any] = {
        "tool": tool_name,
        "exit_code": completed.returncode,
        "result_text": stdout_text.strip(),
        "stderr": stderr_text.strip(),
    }

    if tool_name == "git-autocommit":
        branch = None
        commit_hash = None
        clean_stdout_lines: list[str] = []
        for line in stdout_text.splitlines():
            if line.startswith("__BRANCH__="):
                branch = line.split("=", 1)[1].strip() or None
                continue
            if line.startswith("__COMMIT_HASH__="):
                commit_hash = line.split("=", 1)[1].strip() or None
                continue
            clean_stdout_lines.append(line)

        response["result_text"] = "\n".join(clean_stdout_lines).strip()
        if branch:
            response["branch"] = branch
        if commit_hash:
            response["commit_hash"] = commit_hash

    if completed.returncode != 0:
        if tool_name == "codex":
            stderr_lower = stderr_text.lower()
            auth_markers = ("not authenticated", "login", "unauthorized", "auth")
            if any(marker in stderr_lower for marker in auth_markers):
                abort(
                    500,
                    description=(
                        "Codex is not authenticated. Run `codex login` on the host and mount "
                        "~/.codex into tooler (CODEX_HOME)."
                    ),
                )
        abort(500, description=(stderr_text.strip() or "Tool execution failed"))

    return response


def _preexec_for_user() -> Any:
    if not RUN_AS_USER:
        return None

    user_info = pwd.getpwnam(RUN_AS_USER)

    def demote() -> None:
        os.setgid(user_info.pw_gid)
        os.setuid(user_info.pw_uid)

    return demote


def _send_callback(run: ToolRun) -> None:
    if not run.callback_url or run.callback_sent:
        return

    payload = {
        "tool_run_id": run.id,
        "status": run.status,
        "pid": run.pid,
        "exit_code": run.exit_code,
        "artifacts": run.artifacts,
    }

    from urllib import error as urlerror
    from urllib import request as urlrequest

    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        run.callback_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=5):
            run.callback_sent = True
    except (urlerror.URLError, TimeoutError) as error:
        app.logger.warning("Tool callback failed for run %s: %s", run.id, error)


def _runner_thread(run_id: str, command: list[str] | None) -> None:
    with _tool_runs_lock:
        run = _tool_runs.get(run_id)

    if run is None:
        return

    run.status = "RUNNING"
    run.started_at = time.time()

    if run.startup_error:
        run.status = "FAILED"
        run.exit_code = -1
        run.finished_at = time.time()
        run.stderr_path.write_text(f"{run.startup_error}\n", encoding="utf-8")
        _send_callback(run)
        return

    if command is None:
        run.status = "FAILED"
        run.exit_code = -1
        run.finished_at = time.time()
        run.stderr_path.write_text("Tool command is not configured\n", encoding="utf-8")
        _send_callback(run)
        return

    preexec_fn = None
    if RUN_AS_USER:
        try:
            preexec_fn = _preexec_for_user()
        except KeyError:
            run.status = "FAILED"
            run.finished_at = time.time()
            run.exit_code = -1
            run.stderr_path.write_text(
                f"Configured unix user '{RUN_AS_USER}' does not exist\n", encoding="utf-8"
            )
            _send_callback(run)
            return

    with run.stdout_path.open("w", encoding="utf-8") as stdout_file, run.stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                preexec_fn=preexec_fn,
                env={**os.environ, "CODEX_HOME": str(CODEX_HOME)},
            )
        except Exception as error:
            run.status = "FAILED"
            run.finished_at = time.time()
            run.exit_code = -1
            stderr_file.write(f"Failed to start process: {error}\n")
            stderr_file.flush()
            _send_callback(run)
            return

        run.pid = process.pid
        run.process = process
        exit_code = process.wait()
        run.exit_code = exit_code
        run.finished_at = time.time()
        run.status = "SUCCEEDED" if exit_code == 0 else "FAILED"
        run.process = None

    if run.tool_name == "git-autocommit" and run.stdout_path.exists():
        for line in run.stdout_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("__BRANCH__="):
                run.branch = line.split("=", 1)[1].strip() or None
            if line.startswith("__COMMIT_HASH__="):
                run.commit_hash = line.split("=", 1)[1].strip() or None

        if run.branch and f"branch:{run.branch}" not in run.artifacts:
            run.artifacts.append(f"branch:{run.branch}")
        if run.commit_hash and f"commit_hash:{run.commit_hash}" not in run.artifacts:
            run.artifacts.append(f"commit_hash:{run.commit_hash}")

    _send_callback(run)




@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "bad_request", "message": str(error.description)}), 400


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "not_found", "message": str(error.description)}), 404
@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200


@app.post("/tooler/run")
def run_tooler() -> tuple:
    payload = request.get_json(silent=True) or {}
    tool_name = str(payload.get("tool_name") or "dummy").strip() or "dummy"
    input_payload = payload.get("input")

    if not isinstance(input_payload, dict):
        input_payload = {}

    text = payload.get("text")
    if isinstance(text, str) and text.strip() and "message" not in input_payload:
        input_payload["message"] = text.strip()

    result = _run_sync_tool(tool_name, input_payload)
    return jsonify(result), 200


@app.post("/tool-runs")
def create_tool_run() -> tuple:
    payload = request.get_json(silent=True) or {}
    tool_name = str(payload.get("tool_name", "")).strip()
    if not tool_name:
        abort(400, description="Field 'tool_name' is required")

    input_payload = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    callback_url = str(payload.get("callback_url", "")).strip() or None

    adapter_result = _resolve_tool_adapter(tool_name, input_payload)

    run_id = str(uuid.uuid4())
    run_dir = ARTIFACTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"

    run = ToolRun(
        id=run_id,
        tool_name=tool_name,
        status="QUEUED",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        callback_url=callback_url,
        artifacts=[str(stdout_path), str(stderr_path)],
        startup_error=adapter_result.startup_error,
    )

    with _tool_runs_lock:
        _tool_runs[run_id] = run

    thread = threading.Thread(
        target=_runner_thread,
        args=(run_id, adapter_result.command),
        daemon=True,
        name=f"tool-runner-{run_id[:8]}",
    )
    thread.start()

    # Give the runner a brief chance to spawn the process so pid is visible immediately.
    time.sleep(0.05)

    return jsonify({"tool_run_id": run_id, "pid": run.pid, "status": run.status}), 201


@app.get("/tool-runs/<tool_run_id>")
def get_tool_run(tool_run_id: str) -> tuple:
    with _tool_runs_lock:
        run = _tool_runs.get(tool_run_id)

    if run is None:
        abort(404, description="Tool run not found")

    if run.process is not None:
        exit_code = run.process.poll()
        if exit_code is not None:
            run.exit_code = exit_code
            run.finished_at = time.time()
            run.status = "SUCCEEDED" if exit_code == 0 else "FAILED"
            run.process = None
            _send_callback(run)

    return (
        jsonify(
            {
                "tool_run_id": run.id,
                "status": run.status,
                "stdout_tail": _tail_lines(run.stdout_path, TAIL_LINES),
                "stderr_tail": _tail_lines(run.stderr_path, TAIL_LINES),
                "artifacts": run.artifacts,
                "pid": run.pid,
                "exit_code": run.exit_code,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "branch": run.branch,
                "commit_hash": run.commit_hash,
            }
        ),
        200,
    )


if __name__ == "__main__":
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
