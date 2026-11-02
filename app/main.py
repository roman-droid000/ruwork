"""RUWORK — FastAPI-сервер: API + раздача сайта из public/.

Запуск: uvicorn app.main:app --host 0.0.0.0 --port 4000
"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import guard, jobs, tg
from .db import ROOT, connect, init_db
from .routes import account, admin, admin_tools, catalog, delete_account, chat, market, money, orders, reports, reviews, share, social, tgapp

PUBLIC = (ROOT / "public").resolve()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    tg.setup()
    jobs.start()
    yield


app = FastAPI(title="RUWORK", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
for module in (account, catalog, money, chat, admin, admin_tools, orders, reviews, share, social, tgapp, market, reports, delete_account):
    app.include_router(module.router)
app.include_router(admin_tools.mfa_router)


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    """Необработанная ошибка: оповещение администратору (одно на адрес раз в 10 минут).
    Трассировку Starlette всё равно пишет в журнал сервиса (journalctl -u ruwork)."""
    msg = f"{type(exc).__name__}: {str(exc)[:300]}"
    guard.alert(f"Ошибка 500: {request.method} {request.url.path} — {msg}", key="500:" + request.url.path)
    try:
        conn = connect()
        try:
            conn.execute("INSERT INTO errors(ts, path, msg) VALUES (?, ?, ?)",
                         (int(time.time() * 1000), f"{request.method} {request.url.path}", msg))
            conn.execute("DELETE FROM errors WHERE ts < ?", (int(time.time() * 1000) - 30 * 86_400_000,))
        finally:
            conn.close()
    except Exception:
        pass
    return JSONResponse({"detail": "Ошибка на сервере. Мы уже получили уведомление и разбираемся."}, 500)


@app.get("/api/health", include_in_schema=False)
def health():
    """Для мониторинга доступности (UptimeRobot и т. п.): 200 — всё в порядке, 503 — проблема."""
    problems = []
    try:
        conn = connect()
        try:
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:
        problems.append(f"db: {e}")
    if jobs.status["tick_at"] and time.time() * 1000 - jobs.status["tick_at"] > 10 * 60_000:
        problems.append("фоновые задачи не выполнялись больше 10 минут")
    if jobs.status["backup_error"]:
        problems.append("backup: " + jobs.status["backup_error"])
    age = time.time() - jobs.last_backup_time()
    body = {"ok": not problems, "problems": problems, "backup_age_h": round(age / 3600, 1) if age < 1e9 else None}
    return JSONResponse(body, 200 if not problems else 503)


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
