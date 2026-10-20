"""RUWORK — FastAPI-сервер: API + раздача сайта из public/.

Запуск: uvicorn app.main:app --host 0.0.0.0 --port 4000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import tg
from .db import ROOT, init_db
from .routes import account, admin, catalog, chat, money, orders, reviews, share, social, tgapp

PUBLIC = (ROOT / "public").resolve()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    tg.setup()
    yield


app = FastAPI(title="RUWORK", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
for module in (account, catalog, money, chat, admin, orders, reviews, share, social, tgapp):
    app.include_router(module.router)


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(rest: str):
    raise HTTPException(404, "Не найдено")


@app.get("/{path:path}", include_in_schema=False)
def static(path: str, request: Request):
    """Файлы из public/; любой другой путь (и www-домен) — 301 на главную, чтобы
    переход из поисковика по старой или чужой ссылке открывал главную страницу."""
    host = request.headers.get("host", "")
    if host.startswith("www."):
        return RedirectResponse(f"https://{host[4:]}/", 301)
    target = (PUBLIC / path).resolve()
    if path and target.is_relative_to(PUBLIC) and target.is_file() and target.suffix != ".html":
        return FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})
    if path:
        return RedirectResponse("/", 301)
    return FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-cache"})
