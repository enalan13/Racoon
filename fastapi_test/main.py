from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
#sena  = here
app = FastAPI()
templates = Jinja2Templates(directory="templates")

items = []

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "items": items}
    )

@app.post("/add")
def add_item(item: str = Form(...)):
    items.append(item)
    return RedirectResponse(url="/", status_code=303)
