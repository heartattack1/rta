import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "telegram-bot")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_UPDATES_MODE = os.getenv("TELEGRAM_UPDATES_MODE", "polling").strip().lower()
TRACKER_URL = os.getenv("TRACKER_URL", "http://tracker:8000")
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "storage"))
DEFAULT_PROJECT_NAME = os.getenv("TRACKER_PROJECT_NAME", "telegram-bot")

_request_timeout = 15
_cached_project_id: str | None = None
_polling_thread: threading.Thread | None = None


def telegram_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"


def tracker_tasks_url() -> str:
    return f"{TRACKER_URL.rstrip('/')}/tasks"


def tracker_projects_url() -> str:
    return f"{TRACKER_URL.rstrip('/')}/projects"


def send_ack(chat_id: int) -> None:
    if not TELEGRAM_TOKEN:
        app.logger.warning("TELEGRAM_TOKEN not configured: skip ack")
        return

    try:
        requests.post(
            telegram_api_url("sendMessage"),
            json={"chat_id": chat_id, "text": "в очереди"},
            timeout=_request_timeout,
        )
    except requests.RequestException as error:
        app.logger.error("Failed to send ack to Telegram: %s", error)


def process_update(update: dict[str, Any]) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if "voice" in message:
        handle_voice_message(message)
    elif "text" in message:
        handle_text_message(message)

    if chat_id is not None:
        send_ack(chat_id)


def calculate_next_offset(current_offset: int | None, updates: list[dict[str, Any]]) -> int | None:
    next_offset = current_offset
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = update_id + 1
    return next_offset


def telegram_delete_webhook() -> None:
    if not TELEGRAM_TOKEN:
        return

    try:
        response = requests.post(
            telegram_api_url("deleteWebhook"),
            params={"drop_pending_updates": "true"},
            timeout=_request_timeout,
        )
        response.raise_for_status()
        app.logger.info("Telegram webhook removed (drop_pending_updates=true)")
    except requests.RequestException as error:
        app.logger.error("Failed to delete Telegram webhook: %s", error)


def poll_updates_forever() -> None:
    offset: int | None = None
    app.logger.info("Starting Telegram long polling loop")

    while True:
        try:
            response = requests.get(
                telegram_api_url("getUpdates"),
                params={"timeout": 30, "offset": offset},
                timeout=35,
            )
            response.raise_for_status()
            payload = response.json()
            updates = payload.get("result") or []
            if updates:
                app.logger.info("Received updates batch: %s", len(updates))
            for update in updates:
                process_update(update)
            offset = calculate_next_offset(offset, updates)
        except requests.RequestException as error:
            app.logger.error("Telegram polling request failed: %s", error)
            time.sleep(3)
        except Exception as error:
            app.logger.exception("Unexpected polling error: %s", error)
            time.sleep(3)


def start_update_receiver() -> None:
    global _polling_thread
    has_token = bool(TELEGRAM_TOKEN)
    app.logger.info(
        "Telegram updates mode=%s token_present=%s",
        TELEGRAM_UPDATES_MODE,
        has_token,
    )

    if not has_token:
        app.logger.error("TELEGRAM_TOKEN is empty; update receiving is disabled")
        return

    if TELEGRAM_UPDATES_MODE == "polling":
        telegram_delete_webhook()
        _polling_thread = threading.Thread(target=poll_updates_forever, daemon=True, name="telegram-poller")
        _polling_thread.start()
        app.logger.info("Telegram polling thread started")
        return

    if TELEGRAM_UPDATES_MODE == "webhook":
        app.logger.info("Telegram webhook mode enabled")
        return

    app.logger.error("Unknown TELEGRAM_UPDATES_MODE=%s; update receiving disabled", TELEGRAM_UPDATES_MODE)


def ensure_project_id() -> str:
    global _cached_project_id
    if _cached_project_id:
        return _cached_project_id

    payload = {"name": DEFAULT_PROJECT_NAME}
    response = requests.post(tracker_projects_url(), json=payload, timeout=_request_timeout)
    response.raise_for_status()
    project = response.json()
    _cached_project_id = project["id"]
    return _cached_project_id


def create_task(payload: dict[str, Any]) -> dict[str, Any]:
    project_id = ensure_project_id()
    body = {"project_id": project_id, **payload}
    response = requests.post(tracker_tasks_url(), json=body, timeout=_request_timeout)
    response.raise_for_status()
    return response.json()


def fetch_voice_file_path(file_id: str) -> str:
    response = requests.get(
        telegram_api_url("getFile"), params={"file_id": file_id}, timeout=_request_timeout
    )
    response.raise_for_status()
    payload = response.json()
    return payload["result"]["file_path"]


def download_file(file_path: str) -> Path:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    destination = STORAGE_DIR / Path(file_path).name

    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    response = requests.get(file_url, timeout=_request_timeout)
    response.raise_for_status()

    destination.write_bytes(response.content)
    return destination


def handle_text_message(message: dict[str, Any]) -> None:
    text = message.get("text", "").strip()
    if not text:
        return

    chat_id = (message.get("chat") or {}).get("id")
    create_task({"input_type": "text", "raw_text": text, "source_chat_id": chat_id})


def handle_voice_message(message: dict[str, Any]) -> None:
    voice = message.get("voice") or {}
    file_id = voice.get("file_id")
    if not file_id:
        return

    chat_id = (message.get("chat") or {}).get("id")
    file_path = fetch_voice_file_path(file_id)
    stored = download_file(file_path)
    create_task({"input_type": "voice", "raw_audio_uri": str(stored), "source_chat_id": chat_id})




def send_task_result(chat_id: int, summary: str, audio_uri: str | None = None) -> None:
    if not TELEGRAM_TOKEN:
        app.logger.warning("TELEGRAM_TOKEN not configured: skip task result")
        return

    if audio_uri:
        local_path = Path(audio_uri)
        if local_path.exists():
            with local_path.open("rb") as voice_file:
                requests.post(
                    telegram_api_url("sendVoice"),
                    data={"chat_id": chat_id, "caption": summary or "готово"},
                    files={"voice": voice_file},
                    timeout=_request_timeout,
                ).raise_for_status()
            return

    requests.post(
        telegram_api_url("sendMessage"),
        json={"chat_id": chat_id, "text": summary or "готово"},
        timeout=_request_timeout,
    ).raise_for_status()


@app.errorhandler(requests.RequestException)
def handle_upstream_error(error: requests.RequestException):
    app.logger.error("Upstream request failed: %s", error)
    return jsonify({"error": "upstream_error", "message": str(error)}), 502


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200




@app.post("/callbacks/task-result")
def task_result_callback() -> tuple:
    payload = request.get_json(silent=True) or {}
    app.logger.info("Task callback received: %s", payload)

    chat_id = payload.get("chat_id")
    if chat_id is not None:
        send_task_result(int(chat_id), str(payload.get("summary") or "готово"), payload.get("audio_uri"))

    return jsonify({"ok": True}), 200


@app.post("/webhook")
def telegram_webhook() -> tuple:
    if TELEGRAM_UPDATES_MODE != "webhook":
        return jsonify({"ok": False, "error": "webhook_disabled"}), 404

    if not TELEGRAM_TOKEN:
        app.logger.error("TELEGRAM_TOKEN is empty; webhook update ignored")
        return jsonify({"ok": True, "skipped": "token_missing"}), 200

    update = request.get_json(silent=True) or {}
    process_update(update)

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    start_update_receiver()
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
