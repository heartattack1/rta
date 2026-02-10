import json
import os
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from flask import Flask, abort, jsonify, request

app = Flask(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "service")
USE_LLM_SUMMARIZER = os.getenv("SUMMARIZER_USE_LLM", "false").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class SummarizeRequest:
    refined_text: str
    tool_stdout: str
    tool_stderr: str
    mode: str


class MockSummarizer:
    def summarize(self, payload: SummarizeRequest) -> str:
        stdout_tail = _tail_text(payload.tool_stdout, line_count=8)
        stderr_tail = _tail_text(payload.tool_stderr, line_count=6)

        lines: list[str] = []

        if stderr_tail:
            err_preview = _compact(stderr_tail)
            if payload.mode == "audio":
                lines.append(f"Есть ошибка: {err_preview}.")
            else:
                lines.append(f"⚠️ Errors: {err_preview}")

        summary_bits = _extract_bullet_candidates(stdout_tail)

        if payload.mode == "audio":
            if summary_bits:
                spoken = "; ".join(summary_bits[:3])
                lines.append(f"Результат: {spoken}.")
            elif payload.refined_text:
                lines.append(f"Готово по запросу: {_compact(payload.refined_text, limit=120)}.")
            else:
                lines.append("Инструмент выполнен, деталей в выводе нет.")
            result = " ".join(lines)
            return _compact(result, limit=240)

        if summary_bits:
            lines.append("• " + "\n• ".join(summary_bits[:5]))
        elif payload.refined_text:
            lines.append(f"• Request: {_compact(payload.refined_text, limit=140)}")
            lines.append("• Tool finished without verbose output")
        else:
            lines.append("• Tool finished")

        result = "\n".join(lines)
        return _compact_lines(result, max_len=500)


class GeminiSummarizer:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def summarize(self, payload: SummarizeRequest) -> str:
        prompt = (
            "Ты сервис суммаризации вывода CLI инструмента. "
            "Сначала упомяни ошибки, затем краткий итог. "
            "Для mode=text: выдай до 5 буллетов. "
            "Для mode=audio: выдай 1-2 коротких предложения без маркеров.\n\n"
            f"mode: {payload.mode}\n"
            f"refined_text: {payload.refined_text}\n"
            f"tool_stdout: {_tail_text(payload.tool_stdout, line_count=12)}\n"
            f"tool_stderr: {_tail_text(payload.tool_stderr, line_count=10)}\n"
        )

        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 220,
            },
        }

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )

        req = urlrequest.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urlrequest.urlopen(req, timeout=15) as response:
            parsed = json.loads(response.read().decode("utf-8"))

        candidates = parsed.get("candidates") if isinstance(parsed, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("LLM summarizer returned no candidates")

        first = candidates[0] if isinstance(candidates[0], dict) else {}
        content = first.get("content") if isinstance(first, dict) else {}
        parts = content.get("parts") if isinstance(content, dict) else []
        text_chunks = [part.get("text", "") for part in parts if isinstance(part, dict)]
        summary = "\n".join(chunk.strip() for chunk in text_chunks if isinstance(chunk, str) and chunk.strip()).strip()
        if not summary:
            raise RuntimeError("LLM summarizer returned empty text")
        return _compact_lines(summary, max_len=500)


def _build_summarizer() -> Any:
    if not USE_LLM_SUMMARIZER:
        return MockSummarizer()

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    if not api_key:
        app.logger.warning("SUMMARIZER_USE_LLM enabled but GEMINI_API_KEY missing; falling back to mock summarizer")
        return MockSummarizer()

    try:
        return GeminiSummarizer(api_key=api_key, model=model)
    except Exception as error:  # noqa: BLE001
        app.logger.warning("Failed to initialize LLM summarizer; fallback to mock: %s", error)
        return MockSummarizer()


SUMMARIZER = _build_summarizer()


def _compact(value: str, limit: int = 220) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


def _tail_text(value: str, line_count: int) -> str:
    lines = value.splitlines()
    return "\n".join(lines[-line_count:]).strip()


def _extract_bullet_candidates(stdout_tail: str) -> list[str]:
    candidates: list[str] = []
    for line in stdout_tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("__BRANCH__=", "__COMMIT_HASH__=")):
            key, _, val = stripped.partition("=")
            label = "branch" if key == "__BRANCH__" else "commit"
            candidates.append(f"{label}: {val.strip()}")
            continue
        candidates.append(_compact(stripped, limit=120))

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        normalized = item.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
    return deduped


def _compact_lines(value: str, max_len: int) -> str:
    value = value.strip()
    if len(value) <= max_len:
        return value
    return f"{value[: max_len - 3]}..."


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200


@app.post("/summarize")
def summarize() -> tuple:
    payload = request.get_json(silent=True) or {}

    refined_text = str(payload.get("refined_text") or "").strip()
    tool_stdout = str(payload.get("tool_stdout") or "")
    tool_stderr = str(payload.get("tool_stderr") or "")
    mode = str(payload.get("mode") or "text").strip().lower()

    if mode not in {"text", "audio"}:
        abort(400, description="Field 'mode' must be one of: text, audio")

    if not refined_text and not tool_stdout and not tool_stderr:
        abort(400, description="At least one of refined_text/tool_stdout/tool_stderr is required")

    request_payload = SummarizeRequest(
        refined_text=refined_text,
        tool_stdout=tool_stdout,
        tool_stderr=tool_stderr,
        mode=mode,
    )

    try:
        summary_text = SUMMARIZER.summarize(request_payload)
    except (RuntimeError, ValueError, urlerror.URLError, TimeoutError) as error:
        app.logger.warning("Summarizer failed (%s), using mock fallback", error)
        summary_text = MockSummarizer().summarize(request_payload)

    return jsonify({"summary_text": summary_text, "summary": summary_text}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
