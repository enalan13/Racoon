import json
import os
import re
import time
from collections import defaultdict, deque
from threading import Lock
from io import BytesIO
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject
from reportlab.pdfgen import canvas

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
OFFICIAL_PDF_PATH = os.path.join("static", "Application-for-a-Permanent-Resident-Card.pdf")

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


class PdfFillRequest(BaseModel):
    fields: Dict[str, str] = Field(default_factory=dict)
    flatten: bool = False


class PdfStampItem(BaseModel):
    page: int
    x_px: float
    y_px: float
    height_px: float
    text: str


class PdfStampRequest(BaseModel):
    fields: Dict[str, str] = Field(default_factory=dict)
    pages: List[Dict[str, float]] = Field(default_factory=list)
    stamps: List[PdfStampItem] = Field(default_factory=list)
    flatten: bool = False


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


def _load_pdf_reader() -> PdfReader:
    if not os.path.exists(OFFICIAL_PDF_PATH):
        raise HTTPException(status_code=404, detail="Official PDF not found.")
    reader = PdfReader(OFFICIAL_PDF_PATH)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise HTTPException(status_code=400, detail="Official PDF is encrypted and cannot be read.")
    return reader


def _list_pdf_fields(reader: PdfReader) -> Dict[str, Optional[str]]:
    fields = reader.get_fields() or {}
    result: Dict[str, Optional[str]] = {}
    for name, meta in fields.items():
        value = None
        if isinstance(meta, dict):
            raw_value = meta.get("/V")
            if raw_value is not None:
                value = str(raw_value)
        result[str(name)] = value
    return result


def _fill_pdf(fields: Dict[str, str], flatten: bool) -> bytes:
    reader = _load_pdf_reader()
    writer = PdfWriter(clone_from=reader)
    normalized = {}
    for key, value in fields.items():
        if isinstance(value, str) and value.startswith("/"):
            normalized[key] = NameObject(value)
        else:
            normalized[key] = value
    for page in writer.pages:
        writer.update_page_form_field_values(
            page,
            normalized,
            auto_regenerate=True,
            flatten=flatten,
        )
    writer._root_object.update({NameObject("/NeedAppearances"): BooleanObject(True)})

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _stamp_pdf(fields: Dict[str, str], pages: List[Dict[str, float]], stamps: List[PdfStampItem], flatten: bool) -> bytes:
    reader = _load_pdf_reader()
    writer = PdfWriter(clone_from=reader)

    normalized = {}
    for key, value in fields.items():
        if isinstance(value, str) and value.startswith("/"):
            normalized[key] = NameObject(value)
        else:
            normalized[key] = value
    for page in writer.pages:
        writer.update_page_form_field_values(
            page,
            normalized,
            auto_regenerate=True,
            flatten=flatten,
        )

    overlay_buf = BytesIO()
    c = canvas.Canvas(overlay_buf)

    page_count = len(writer.pages)
    for page_index in range(page_count):
        pdf_page = writer.pages[page_index]
        media_box = pdf_page.mediabox
        page_w_pt = float(media_box.width)
        page_h_pt = float(media_box.height)
        c.setPageSize((page_w_pt, page_h_pt))

        page_meta = pages[page_index] if page_index < len(pages) else {"width": 1.0, "height": 1.0}
        page_w_px = float(page_meta.get("width", 1.0)) or 1.0
        page_h_px = float(page_meta.get("height", 1.0)) or 1.0

        for item in stamps:
            if item.page != page_index:
                continue
            text = (item.text or "").strip()
            if not text:
                continue
            x_pt = (item.x_px / page_w_px) * page_w_pt
            baseline_px = item.y_px + (item.height_px * 0.75)
            y_pt = page_h_pt - (baseline_px / page_h_px) * page_h_pt
            font_size = max(7.0, min(12.0, (item.height_px / page_h_px) * page_h_pt * 0.8))
            c.setFont("Helvetica", font_size)
            c.drawString(x_pt, y_pt, text)

        c.showPage()

    c.save()
    overlay_buf.seek(0)
    overlay_reader = PdfReader(overlay_buf)

    for i, page in enumerate(writer.pages):
        if i < len(overlay_reader.pages):
            page.merge_page(overlay_reader.pages[i])

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


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


@app.get("/pr-card-form-pdf", response_class=HTMLResponse)
def pr_card_form_pdf(request: Request):
    return templates.TemplateResponse(
        "pr_card_form_pdf.html",
        {"request": request}
    )


@app.get("/api/pr-card-pdf/fields")
def pr_card_pdf_fields():
    reader = _load_pdf_reader()
    return _list_pdf_fields(reader)


@app.post("/api/pr-card-pdf")
def pr_card_pdf_fill(payload: PdfFillRequest):
    pdf_bytes = _fill_pdf(payload.fields, payload.flatten)
    headers = {"Content-Disposition": "attachment; filename=pr-card-filled.pdf"}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.post("/api/pr-card-pdf-stamp")
def pr_card_pdf_stamp(payload: PdfStampRequest):
    pdf_bytes = _stamp_pdf(payload.fields, payload.pages, payload.stamps, payload.flatten)
    headers = {"Content-Disposition": "attachment; filename=pr-card-filled.pdf"}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


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
