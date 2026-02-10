import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher

from flask import Flask, abort, jsonify, request

app = Flask(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "service")
REFINE_PROVIDER = os.getenv("REFINE_PROVIDER", "mock").lower()

FILLER_PATTERN = re.compile(
    r"\b(uh|um|erm|hmm|like|you know|actually|basically|literally|well|ээ+|эм+|ну|как бы|типа|короче|в общем)\b",
    flags=re.IGNORECASE,
)

CYR_TO_LAT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "c",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)



MATCH_REPLACEMENTS = {
    "dzhimini": "gemini",
    "jimini": "gemini",
    "gimini": "gemini",
    "flesh": "flash",
    "flash": "flash",
    "dva": "2",
    "dve": "2",
    "tri": "3",
    "chetyre": "4",
    "pyat": "5",
    "pyatb": "5",
}

@dataclass
class Project:
    id: object
    name: str
    slug: str


class GeminiRefineClient:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        self.api_key = api_key
        self.model = model

    def refine(self, text: str, projects: list[Project]) -> dict[str, object]:
        prompt = {
            "instruction": (
                "You are refining ASR transcript into concise technical text. "
                "Remove filler words and obvious disfluencies, keep meaning and terminology. "
                "Infer matching project slug despite ASR spelling distortions. "
                "Return JSON with keys refined_text (string) and inferred_project_slug (string or null)."
            ),
            "text": text,
            "projects": [{"name": p.name, "slug": p.slug} for p in projects],
        }

        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": json.dumps(prompt, ensure_ascii=False),
                        }
                    ]
                }
            ],
            "generationConfig": {"responseMimeType": "application/json"},
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Gemini HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Gemini connection error: {error}") from error

        content = payload.get("candidates", [{}])[0].get("content", {})
        text_part = ""
        for part in content.get("parts", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_part = part["text"]
                break

        if not text_part:
            raise RuntimeError("Gemini empty response")

        try:
            parsed = json.loads(text_part)
        except json.JSONDecodeError as error:
            raise RuntimeError("Gemini returned non-JSON response") from error

        refined_text = str(parsed.get("refined_text", "")).strip()
        inferred_project_slug = parsed.get("inferred_project_slug")
        if inferred_project_slug is not None:
            inferred_project_slug = str(inferred_project_slug).strip() or None
        return {
            "refined_text": refined_text,
            "inferred_project_slug": inferred_project_slug,
        }


def normalize_technical_text(text: str) -> str:
    result = (text or "").strip()
    if not result:
        return ""

    result = FILLER_PATTERN.sub(" ", result)
    result = re.sub(r"\s+", " ", result)
    result = re.sub(r"\s+([,.;:!?])", r"\1", result)
    return result.strip(" ,")


def normalize_for_match(value: str) -> str:
    lowered = (value or "").lower().translate(CYR_TO_LAT)
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    tokens = [MATCH_REPLACEMENTS.get(token, token) for token in lowered.split()]
    return " ".join(tokens)


def compact_for_match(value: str) -> str:
    return normalize_for_match(value).replace(" ", "")


def score_project_match(text: str, project: Project) -> float:
    target = normalize_for_match(text)
    if not target:
        return 0.0

    candidates = [project.name, project.slug]
    best = 0.0
    for candidate in candidates:
        source = normalize_for_match(candidate)
        source_compact = compact_for_match(candidate)
        target_compact = compact_for_match(text)
        if not source:
            continue
        ratio = SequenceMatcher(None, target, source).ratio()
        token_ratios = [SequenceMatcher(None, token, source_compact).ratio() for token in target.split() if token]
        partial = 1.0 if source_compact and source_compact in target_compact else 0.0
        best = max(best, ratio, partial, *token_ratios)
    return best


def infer_project_slug(text: str, projects: list[Project], threshold: float = 0.62) -> str | None:
    if not projects:
        return None

    ranked = sorted(
        ((score_project_match(text, project), project.slug) for project in projects),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_slug = ranked[0]
    if best_score < threshold:
        return None
    return best_slug


def parse_projects(payload_projects: object) -> list[Project]:
    if not isinstance(payload_projects, list):
        return []

    parsed: list[Project] = []
    for project in payload_projects:
        if not isinstance(project, dict):
            continue
        name = project.get("name")
        slug = project.get("slug")
        if isinstance(name, str) and isinstance(slug, str) and name.strip() and slug.strip():
            parsed.append(Project(id=project.get("id"), name=name.strip(), slug=slug.strip()))
    return parsed


def run_mock_refine(text: str, projects: list[Project]) -> dict[str, object]:
    refined_text = normalize_technical_text(text)
    return {
        "refined_text": refined_text,
        "inferred_project_slug": infer_project_slug(refined_text, projects),
    }


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200


@app.post("/refine")
def refine() -> tuple:
    payload = request.get_json(silent=True) or {}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        abort(400, description="Field 'text' is required")

    projects = parse_projects(payload.get("projects"))

    provider = REFINE_PROVIDER
    if provider == "mock":
        result = run_mock_refine(text, projects)
    elif provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            abort(500, description="GEMINI_API_KEY is required for gemini provider")
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        client = GeminiRefineClient(api_key=api_key, model=model)
        try:
            result = client.refine(text, projects)
        except RuntimeError as error:
            abort(502, description=f"Gemini refine error: {error}")
    else:
        abort(500, description=f"Unsupported REFINE_PROVIDER: {provider}")

    return jsonify(result), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
