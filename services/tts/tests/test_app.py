import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TtsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()

        os.environ["TTS_PROVIDER"] = "mock"
        os.environ["TTS_OUTPUT_DIR"] = self.tmpdir.name

        if "app" in sys.modules:
            del sys.modules["app"]
        import app as tts_app  # pylint: disable=import-outside-toplevel

        self.tts_app = tts_app
        self.client = tts_app.app.test_client()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_tts_returns_ogg_uri_and_writes_files(self) -> None:
        response = self.client.post("/tts", json={"text": "Короткое summary", "task_id": "task-123"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["audio_uri"].endswith("task-123.ogg"))
        self.assertTrue(Path(self.tmpdir.name, "task-123.wav").exists())
        self.assertTrue(Path(self.tmpdir.name, "task-123.ogg").exists())

    def test_legacy_endpoint_is_supported(self) -> None:
        response = self.client.post("/tts/synthesize", json={"text": "summary"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("audio_uri", payload)
        self.assertTrue(payload["audio_uri"].endswith(".ogg"))

    def test_validation_requires_text(self) -> None:
        response = self.client.post("/tts", json={})

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
