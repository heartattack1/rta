import json
import os
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, abort, jsonify, request

app = Flask(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "tracker")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///tmp/tracker.db")
ASR_URL = os.getenv("ASR_URL", "http://asr:8000")
REFINE_URL = os.getenv("REFINE_URL", "http://refine:8000")
TOOLER_URL = os.getenv("TOOLER_URL", "http://tooler:8000")
SUMMARIZER_URL = os.getenv("SUMMARIZER_URL", "http://summarizer:8000")
TTS_URL = os.getenv("TTS_URL", "http://tts:8000")
BOT_CALLBACK_URL = os.getenv("BOT_CALLBACK_URL", "")
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "20"))

TASK_STATES = [
    "RECEIVED",
    "ROUTED",
    "TRANSCRIBING",
    "REFINING",
    "TOOL_QUEUED",
    "TOOL_RUNNING",
    "SUMMARIZING",
    "TTS_GENERATING",
    "DELIVERED",
    "FAILED",
]


ALLOWED_TASK_TRANSITIONS = {
    "RECEIVED": {"ROUTED", "FAILED"},
    "ROUTED": {"TRANSCRIBING", "REFINING", "FAILED"},
    "TRANSCRIBING": {"REFINING", "FAILED"},
    "REFINING": {"TOOL_QUEUED", "FAILED"},
    "TOOL_QUEUED": {"TOOL_RUNNING", "FAILED"},
    "TOOL_RUNNING": {"SUMMARIZING", "FAILED"},
    "SUMMARIZING": {"TTS_GENERATING", "DELIVERED", "FAILED"},
    "TTS_GENERATING": {"DELIVERED", "FAILED"},
    "DELIVERED": set(),
    "FAILED": set(),
}

TOOL_RUN_STATES = ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]

_task_queue: queue.Queue[str] = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def parse_database_path(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise RuntimeError("Only sqlite:/// DATABASE_URL is supported")
    return database_url[len(prefix) :]


def get_connection() -> sqlite3.Connection:
    db_path = parse_database_path(DATABASE_URL)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    db_path = Path(parse_database_path(DATABASE_URL))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path = Path(__file__).resolve().parent / "sql" / "schema.sql"
    ddl = schema_path.read_text(encoding="utf-8")
    with get_connection() as connection:
        connection.executescript(ddl)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "failure_reason" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN failure_reason TEXT")
        connection.commit()


def row_to_task(row: sqlite3.Row, connection: sqlite3.Connection) -> dict[str, Any]:
    tool_runs = connection.execute(
        "SELECT id FROM tool_runs WHERE task_id = ? ORDER BY created_at ASC", (row["id"],)
    ).fetchall()
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "input_type": row["input_type"],
        "raw_text": row["raw_text"],
        "raw_audio_uri": row["raw_audio_uri"],
        "transcript": row["transcript"],
        "refined_text": row["refined_text"],
        "status": row["status"],
        "tool_runs": [tool_run["id"] for tool_run in tool_runs],
        "final_summary": row["final_summary"],
        "final_audio_uri": row["final_audio_uri"],
        "failure_reason": row["failure_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_task_or_404(task_id: str, connection: sqlite3.Connection) -> sqlite3.Row:
    task_row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task_row is None:
        abort(404, description="Task not found")
    return task_row


def get_project_or_404(project_id: str, connection: sqlite3.Connection) -> sqlite3.Row:
    project_row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project_row is None:
        abort(404, description="Project not found")
    return project_row


def validate_task_transition(current_status: str, next_status: str) -> None:
    if next_status == current_status:
        return
    allowed = ALLOWED_TASK_TRANSITIONS.get(current_status, set())
    if next_status not in allowed:
        abort(
            400,
            description=(
                f"Invalid task status transition from {current_status} to {next_status}"
            ),
        )


def enqueue_task(task_id: str) -> None:
    _task_queue.put(task_id)


def update_task_internal(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    status: str | None = None,
    transcript: str | None = None,
    refined_text: str | None = None,
    final_summary: str | None = None,
    final_audio_uri: str | None = None,
    failure_reason: str | None = None,
) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Task not found: {task_id}")

    current_status = row["status"]
    updates: list[str] = []
    values: list[Any] = []

    if status is not None:
        validate_task_transition(current_status, status)
        updates.append("status = ?")
        values.append(status)

    if transcript is not None:
        updates.append("transcript = ?")
        values.append(transcript)

    if refined_text is not None:
        updates.append("refined_text = ?")
        values.append(refined_text)

    if final_summary is not None:
        updates.append("final_summary = ?")
        values.append(final_summary)

    if final_audio_uri is not None:
        updates.append("final_audio_uri = ?")
        values.append(final_audio_uri)

    if failure_reason is not None:
        updates.append("failure_reason = ?")
        values.append(failure_reason)

    if not updates:
        return row

    updated_at = utc_now_iso()
    updates.append("updated_at = ?")
    values.append(updated_at)
    values.append(task_id)

    connection.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)

    if status is not None and status != current_status:
        connection.execute(
            "INSERT INTO task_status_history (id, task_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, current_status, status, updated_at),
        )

    return connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def create_tool_run_internal(
    connection: sqlite3.Connection,
    task_id: str,
    tool_name: str,
    input_payload: dict[str, Any] | None,
) -> str:
    tool_run_id = str(uuid.uuid4())
    now = utc_now_iso()
    connection.execute(
        """
        INSERT INTO tool_runs (id, task_id, tool_name, status, input, output, started_at, finished_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tool_run_id,
            task_id,
            tool_name,
            "QUEUED",
            json.dumps(input_payload) if input_payload is not None else None,
            None,
            None,
            None,
            now,
            now,
        ),
    )
    return tool_run_id


def update_tool_run_internal(
    connection: sqlite3.Connection,
    tool_run_id: str,
    *,
    status: str,
    output_payload: dict[str, Any] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    now = utc_now_iso()
    connection.execute(
        """
        UPDATE tool_runs
        SET status = ?, output = ?, started_at = COALESCE(?, started_at),
            finished_at = COALESCE(?, finished_at), updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            json.dumps(output_payload) if output_payload is not None else None,
            started_at,
            finished_at,
            now,
            tool_run_id,
        ),
    )


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(url, json=payload, timeout=UPSTREAM_TIMEOUT_SECONDS)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return body


