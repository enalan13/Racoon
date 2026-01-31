import json
import os
import re
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

app = FastAPI()
templates = Jinja2Templates(directory="templates")

languages = [
    {"code": "en", "label": "English", "flag": "🇬🇧", "text": "Hello! This is a simple multilingual page."},
    {"code": "es", "label": "Espanol", "flag": "🇪🇸", "text": "Hola. Esta es una pagina sencilla en varios idiomas."},
    {"code": "fr", "label": "Francais", "flag": "🇫🇷", "text": "Bonjour. Ceci est une page simple en plusieurs langues."},
    {"code": "de", "label": "Deutsch", "flag": "🇩🇪", "text": "Hallo. Dies ist eine einfache mehrsprachige Seite."},
    {"code": "tr", "label": "Turkce", "flag": "🇹🇷", "text": "Merhaba. Bu, cok dilli basit bir sayfadir."},
]

RATE_LIMIT_PER_MIN = 10
_rate_lock = Lock()
_rate_bucket: Dict[str, Deque[float]] = defaultdict(deque)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    selected_field_id: str = Field(..., min_length=1, max_length=200)
    selected_field_label: str = Field(..., min_length=1, max_length=200)
    user_language: str = Field(..., min_length=2, max_length=50)


class ChatResponse(BaseModel):
    assistant_answer: str
    suggested_fill_en: str
    warnings: List[str]


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        q = _rate_bucket[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            return True
        q.append(now)
    return False


def _strip_html(text: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def _mask_sensitive(text: str) -> (str, List[str]):
    warnings = []
    masked = text

    patterns = [
        (r"\b\d{3}-\d{2}-\d{4}\b", "SSN/SIN"),
        (r"\b\d{9}\b", "SIN"),
        (r"\b(?:\d[ -]*?){13,19}\b", "credit card"),
        (r"\b[A-Z]{1,2}\d{6,8}\b", "passport"),
        (r"\b[A-Z0-9]{8,9}\b", "passport"),
    ]

    for pattern, label in patterns:
        if re.search(pattern, masked):
            masked = re.sub(pattern, "[REDACTED]", masked)
            warnings.append(f"Sensitive data detected: {label}. Please avoid sharing it.")

    return masked, warnings


def _build_messages(payload: ChatRequest, sanitized: str, warnings: List[str]) -> List[dict]:
    developer = (
        "You are an assistant that helps users fill Canadian PR forms. "
        "Focus ONLY on the selected field. Do NOT provide legal advice. "
        "Warn if sensitive data is present. Respond in the user's language. "
        "Return a JSON object with keys: assistant_answer, suggested_fill_en, warnings."
    )
    user = {
        "selected_field_id": payload.selected_field_id,
        "selected_field_label_en": payload.selected_field_label,
        "user_language": payload.user_language,
        "user_message": sanitized,
        "sensitive_data_warnings": warnings,
    }
    return [
        {"role": "developer", "content": developer},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _call_llm(messages: List[dict]) -> str:
    return json.dumps(
        {
            "assistant_answer": "AI is currently disabled. Please connect an LLM provider.",
            "suggested_fill_en": "",
            "warnings": [],
        }
    )


def _parse_model_json(text: str) -> Dict[str, object]:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {
        "assistant_answer": text.strip(),
        "suggested_fill_en": "",
        "warnings": [],
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "languages": languages, "default_code": "en"}
    )


@app.get("/pr-card-form", response_class=HTMLResponse)
def pr_card_form(request: Request):
    return templates.TemplateResponse(
        "pr_card_form.html",
        {"request": request}
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest):
    ip = _get_client_ip(request)
    if _rate_limited(ip):
        return JSONResponse(
            status_code=429,
            content={
                "assistant_answer": "Please wait a moment before sending more messages.",
                "suggested_fill_en": "",
                "warnings": ["Rate limit: 10 requests per minute per IP."],
            },
        )

    cleaned = _strip_html(payload.message)
    sanitized, warnings = _mask_sensitive(cleaned)
    messages = _build_messages(payload, sanitized, warnings)
    raw = _call_llm(messages)
    parsed = _parse_model_json(raw)
    merged_warnings = warnings + list(parsed.get("warnings", []))

    return ChatResponse(
        assistant_answer=str(parsed.get("assistant_answer", "")).strip(),
        suggested_fill_en=str(parsed.get("suggested_fill_en", "")).strip(),
        warnings=merged_warnings,
    )
