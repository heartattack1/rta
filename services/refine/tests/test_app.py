import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import unittest

import app as refine_app


class RefineServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["REFINE_PROVIDER"] = "mock"
        refine_app.REFINE_PROVIDER = "mock"
        self.client = refine_app.app.test_client()

    def test_mock_refine_removes_fillers(self) -> None:
        response = self.client.post(
            "/refine",
            json={"text": "Ну, um давайте типа сделаем deploy проекта."},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["refined_text"], "давайте сделаем deploy проекта.")
        self.assertIsNone(payload["inferred_project_slug"])

    def test_mock_refine_matches_project_with_asr_distortion(self) -> None:
        response = self.client.post(
            "/refine",
            json={
                "text": "Сегодня обсуждаем джимини флеш два пять и задержки.",
                "projects": [
                    {"id": 1, "name": "Gemini Flash 2.5", "slug": "gemini-flash-2-5"},
                    {"id": 2, "name": "Whisper", "slug": "whisper"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["inferred_project_slug"], "gemini-flash-2-5")

    def test_refine_validates_required_text(self) -> None:
        response = self.client.post("/refine", json={"projects": []})

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
