import json
import os
import pwd
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request

app = Flask(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "service")
ARTIFACTS_DIR = Path(os.getenv("TOOLER_ARTIFACTS_DIR", "/tmp/tooler-artifacts"))
RUN_AS_USER = os.getenv("TOOLER_RUN_AS_USER", "").strip()
TAIL_LINES = int(os.getenv("TOOLER_TAIL_LINES", "40"))
CODEX_API_KEY = os.getenv("CODEX_API_KEY", "").strip()


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


@dataclass(frozen=True)
class ToolAdapterResult:
    command: list[str] | None = None
    startup_error: str | None = None


def _build_dummy_adapter(payload: dict[str, Any]) -> ToolAdapterResult:
    return ToolAdapterResult(command=_build_dummy_command(payload))


def _build_codex_adapter(payload: dict[str, Any]) -> ToolAdapterResult:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        abort(400, description="Field 'input.prompt' is required for tool 'codex'")

    if not CODEX_API_KEY:
        return ToolAdapterResult(
            startup_error=(
                "Tool 'codex' is not configured: set CODEX_API_KEY environment variable"
            )
        )

    script = (
        "echo \"codex tool invoked\"; "
        "echo \"prompt: {prompt}\"; "
        "echo \"CODEX_API_KEY is configured\""
    ).format(prompt=prompt.replace('"', "'"))
    return ToolAdapterResult(command=["bash", "-c", script])


TOOL_ADAPTERS: dict[str, Any] = {
    "dummy": _build_dummy_adapter,
    "codex": _build_codex_adapter,
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
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        abort(400, description="Field 'text' is required")

    result_text = text.strip()
    return jsonify({"result_text": result_text, "tool": "noop"}), 200


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
            }
        ),
        200,
    )


if __name__ == "__main__":
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
