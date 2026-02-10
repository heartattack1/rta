import importlib.util
from pathlib import Path
from unittest.mock import Mock


def load_app_module():
    module_path = Path(__file__).resolve().parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("telegram_bot_app", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_calculate_next_offset_progresses_with_updates():
    app_module = load_app_module()

    updates = [
        {"update_id": 101, "message": {"text": "a"}},
        {"update_id": 102, "message": {"text": "b"}},
        {"update_id": 103, "message": {"text": "c"}},
    ]

    next_offset = app_module.calculate_next_offset(None, updates)

    assert next_offset == 104


def test_mode_selection_starts_thread_only_in_polling(monkeypatch):
    app_module = load_app_module()

    monkeypatch.setattr(app_module, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(app_module, "TELEGRAM_UPDATES_MODE", "polling")

    delete_webhook_mock = Mock()
    monkeypatch.setattr(app_module, "telegram_delete_webhook", delete_webhook_mock)

    started = {"value": False}

    class DummyThread:
        def __init__(self, target, daemon, name):
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self):
            started["value"] = True

    monkeypatch.setattr(app_module.threading, "Thread", DummyThread)
    app_module.start_update_receiver()

    assert delete_webhook_mock.called
    assert started["value"] is True

    started["value"] = False
    delete_webhook_mock.reset_mock()
    monkeypatch.setattr(app_module, "TELEGRAM_UPDATES_MODE", "webhook")
    app_module.start_update_receiver()

    assert delete_webhook_mock.called is False
    assert started["value"] is False


def test_empty_token_disables_receiver_without_api_calls(monkeypatch, caplog):
    app_module = load_app_module()

    monkeypatch.setattr(app_module, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(app_module, "TELEGRAM_UPDATES_MODE", "polling")

    delete_webhook_mock = Mock()
    monkeypatch.setattr(app_module, "telegram_delete_webhook", delete_webhook_mock)

    requests_get_mock = Mock()
    requests_post_mock = Mock()
    monkeypatch.setattr(app_module.requests, "get", requests_get_mock)
    monkeypatch.setattr(app_module.requests, "post", requests_post_mock)

    with caplog.at_level("INFO"):
        app_module.start_update_receiver()

    assert delete_webhook_mock.called is False
    assert requests_get_mock.called is False
    assert requests_post_mock.called is False
    assert "TELEGRAM_TOKEN is empty; update receiving is disabled" in caplog.text
