import tempfile
import unittest
import wave
from pathlib import Path

import app as asr_app


class AsrHelpersTest(unittest.TestCase):
    def _create_wav(self, path: Path, duration_seconds: int) -> None:
        sample_rate = 16000
        frames = b"\x00\x00" * sample_rate * duration_seconds
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(frames)

    def test_chunk_wav_splits_long_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "input.wav"
            self._create_wav(wav_path, duration_seconds=32)

            chunks = asr_app.chunk_wav(wav_path, chunk_seconds=15)

            self.assertEqual(len(chunks), 3)
            durations = [round(asr_app.wav_duration_seconds(chunk), 1) for chunk in chunks]
            self.assertEqual(durations, [15.0, 15.0, 2.0])

    def test_transcribe_wav_joins_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "input.wav"
            self._create_wav(wav_path, duration_seconds=31)

            original_loader = asr_app._load_transcriber

            def fake_loader():
                def fake_transcribe(path: Path) -> str:
                    return path.stem

                return fake_transcribe

            asr_app._load_transcriber = fake_loader
            try:
                transcript = asr_app.transcribe_wav(wav_path)
            finally:
                asr_app._load_transcriber = original_loader

            self.assertIn("input.chunk_0000", transcript)
            self.assertIn("input.chunk_0001", transcript)
            self.assertIn("input.chunk_0002", transcript)


if __name__ == "__main__":
    unittest.main()
