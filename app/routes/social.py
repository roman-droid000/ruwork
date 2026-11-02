"""Колокольчик уведомлений, избранное и публичный профиль продавца."""

import sqlite3

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, guard, support, tg
from ..db import get_db, now_ms
from ..logic import (
    SOLD_SQL, ST_CANCEL, ST_DONE, TOP_SELLER_SALES, esc, gig_out, response_times, seller_level, seller_ratings, unread_notes,
)
from .market import portfolio

router = APIRouter(prefix="/api")


class AboutIn(BaseModel):
    about: str


@router.get("/notifications")
def notifications(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT id, kind, text, link, read, ts FROM notifications WHERE user_id = ? ORDER BY ts DESC, id DESC LIMIT 50",
        (user["id"],),
    ).fetchall()
    return {"items": [dict(r) for r in rows], "unread": unread_notes(conn, user["id"])}


@router.post("/notifications/read")
def notifications_read(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    conn.execute("UPDATE notifications SET read = 1 WHERE user_id = ? AND read = 0", (user["id"],))
    # Старше 200 последних не храним.
    conn.execute(
        "DELETE FROM notifications WHERE user_id = ? AND id NOT IN"
        " (SELECT id FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 200)",
        (user["id"], user["id"]),
    )
    return {"unread": 0}


@router.post("/favs/{gig_id}")
def toggle_fav(gig_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Добавить в избранное или убрать оттуда. Возвращает весь список."""
    if not conn.execute("DELETE FROM favorites WHERE user_id = ? AND gig_id = ?", (user["id"], gig_id)).rowcount:
        if not conn.execute("SELECT 1 FROM gigs WHERE id = ?", (gig_id,)).fetchone():
            raise HTTPException(404, "Объявление не найдено")
        conn.execute("INSERT INTO favorites(user_id, gig_id, ts) VALUES (?, ?, ?)", (user["id"], gig_id, now_ms()))
    return {"favs": [r[0] for r in conn.execute(
        "SELECT gig_id FROM favorites WHERE user_id = ? ORDER BY ts DESC", (user["id"],))]}


@router.patch("/me/about")
def change_about(body: AboutIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    about = esc(guard.mask_contacts(body.about.strip()))[:600]
    conn.execute("UPDATE users SET about = ? WHERE id = ?", (about, user["id"]))
    return {"me": auth.public_user(conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())}


@router.get("/sellers/{user_id}")
def seller_profile(user_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Публичный профиль продавца: без email и баланса."""
    u = conn.execute("SELECT id, name, about, avatar, created_at, blocked_at, last_seen, verified, vacation_until, vacation_note,"
                     " ext_url, ext_site, ext_rating, ext_reviews, ext_status FROM users WHERE id = ?",
                     (user_id,)).fetchone()
    if not u or u["blocked_at"] is not None:
        raise HTTPException(404, "Продавец не найден")
    done = conn.execute("SELECT count(*) FROM orders WHERE seller_id = ? AND status = ?", (user_id, ST_DONE)).fetchone()[0]
    active = conn.execute(
        "SELECT count(*) FROM orders WHERE seller_id = ? AND status NOT IN (?, 'Отменён')", (user_id, ST_DONE)
    ).fetchone()[0]
    gigs = conn.execute(
        f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.owner_id = ? AND g.mod = 'ok' ORDER BY g.created_at DESC", (user_id,)
    ).fetchall()
    # Доля завершённых сделок (выполненных и отменённых) без спора.
    closed, calm = conn.execute(
        "SELECT count(*), sum(dispute_at IS NULL) FROM orders WHERE seller_id = ? AND status IN (?, ?)",
        (user_id, ST_DONE, ST_CANCEL),
    ).fetchone()
    rating, rev = seller_ratings(conn).get(user_id, (0, 0))
    lvl = seller_level(conn, user_id, full=True)
    return {"id": u["id"], "name": u["name"], "about": u["about"], "avatar": u["avatar"], "since": u["created_at"], "done": done,
            "active": active, "top_seller": done > TOP_SELLER_SALES, "gigs": [gig_out(r) for r in gigs],
            "rating": rating, "rev": rev, "verified": bool(u["verified"]), "resp": response_times(conn).get(user_id),
            "online": (u["last_seen"] or 0) >= now_ms() - auth.ONLINE_MS,
            "calm_pct": round(100 * calm / closed) if closed else None,
            "level": lvl["name"], "level_idx": lvl["idx"], "on_time": lvl["stats"]["on_time"] if lvl["stats"]["done"] else None,
            "away": u["vacation_until"] if (u["vacation_until"] or 0) > now_ms() else None, "away_note": u["vacation_note"],
            "portfolio": portfolio(conn, user_id),
            "ext": {"site": u["ext_site"], "url": u["ext_url"], "rating": u["ext_rating"], "reviews": u["ext_reviews"]}
            if u["ext_status"] == "ok" else None}


# Telegram сотрудника — второй фактор входа в админку: с сайта его не отвязать и не сменить,
# иначе укравший пароль привязал бы свой Telegram. Сменить можно только в базе на сервере.
STAFF_TG = "Telegram сотрудника служит для входа в админ-панель и меняется только через владельца сайта"


@router.post("/me/telegram")
def telegram_link(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Ссылка на бота с одноразовым кодом: после /start <код> бот привяжет этот чат к аккаунту."""
    if auth.staff_role(user) and user["tg_chat"]:
        raise HTTPException(403, STAFF_TG)
    if not (tg.TOKEN and tg.username):
        raise HTTPException(400, "Уведомления в Telegram пока не настроены")
    code = tg.new_link_code()
    conn.execute("UPDATE users SET tg_code = ? WHERE id = ?", (code, user["id"]))
    return {"url": f"https://t.me/{tg.username}?start={code}"}


@router.delete("/me/telegram")
def telegram_unlink(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if auth.staff_role(user):
        raise HTTPException(403, STAFF_TG)
    row = conn.execute("SELECT tg_chat FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.execute("UPDATE users SET tg_chat = NULL, tg_code = NULL WHERE id = ?", (user["id"],))
    tg.send(row["tg_chat"], "Уведомления RUWORK отключены. Подключить снова можно в профиле на сайте.")
    return {"ok": True}


@router.post("/tg/{secret}", include_in_schema=False)
def telegram_webhook(secret: str, request: Request, update: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)):
    if not tg.SECRET or secret != tg.SECRET or request.headers.get("x-telegram-bot-api-secret-token") != tg.SECRET:
        raise HTTPException(404, "Не найдено")
    try:
        support.handle(conn, update)
    except Exception:  # иначе Telegram будет повторять это обновление снова и снова
        support.log.exception("Ошибка обработки обновления Telegram")
    return {"ok": True}