def process_task(task_id: str) -> None:
    with get_connection() as connection:
        task_row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task_row is None:
            app.logger.warning("Skip missing task %s", task_id)
            return

    try:
        with get_connection() as connection:
            task_row = update_task_internal(connection, task_id, status="ROUTED")
            connection.commit()

        input_text: str
        if task_row["input_type"] == "voice":
            with get_connection() as connection:
                update_task_internal(connection, task_id, status="TRANSCRIBING")
                connection.commit()
            asr_result = post_json(
                f"{ASR_URL.rstrip('/')}/asr/transcribe",
                {"audio_uri": task_row["raw_audio_uri"]},
            )
            transcript = str(asr_result.get("transcript", "")).strip()
            if not transcript:
                raise RuntimeError("ASR returned empty transcript")
            input_text = transcript
            with get_connection() as connection:
                update_task_internal(
                    connection,
                    task_id,
                    transcript=transcript,
                    status="REFINING",
                    failure_reason="",
                )
                connection.commit()
        else:
            input_text = str(task_row["raw_text"] or "").strip()
            if not input_text:
                raise RuntimeError("Text task has empty raw_text")
            with get_connection() as connection:
                update_task_internal(connection, task_id, status="REFINING", failure_reason="")
                connection.commit()

        refine_result = post_json(
            f"{REFINE_URL.rstrip('/')}/refine",
            {"text": input_text, "projects": []},
        )
        refined_text = str(refine_result.get("refined_text", "")).strip()
        if not refined_text:
            raise RuntimeError("Refine returned empty refined_text")

        with get_connection() as connection:
            update_task_internal(connection, task_id, refined_text=refined_text, status="TOOL_QUEUED")
            tool_run_id = create_tool_run_internal(
                connection,
                task_id,
                "tooler",
                {"text": refined_text},
            )
            connection.commit()

        with get_connection() as connection:
            update_task_internal(connection, task_id, status="TOOL_RUNNING")
            update_tool_run_internal(
                connection,
                tool_run_id,
                status="RUNNING",
                started_at=utc_now_iso(),
            )
            connection.commit()

        tool_result = post_json(
            f"{TOOLER_URL.rstrip('/')}/tooler/run",
            {"task_id": task_id, "text": refined_text},
        )

        with get_connection() as connection:
            update_tool_run_internal(
                connection,
                tool_run_id,
                status="SUCCEEDED",
                output_payload=tool_result,
                finished_at=utc_now_iso(),
            )
            update_task_internal(connection, task_id, status="SUMMARIZING")
            connection.commit()

        summarize_result = post_json(
            f"{SUMMARIZER_URL.rstrip('/')}/summarize",
            {
                "task_id": task_id,
                "refined_text": refined_text,
                "tool_stdout": str(tool_result.get("result_text") or ""),
                "tool_stderr": str(tool_result.get("stderr") or ""),
                "mode": "audio" if task_row["input_type"] == "voice" else "text",
            },
        )
        summary_text = str(
            summarize_result.get("summary_text")
            or summarize_result.get("summary")
            or ""
        ).strip()
        if not summary_text:
            raise RuntimeError("Summarizer returned empty summary")

        final_audio_uri: str | None = None
        if task_row["input_type"] == "voice":
            with get_connection() as connection:
                update_task_internal(connection, task_id, final_summary=summary_text, status="TTS_GENERATING")
                connection.commit()
            tts_result = post_json(
                f"{TTS_URL.rstrip('/')}/tts/synthesize",
                {"text": summary_text, "task_id": task_id},
            )
            final_audio_uri = str(tts_result.get("audio_uri", "")).strip()
            if not final_audio_uri:
                raise RuntimeError("TTS returned empty audio_uri")

        with get_connection() as connection:
            update_task_internal(
                connection,
                task_id,
                final_summary=summary_text,
                final_audio_uri=final_audio_uri,
                status="DELIVERED",
                failure_reason="",
            )
            delivered_task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            connection.commit()

        if BOT_CALLBACK_URL.strip():
            payload = {
                "task_id": task_id,
                "status": "DELIVERED",
                "summary": delivered_task["final_summary"],
                "audio_uri": delivered_task["final_audio_uri"],
            }
            try:
                requests.post(BOT_CALLBACK_URL, json=payload, timeout=UPSTREAM_TIMEOUT_SECONDS)
            except requests.RequestException as callback_error:
                app.logger.warning("Bot callback failed for task %s: %s", task_id, callback_error)

    except Exception as error:
        reason = str(error).strip() or "Unknown pipeline error"
        app.logger.exception("Task %s failed: %s", task_id, reason)
        with get_connection() as connection:
            row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is not None and row["status"] != "FAILED":
                update_task_internal(
                    connection,
                    task_id,
                    status="FAILED",
                    failure_reason=reason[:500],
                )
                connection.commit()


