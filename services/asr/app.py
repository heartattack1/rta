import logging
import os
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import urlretrieve

from flask import Flask, abort, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

SERVICE_NAME = os.getenv("SERVICE_NAME", "service")
SAMPLE_RATE = 16_000
CHANNELS = 1
CHUNK_SECONDS = int(os.getenv("ASR_CHUNK_SECONDS", "15"))
TOO_LONG_ERROR = "Too long wav file"

_transcribe_fn: Callable[[Path], str] | None = None


def resolve_audio_uri(audio_uri: str, workdir: Path) -> Path:
    parsed = urlparse(audio_uri)
    if parsed.scheme in {"http", "https"}:
        target = workdir / "input.ogg"
        urlretrieve(audio_uri, target)
        return target

    if parsed.scheme == "file":
        file_path = Path(parsed.path)
    else:
        file_path = Path(audio_uri)

    if not file_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_uri}")

    target = workdir / file_path.name
    if target.resolve() != file_path.resolve():
        shutil.copy(file_path, target)
    return target


def convert_audio_to_wav(source_path: Path, target_path: Path) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        str(target_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav_file:
        frames = wav_file.getnframes()
        sample_rate = wav_file.getframerate()
        return frames / float(sample_rate)


def chunk_wav(wav_path: Path, chunk_seconds: int = CHUNK_SECONDS) -> list[Path]:
    chunks: list[Path] = []
    with wave.open(str(wav_path), "rb") as wav_in:
        sample_rate = wav_in.getframerate()
        frames_per_chunk = sample_rate * chunk_seconds
        total_frames = wav_in.getnframes()
        params = wav_in.getparams()

        chunk_index = 0
        while True:
            chunk_frames = wav_in.readframes(frames_per_chunk)
            if not chunk_frames:
                break

            chunk_path = wav_path.with_name(f"{wav_path.stem}.chunk_{chunk_index:04d}.wav")
            with wave.open(str(chunk_path), "wb") as wav_out:
                wav_out.setparams(params)
                wav_out.writeframes(chunk_frames)
            chunks.append(chunk_path)
            chunk_index += 1

    if total_frames == 0:
        return [wav_path]
    return chunks or [wav_path]


def _extract_text(raw_result: object) -> str:
    if isinstance(raw_result, str):
        return raw_result.strip()

    if isinstance(raw_result, dict):
        text = raw_result.get("text") or raw_result.get("transcript")
        if isinstance(text, str):
            return text.strip()

    if isinstance(raw_result, list):
        parts = [_extract_text(item) for item in raw_result]
        return " ".join(part for part in parts if part).strip()

    return ""


def _load_transcriber() -> Callable[[Path], str]:
    global _transcribe_fn
    if _transcribe_fn is not None:
        return _transcribe_fn

    from gigaam import GigaAM  # type: ignore

    model_name = os.getenv("GIGAAM_MODEL", "v2_rnnt")
    device = os.getenv("GIGAAM_DEVICE", "cpu")
    model = GigaAM(model_name=model_name, device=device)

    def transcribe(path: Path) -> str:
        if hasattr(model, "transcribe"):
            raw = model.transcribe(str(path))
        else:
            raw = model(str(path))

        text = _extract_text(raw)
        if not text:
            raise RuntimeError("Empty ASR result")
        return text

    _transcribe_fn = transcribe
    return _transcribe_fn


def transcribe_wav(wav_path: Path) -> str:
    transcribe_fn = _load_transcriber()
    total_duration = wav_duration_seconds(wav_path)
    app.logger.info("ASR wav duration: %.2fs", total_duration)

    chunk_paths = [wav_path]
    if total_duration > CHUNK_SECONDS:
        chunk_paths = chunk_wav(wav_path, CHUNK_SECONDS)
        app.logger.info("ASR chunked into %s parts (%ss)", len(chunk_paths), CHUNK_SECONDS)

    pieces: list[str] = []
    for idx, chunk_path in enumerate(chunk_paths):
        chunk_start = time.monotonic()
        try:
            text = transcribe_fn(chunk_path)
        except Exception as error:
            if TOO_LONG_ERROR in str(error) and len(chunk_paths) == 1:
                app.logger.warning("Chunking fallback due to model length error")
                chunk_paths = chunk_wav(wav_path, CHUNK_SECONDS)
                return transcribe_wav_from_chunks(chunk_paths, transcribe_fn)
            raise
        elapsed = time.monotonic() - chunk_start
        app.logger.info("ASR chunk %s/%s done in %.2fs", idx + 1, len(chunk_paths), elapsed)
        pieces.append(text)

    return " ".join(piece for piece in pieces if piece).strip()


def transcribe_wav_from_chunks(chunk_paths: list[Path], transcribe_fn: Callable[[Path], str]) -> str:
    pieces: list[str] = []
    for idx, chunk_path in enumerate(chunk_paths):
        chunk_start = time.monotonic()
        text = transcribe_fn(chunk_path)
        elapsed = time.monotonic() - chunk_start
        app.logger.info("ASR chunk %s/%s done in %.2fs", idx + 1, len(chunk_paths), elapsed)
        pieces.append(text)
    return " ".join(piece for piece in pieces if piece).strip()


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200


@app.post("/asr/transcribe")
def asr_transcribe() -> tuple:
    payload = request.get_json(silent=True) or {}
    audio_uri = payload.get("audio_uri")
    if not audio_uri:
        abort(400, description="Field 'audio_uri' is required")

    request_started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="asr-") as tmp_dir:
        workdir = Path(tmp_dir)
        try:
            source_path = resolve_audio_uri(audio_uri, workdir)
            wav_path = workdir / "input.wav"

            convert_start = time.monotonic()
            convert_audio_to_wav(source_path, wav_path)
            app.logger.info("ASR convert OGG→WAV done in %.2fs", time.monotonic() - convert_start)

            transcription_start = time.monotonic()
            transcript_text = transcribe_wav(wav_path)
            app.logger.info("ASR transcription done in %.2fs", time.monotonic() - transcription_start)
        except subprocess.CalledProcessError as error:
            app.logger.exception("ffmpeg conversion failed")
            abort(400, description=error.stderr.strip() or "ffmpeg conversion failed")
        except FileNotFoundError as error:
            abort(400, description=str(error))
        except Exception as error:
            app.logger.exception("ASR transcription failed")
            abort(500, description=f"ASR error: {error}")

    app.logger.info("ASR total request time %.2fs", time.monotonic() - request_started)
    return jsonify({"transcript_text": transcript_text}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
