import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

import app as summarizer_app


class SummarizerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = summarizer_app.app.test_client()

    def test_text_mode_prioritizes_errors_and_bullets(self) -> None:
        response = self.client.post(
            "/summarize",
            json={
                "refined_text": "run deployment checks",
                "tool_stdout": "step 1 done\nstep 2 done\nstep 2 done",
                "tool_stderr": "fatal: missing access rights",
                "mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("summary_text", payload)
        self.assertIn("⚠️ Errors", payload["summary_text"])
        self.assertIn("• step 1 done", payload["summary_text"])

    def test_audio_mode_returns_short_sentence(self) -> None:
        response = self.client.post(
            "/summarize",
            json={
                "refined_text": "summarize tool execution",
                "tool_stdout": "build complete\ntests passed",
                "tool_stderr": "",
                "mode": "audio",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("Результат:", payload["summary_text"])
        self.assertNotIn("\n•", payload["summary_text"])

    def test_validation_requires_any_source_text(self) -> None:
        response = self.client.post(
            "/summarize",
            json={"refined_text": "", "tool_stdout": "", "tool_stderr": "", "mode": "text"},
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
