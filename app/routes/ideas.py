"""Идеи по улучшению платформы. Пользователь предлагает идею со слайда в карусели на главной,
администратор получает оповещение в Telegram и разбирает идеи во вкладке «Идеи» админ-панели."""

import html
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, guard
from ..db import get_db, now_ms
from ..logic import audit, esc, new_id, notify

router = APIRouter(prefix="/api")

IDEA_RATE = (5, 3600)  # идей в час от одного пользователя
# Статус -> (подпись для журнала, текст уведомления автору или None).
STATUSES = {
    "new": ("Новая", None),
    "planned": ("В планах", "Спасибо за идею! Мы взяли её в работу: «{t}»"),
    "done": ("Сделано", "Ваша идея реализована на RUWORK: «{t}». Спасибо!"),
    "declined": ("Отклонена", "Спасибо за идею «{t}». Сейчас мы не планируем её реализовывать."),
}


class IdeaIn(BaseModel):
    text: str


class IdeaStatusIn(BaseModel):
    status: str


@router.post("/ideas")
def create_idea(body: IdeaIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    text = esc(body.text.strip())[:2000]
    if len(body.text.strip()) < 10:
        raise HTTPException(400, "Опишите идею подробнее — хотя бы пару предложений")
    guard.throttle("idea:" + user["id"], IDEA_RATE, "Слишком много идей за час — попробуйте позже")
    conn.execute("INSERT INTO ideas(id, user_id, text, status, ts) VALUES (?, ?, ?, 'new', ?)",
                 (new_id("id"), user["id"], text, now_ms()))
    guard.alert(f"Новая идея от {user['name']}: {text[:300]}", "#/admin/ideas")
    return {"ok": True}


ADMIN_SQL = "SELECT i.*, u.name AS author FROM ideas i JOIN users u ON u.id = i.user_id"


@router.get("/admin/ideas", dependencies=[Depends(auth.require_staff)])
def admin_ideas(conn: sqlite3.Connection = Depends(get_db)):
    """Новые идеи (сначала свежие), затем 100 последних разобранных."""
    new = conn.execute(ADMIN_SQL + " WHERE i.status = 'new' ORDER BY i.ts DESC").fetchall()
    rest = conn.execute(ADMIN_SQL + " WHERE i.status != 'new' ORDER BY i.status_at DESC LIMIT 100").fetchall()
    return [dict(r) for r in new + rest]


@router.post("/admin/ideas/{idea_id}/status")
def set_idea_status(idea_id: str, body: IdeaStatusIn, staff=Depends(auth.require_staff),
                    conn: sqlite3.Connection = Depends(get_db)):
    if body.status not in STATUSES:
        raise HTTPException(400, "Неизвестный статус")
    r = conn.execute("SELECT user_id, text, status FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Идея не найдена")
    if r["status"] != body.status:
        conn.execute("UPDATE ideas SET status = ?, status_at = ? WHERE id = ?", (body.status, now_ms(), idea_id))
        label, msg = STATUSES[body.status]
        raw = html.unescape(r["text"])  # в базе текст экранирован, а колокольчик экранирует сам
        short = raw[:80] + ("…" if len(raw) > 80 else "")
        audit(conn, staff, "idea_status", f"Идея «{short}»: {label}", r["user_id"], idea_id)
        if msg:
            notify(conn, r["user_id"], "idea", msg.format(t=short), "#/")
    return admin_ideas(conn)
