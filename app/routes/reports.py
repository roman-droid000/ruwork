"""Жалобы на объявления, пользователей и задания биржи. Пользователь отправляет жалобу, администратор
получает оповещение в Telegram и разбирает её во вкладке «Жалобы» админ-панели."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, guard
from ..db import get_db, now_ms
from ..logic import audit, esc, new_id

router = APIRouter(prefix="/api")

REASONS = ["Запрещённая услуга", "Мошенничество или обман", "Просит оплату или контакты вне RUWORK",
           "Оскорбления или спам", "Другое"]
REPORT_RATE = (10, 3600)  # жалоб в час от одного пользователя
KINDS = {
    "gig": "SELECT title AS name, owner_id AS owner FROM gigs WHERE id = ?",
    "user": "SELECT name, id AS owner FROM users WHERE id = ?",
    "request": "SELECT title AS name, user_id AS owner FROM requests WHERE id = ?",
}
KIND_NAMES = {"gig": "объявление", "user": "пользователя", "request": "задание"}


class ReportIn(BaseModel):
    kind: str
    target: str
    reason: str
    text: str = ""


@router.post("/reports")
def create_report(body: ReportIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if body.kind not in KINDS or body.reason not in REASONS:
        raise HTTPException(400, "Выберите причину жалобы")
    text = esc(body.text.strip())[:1000]
    if body.reason == "Другое" and len(text) < 10:
        raise HTTPException(400, "Опишите проблему подробнее")
    t = conn.execute(KINDS[body.kind], (body.target,)).fetchone()
    if t is None:
        raise HTTPException(404, "Не найдено")
    if t["owner"] == user["id"]:
        raise HTTPException(400, "Нельзя пожаловаться на себя")
    guard.throttle("report:" + user["id"], REPORT_RATE, "Слишком много жалоб — попробуйте позже")
    # Повторная жалоба на то же самое обновляет прежнюю и снова открывает её.
    conn.execute(
        "INSERT INTO reports(id, user_id, kind, target, reason, text, status, ts) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)"
        " ON CONFLICT(user_id, kind, target) DO UPDATE SET reason = excluded.reason, text = excluded.text,"
        " status = 'open', ts = excluded.ts",
        (new_id("rp"), user["id"], body.kind, body.target, body.reason, text, now_ms()),
    )
    n = conn.execute("SELECT count(*) FROM reports WHERE kind = ? AND target = ? AND status = 'open'",
                     (body.kind, body.target)).fetchone()[0]
    guard.alert(f"Жалоба на {KIND_NAMES[body.kind]} «{t['name']}» от {user['name']}: {body.reason}"
                + (f" — {text[:200]}" if text else "") + (f" (открытых жалоб: {n})" if n > 1 else ""),
                "#/admin/reports")
    return {"ok": True}


ADMIN_SQL = (
    "SELECT r.*, u.name AS reporter,"
    " CASE r.kind WHEN 'gig' THEN (SELECT title FROM gigs WHERE id = r.target)"
    " WHEN 'user' THEN (SELECT name FROM users WHERE id = r.target)"
    " ELSE (SELECT title FROM requests WHERE id = r.target) END AS target_name,"
    " CASE r.kind WHEN 'gig' THEN (SELECT owner_id FROM gigs WHERE id = r.target)"
    " WHEN 'user' THEN r.target ELSE (SELECT user_id FROM requests WHERE id = r.target) END AS owner,"
    " (SELECT count(*) FROM reports x WHERE x.kind = r.kind AND x.target = r.target AND x.status = 'open') AS same"
    " FROM reports r JOIN users u ON u.id = r.user_id"
)


@router.get("/admin/reports", dependencies=[Depends(auth.require_staff)])
def admin_reports(conn: sqlite3.Connection = Depends(get_db)):
    """Открытые жалобы (сначала старые) и 50 последних разобранных."""
    open_ = conn.execute(ADMIN_SQL + " WHERE r.status = 'open' ORDER BY r.ts").fetchall()
    done = conn.execute(ADMIN_SQL + " WHERE r.status != 'open' ORDER BY r.ts DESC LIMIT 50").fetchall()
    return [dict(r) for r in open_ + done]


@router.post("/admin/reports/{report_id}/close")
def close_report(report_id: str, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    """Жалоба разобрана. Закрываются и остальные открытые жалобы на то же самое."""
    r = conn.execute("SELECT kind, target FROM reports WHERE id = ?", (report_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Жалоба не найдена")
    n = conn.execute("UPDATE reports SET status = 'closed' WHERE kind = ? AND target = ? AND status = 'open'",
                     (r["kind"], r["target"])).rowcount
    audit(conn, staff, "report_close", f"Разобрано жалоб на {KIND_NAMES[r['kind']]}: {n}", None, r["target"])
    return admin_reports(conn)
