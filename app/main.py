"""RUWORK — FastAPI-сервер: API + раздача сайта из public/.

Запуск: uvicorn app.main:app --host 0.0.0.0 --port 4000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .db import ROOT, init_db
from .routes import account, admin, catalog, chat, money, orders

PUBLIC = (ROOT / "public").resolve()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="RUWORK", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
for module in (account, catalog, money, chat, admin, orders):
    app.include_router(module.router)


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(rest: str):
    raise HTTPException(404, "Не найдено")


@app.get("/{path:path}", include_in_schema=False)
def static(path: str):
    """Файлы из public/; любой другой путь отдаёт index.html (SPA-фолбэк)."""
    target = (PUBLIC / path).resolve()
    if path and target.is_relative_to(PUBLIC) and target.is_file() and target.suffix != ".html":
        return FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})
    return FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-cache"})