def worker_loop() -> None:
    while True:
        task_id = _task_queue.get()
        try:
            process_task(task_id)
        finally:
            _task_queue.task_done()


def ensure_worker_started() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        worker = threading.Thread(target=worker_loop, daemon=True, name="tracker-orchestrator")
        worker.start()
        _worker_started = True


@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "bad_request", "message": str(error.description)}), 400


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "not_found", "message": str(error.description)}), 404


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200


@app.post("/projects")
def create_project() -> tuple:
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        abort(400, description="Field 'name' is required")

    project_id = str(uuid.uuid4())
    now = utc_now_iso()
    metadata = payload.get("metadata")

    with get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id, name, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, name, json.dumps(metadata) if metadata is not None else None, now, now),
        )
        connection.commit()

    return (
        jsonify(
            {
                "id": project_id,
                "name": name,
                "metadata": metadata,
                "created_at": now,
                "updated_at": now,
            }
        ),
        201,
    )


@app.get("/projects")
def list_projects() -> tuple:
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()

    projects = [
        {
            "id": row["id"],
            "name": row["name"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    return jsonify(projects), 200


@app.post("/tasks")
def create_task() -> tuple:
    payload = request.get_json(silent=True) or {}

    project_id = payload.get("project_id")
    if not project_id:
        abort(400, description="Field 'project_id' is required")

    input_type = payload.get("input_type")
    raw_text = payload.get("raw_text")
    raw_audio_uri = payload.get("raw_audio_uri")

    if input_type not in {"text", "voice"}:
        abort(400, description="Field 'input_type' must be one of: text, voice")

    if input_type == "text" and not raw_text:
        abort(400, description="Field 'raw_text' is required for text tasks")

    if input_type == "voice" and not raw_audio_uri:
        abort(400, description="Field 'raw_audio_uri' is required for voice tasks")

    task_id = str(uuid.uuid4())
    status = "RECEIVED"
    now = utc_now_iso()

    with get_connection() as connection:
        get_project_or_404(project_id, connection)
        connection.execute(
            """
            INSERT INTO tasks (
                id, project_id, input_type, raw_text, raw_audio_uri,
                transcript, refined_text, status, final_summary,
                final_audio_uri, failure_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                project_id,
                input_type,
                raw_text,
                raw_audio_uri,
                None,
                None,
                status,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO task_status_history (id, task_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, None, status, now),
        )
        task_row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        connection.commit()
        task = row_to_task(task_row, connection)

    enqueue_task(task_id)
    return jsonify(task), 201


@app.get("/tasks/<task_id>")
def get_task(task_id: str) -> tuple:
    with get_connection() as connection:
        row = get_task_or_404(task_id, connection)
        task = row_to_task(row, connection)
        history_rows = connection.execute(
            "SELECT from_status, to_status, changed_at FROM task_status_history WHERE task_id = ? ORDER BY changed_at ASC",
            (task_id,),
        ).fetchall()

    task["status_history"] = [
        {
            "from": history_row["from_status"],
            "to": history_row["to_status"],
            "changed_at": history_row["changed_at"],
        }
        for history_row in history_rows
    ]

    return jsonify(task), 200


@app.patch("/tasks/<task_id>")
def update_task(task_id: str) -> tuple:
    payload = request.get_json(silent=True) or {}
    updatable_fields = {
        "status": "status",
        "transcript": "transcript",
        "refined_text": "refined_text",
        "final_summary": "final_summary",
        "final_audio_uri": "final_audio_uri",
        "raw_audio_uri": "raw_audio_uri",
        "failure_reason": "failure_reason",
    }

    unknown_fields = [key for key in payload.keys() if key not in updatable_fields]
    if unknown_fields:
        abort(400, description=f"Unknown fields: {', '.join(unknown_fields)}")

    if not payload:
        abort(400, description="No fields to update")

    with get_connection() as connection:
        task_row = get_task_or_404(task_id, connection)
        current_status = task_row["status"]

        if "status" in payload:
            next_status = payload["status"]
            if next_status not in TASK_STATES:
                abort(400, description=f"Unknown status '{next_status}'")
            validate_task_transition(current_status, next_status)

        update_assignments = []
        values = []
        for payload_key, column in updatable_fields.items():
            if payload_key in payload:
                update_assignments.append(f"{column} = ?")
                values.append(payload[payload_key])

        updated_at = utc_now_iso()
        update_assignments.append("updated_at = ?")
        values.append(updated_at)
        values.append(task_id)

        connection.execute(
            f"UPDATE tasks SET {', '.join(update_assignments)} WHERE id = ?", values
        )

        if "status" in payload and payload["status"] != current_status:
            connection.execute(
                "INSERT INTO task_status_history (id, task_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    task_id,
                    current_status,
                    payload["status"],
                    updated_at,
                ),
            )

        updated_row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        connection.commit()
        task = row_to_task(updated_row, connection)

    return jsonify(task), 200


@app.post("/tool-runs")
def create_tool_run() -> tuple:
    payload = request.get_json(silent=True) or {}

    task_id = payload.get("task_id")
    tool_name = str(payload.get("tool_name", "")).strip()
    status = payload.get("status", "QUEUED")
    input_payload = payload.get("input")
    output_payload = payload.get("output")

    if not task_id:
        abort(400, description="Field 'task_id' is required")

    if not tool_name:
        abort(400, description="Field 'tool_name' is required")

    if status not in TOOL_RUN_STATES:
        abort(400, description=f"Field 'status' must be one of: {', '.join(TOOL_RUN_STATES)}")

    tool_run_id = str(uuid.uuid4())
    now = utc_now_iso()

    with get_connection() as connection:
        get_task_or_404(task_id, connection)
        connection.execute(
            """
            INSERT INTO tool_runs (id, task_id, tool_name, status, input, output, started_at, finished_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tool_run_id,
                task_id,
                tool_name,
                status,
                json.dumps(input_payload) if input_payload is not None else None,
                json.dumps(output_payload) if output_payload is not None else None,
                payload.get("started_at"),
                payload.get("finished_at"),
                now,
                now,
            ),
        )
        connection.commit()

    return jsonify({"id": tool_run_id, "task_id": task_id, "status": status}), 201


@app.get("/tool-runs/<tool_run_id>")
def get_tool_run(tool_run_id: str) -> tuple:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM tool_runs WHERE id = ?", (tool_run_id,)).fetchone()

    if row is None:
        abort(404, description="Tool run not found")

    return (
        jsonify(
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "tool_name": row["tool_name"],
                "status": row["status"],
                "input": json.loads(row["input"]) if row["input"] else None,
                "output": json.loads(row["output"]) if row["output"] else None,
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        ),
        200,
    )


init_db()
ensure_worker_started()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
