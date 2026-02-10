import math
import os
import struct
import subprocess
import uuid
import wave
from pathlib import Path

from flask import Flask, abort, jsonify, request

app = Flask(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "tts")
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "mock").strip().lower()
TTS_OUTPUT_DIR = Path(os.getenv("TTS_OUTPUT_DIR", "storage/tts"))
TTS_PUBLIC_BASE_URI = os.getenv("TTS_PUBLIC_BASE_URI", "")
SILERO_LANGUAGE = os.getenv("SILERO_LANGUAGE", "ru")
SILERO_MODEL_ID = os.getenv("SILERO_MODEL_ID", "v3_1_ru")
SILERO_SPEAKER = os.getenv("SILERO_SPEAKER", "xenia")
SILERO_SAMPLE_RATE = int(os.getenv("SILERO_SAMPLE_RATE", "48000"))

_silero_model = None


def _ensure_output_dir() -> None:
    TTS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _audio_uri(path: Path) -> str:
    if TTS_PUBLIC_BASE_URI:
        return f"{TTS_PUBLIC_BASE_URI.rstrip('/')}/{path.name}"
    return str(path)


def _wav_path(task_id: str) -> Path:
    return TTS_OUTPUT_DIR / f"{task_id}.wav"


def _ogg_path(task_id: str) -> Path:
    return TTS_OUTPUT_DIR / f"{task_id}.ogg"


def _write_mock_wav(path: Path, *, seconds: float = 0.2, sample_rate: int = 16000) -> None:
    total_samples = max(1, int(seconds * sample_rate))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(total_samples):
            angle = 2.0 * math.pi * 440.0 * (index / sample_rate)
            sample = int(8000 * math.sin(angle))
            wav.writeframesraw(struct.pack("<h", sample))


def _write_mock_ogg(path: Path) -> None:
    # Minimal placeholder; enough for local integration contracts where content is not parsed.
    path.write_bytes(b"OggS\x00mock-voice")


def _ffmpeg_wav_to_ogg(wav_path: Path, ogg_path: Path) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(wav_path),
        "-ac",
        "1",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        str(ogg_path),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {completed.stderr.strip() or completed.stdout.strip()}")


def _load_silero_model():
    global _silero_model
    if _silero_model is not None:
        return _silero_model

    import torch

    torch.set_num_threads(1)
    model, _ = torch.hub.load(
        repo_or_dir="snakers4/silero-models",
        model="silero_tts",
        language=SILERO_LANGUAGE,
        speaker=SILERO_MODEL_ID,
    )
    model.to(torch.device("cpu"))
    _silero_model = model
    return _silero_model


def _synthesize_with_silero(text: str, wav_path: Path, ogg_path: Path) -> None:
    model = _load_silero_model()
    model.save_wav(text=text, speaker=SILERO_SPEAKER, sample_rate=SILERO_SAMPLE_RATE, audio_path=str(wav_path))
    _ffmpeg_wav_to_ogg(wav_path, ogg_path)


def _synthesize_mock(wav_path: Path, ogg_path: Path) -> None:
    _write_mock_wav(wav_path)
    try:
        _ffmpeg_wav_to_ogg(wav_path, ogg_path)
    except Exception:
        _write_mock_ogg(ogg_path)


def _synthesize(text: str, task_id: str) -> Path:
    _ensure_output_dir()
    wav_path = _wav_path(task_id)
    ogg_path = _ogg_path(task_id)

    if TTS_PROVIDER == "silero":
        _synthesize_with_silero(text, wav_path, ogg_path)
    else:
        _synthesize_mock(wav_path, ogg_path)

    return ogg_path


def _handle_synthesize() -> tuple:
    payload = request.get_json(silent=True) or {}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        abort(400, description="Field 'text' is required")

    task_id = str(payload.get("task_id") or uuid.uuid4())
    ogg_path = _synthesize(text=text.strip(), task_id=task_id)
    return jsonify({"audio_uri": _audio_uri(ogg_path)}), 200


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200


@app.post("/tts")
def tts() -> tuple:
    return _handle_synthesize()


@app.post("/tts/synthesize")
def synthesize_legacy() -> tuple:
    return _handle_synthesize()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
