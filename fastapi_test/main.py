from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

languages = [
    {"code": "en", "label": "English", "flag": "🇬🇧", "text": "Hello! This is a simple multilingual page."},
    {"code": "es", "label": "Espanol", "flag": "🇪🇸", "text": "Hola. Esta es una pagina sencilla en varios idiomas."},
    {"code": "fr", "label": "Francais", "flag": "🇫🇷", "text": "Bonjour. Ceci est une page simple en plusieurs langues."},
    {"code": "de", "label": "Deutsch", "flag": "🇩🇪", "text": "Hallo. Dies ist eine einfache mehrsprachige Seite."},
    {"code": "tr", "label": "Turkce", "flag": "🇹🇷", "text": "Merhaba. Bu, cok dilli basit bir sayfadir."},
]

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
