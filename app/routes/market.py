"""Биржа заданий, подписки на поиск, портфолио, статистика продавца, уровни и режим «В отпуске».

Биржа: покупатель публикует задание с бюджетом и сроком, продавцы откликаются одним из своих объявлений
со своей ценой и сроком. Принятый отклик сразу становится обычным заказом по этому объявлению
(оплата с баланса, удержание до подтверждения), поэтому чат, отзывы и споры работают как у любого заказа.
"""

import re
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import auth, guard
from ..db import UPLOAD_DIR, get_db, now_ms, tx
from ..logic import (
    CATS, DAY_MS, MINP, ST_CANCEL, ST_DONE, SUBS, TIERS, VACATION_MAX_MS, esc, get_coins, new_id, notify, rub,
    seller_level, user_state,
)
from .chat import chat_owner
from .money import place_order, seller_away, usable_bonus
from .orders import COVER_MAX, NAME_RE, cover_ext

router = APIRouter()

REQ_OPEN, REQ_HIRED, REQ_CLOSED = "open", "hired", "closed"
OPEN_REQUESTS_MAX = 5
SEARCHES_MAX = 10
PORTFOLIO_MAX = 30


# ---------- Биржа заданий ----------

class RequestIn(BaseModel):
    cat: str
    sub: str
    title: str
    descr: str
    budget: float
    days: int


class OfferIn(BaseModel):
    gig_id: str
    price: float
    days: int
    text: str


def user_brief(conn: sqlite3.Connection, user_id: str) -> dict:
    u = conn.execute("SELECT id, name, avatar, verified FROM users WHERE id = ?", (user_id,)).fetchone()
    return {"id": u["id"], "name": u["name"], "avatar": u["avatar"], "verified": bool(u["verified"])} if u else {}


def request_out(conn: sqlite3.Connection, r: sqlite3.Row, viewer: str | None) -> dict:
    n = conn.execute("SELECT count(*) FROM offers WHERE request_id = ? AND status != 'withdrawn'", (r["id"],)).fetchone()[0]
    mine = conn.execute("SELECT 1 FROM offers WHERE request_id = ? AND seller_id = ? AND status != 'withdrawn'",
                        (r["id"], viewer)).fetchone() if viewer else None
    return {"id": r["id"], "cat": r["cat"], "sub": r["sub"], "title": r["title"], "descr": r["descr"],
            "budget": r["budget"], "days": r["days"], "status": r["status"], "ts": r["ts"], "order": r["order_id"],
            "buyer": user_brief(conn, r["user_id"]), "offers": n, "own": r["user_id"] == viewer, "offered": bool(mine)}


@router.get("/api/requests")
def list_requests(mine: int = 0, user=Depends(auth.optional_user), conn: sqlite3.Connection = Depends(get_db)):
    """Открытые задания всех покупателей или (mine=1) все свои, включая закрытые."""
    uid = user["id"] if user else None
    if mine and uid:
        rows = conn.execute("SELECT * FROM requests WHERE user_id = ? ORDER BY ts DESC LIMIT 100", (uid,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT r.* FROM requests r JOIN users u ON u.id = r.user_id WHERE r.status = ? AND u.blocked_at IS NULL"
            " ORDER BY r.ts DESC LIMIT 200", (REQ_OPEN,),
        ).fetchall()
    return [request_out(conn, r, uid) for r in rows]


@router.post("/api/requests")
def create_request(body: RequestIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    guard.check_banned(body.title, body.descr)
    title = esc(guard.mask_contacts(body.title.strip()))[:120]
    descr = esc(guard.mask_contacts(body.descr.strip()))[:2000]
    budget = round(body.budget)
    if body.cat not in CATS or body.sub not in SUBS.get(body.cat, []):
        raise HTTPException(400, "Выберите категорию и подкатегорию")
    if len(title) < 5 or len(descr) < 20:
        raise HTTPException(400, "Опишите задание: название от 5 символов, описание от 20")
    if not 100 <= budget <= 1_000_000:
        raise HTTPException(400, "Бюджет — от 100 до 1 000 000 ₽")
    if not 1 <= body.days <= 60:
        raise HTTPException(400, "Срок — от 1 до 60 дней")
    with tx(conn):
        n = conn.execute("SELECT count(*) FROM requests WHERE user_id = ? AND status = ?", (user["id"], REQ_OPEN)).fetchone()[0]
        if n >= OPEN_REQUESTS_MAX:
            raise HTTPException(400, f"Открытых заданий может быть не больше {OPEN_REQUESTS_MAX}. Закройте одно из старых.")
        rid = new_id("rq")
        conn.execute(
            "INSERT INTO requests(id, user_id, cat, sub, title, descr, budget, days, status, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, user["id"], body.cat, body.sub, title, descr, budget, body.days, REQ_OPEN, now_ms()),
        )
        return request_out(conn, conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone(), user["id"])


def load_request(conn: sqlite3.Connection, rid: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "Задание не найдено")
    return r


def offer_out(conn: sqlite3.Connection, o: sqlite3.Row) -> dict:
    g = conn.execute("SELECT id, title, cover FROM gigs WHERE id = ?", (o["gig_id"],)).fetchone()
    return {"id": o["id"], "price": o["price"], "days": o["days"], "text": o["text"], "status": o["status"], "ts": o["ts"],
            "seller": {**user_brief(conn, o["seller_id"]), "level": seller_level(conn, o["seller_id"])[1]},
            "gig": {"id": g["id"], "title": g["title"], "cover": g["cover"]} if g else None}


@router.get("/api/requests/{rid}")
def get_request(rid: str, user=Depends(auth.optional_user), conn: sqlite3.Connection = Depends(get_db)):
    """Автор задания видит все отклики, продавец — только свой, остальные — только их число."""
    r = load_request(conn, rid)
    uid = user["id"] if user else None
    if r["status"] != REQ_OPEN and r["user_id"] != uid and not conn.execute(
            "SELECT 1 FROM offers WHERE request_id = ? AND seller_id = ?", (rid, uid)).fetchone():
        raise HTTPException(404, "Задание закрыто")
    out = request_out(conn, r, uid)
    if r["user_id"] == uid:
        rows = conn.execute("SELECT * FROM offers WHERE request_id = ? AND status != 'withdrawn' ORDER BY ts", (rid,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM offers WHERE request_id = ? AND seller_id = ? AND status != 'withdrawn'",
                            (rid, uid)).fetchall()
    out["list"] = [offer_out(conn, o) for o in rows]
    return out


@router.post("/api/requests/{rid}/offers")
def send_offer(rid: str, body: OfferIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Отклик продавца одним из своих объявлений. Повторный отклик заменяет прежний."""
    text = esc(guard.mask_contacts(body.text.strip()))[:1000]
    price = round(body.price)
    if len(text) < 10:
        raise HTTPException(400, "Напишите покупателю, как вы выполните задание (от 10 символов)")
    if not 1 <= body.days <= 60:
        raise HTTPException(400, "Срок — от 1 до 60 дней")
    with tx(conn):
        r = load_request(conn, rid)
        if r["status"] != REQ_OPEN:
            raise HTTPException(400, "Задание уже закрыто")
        if r["user_id"] == user["id"]:
            raise HTTPException(400, "Нельзя откликнуться на своё задание")
        g = conn.execute("SELECT id, sub, mod FROM gigs WHERE id = ? AND owner_id = ?", (body.gig_id, user["id"])).fetchone()
        if not g:
            raise HTTPException(400, "Выберите своё объявление, по которому будет оформлен заказ")
        if g["mod"] != "ok":
            raise HTTPException(400, "Это объявление ещё на проверке или скрыто — откликнитесь другим")
        if not max(100, MINP.get(g["sub"], 0)) <= price <= 1_000_000:
            raise HTTPException(400, f"Цена — от {rub(max(100, MINP.get(g['sub'], 0)))} до 1 000 000 ₽")
        if seller_away(conn, user["id"]):
            raise HTTPException(400, "Вы в режиме «В отпуске». Выключите его в профиле, чтобы откликаться.")
        old = conn.execute("SELECT id FROM offers WHERE request_id = ? AND seller_id = ?", (rid, user["id"])).fetchone()
        if old:
            conn.execute("UPDATE offers SET gig_id = ?, price = ?, days = ?, text = ?, status = 'open', ts = ? WHERE id = ?",
                         (g["id"], price, body.days, text, now_ms(), old["id"]))
        else:
            conn.execute(
                "INSERT INTO offers(id, request_id, seller_id, gig_id, price, days, text, status, ts) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (new_id("of"), rid, user["id"], g["id"], price, body.days, text, now_ms()),
            )
            notify(conn, r["user_id"], "offer", f"Новый отклик на задание «{r['title']}»: {user['name']}, {rub(price)}, {body.days} дн.",
                   f"#/request/{rid}")
        return get_request(rid, user=user, conn=conn)


@router.delete("/api/requests/{rid}/offers")
def withdraw_offer(rid: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if not conn.execute("UPDATE offers SET status = 'withdrawn' WHERE request_id = ? AND seller_id = ? AND status = 'open'",
                        (rid, user["id"])).rowcount:
        raise HTTPException(400, "Отклика нет")
    return get_request(rid, user=user, conn=conn)


@router.post("/api/requests/{rid}/close")
def close_request(rid: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if not conn.execute("UPDATE requests SET status = ? WHERE id = ? AND user_id = ? AND status = ?",
                        (REQ_CLOSED, rid, user["id"], REQ_OPEN)).rowcount:
        raise HTTPException(400, "Задание уже закрыто")
    return get_request(rid, user=user, conn=conn)


@router.post("/api/offers/{offer_id}/accept")
def accept_offer(offer_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Покупатель выбирает исполнителя: оплата отклика с баланса, заказ по объявлению продавца с ценой и сроком отклика."""
    with tx(conn):
        of = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
        r = load_request(conn, of["request_id"]) if of else None
        if not of or r["user_id"] != user["id"]:
            raise HTTPException(404, "Отклик не найден")
        if r["status"] != REQ_OPEN or of["status"] != "open":
            raise HTTPException(400, "Задание уже закрыто")
        g = conn.execute(
            "SELECT g.id, g.title, g.owner_id FROM gigs g JOIN users u ON u.id = g.owner_id"
            " WHERE g.id = ? AND u.blocked_at IS NULL AND g.mod = 'ok'",
            (of["gig_id"],),
        ).fetchone()
        if not g:
            raise HTTPException(400, "Объявление продавца больше недоступно")
        if seller_away(conn, g["owner_id"]):
            raise HTTPException(400, "Продавец сейчас в отпуске и не принимает заказы")
        if get_coins(conn, user["id"]) + usable_bonus(conn, user["id"], [g["owner_id"]]) < of["price"]:
            raise HTTPException(400, f"Недостаточно монет: нужно {rub(of['price'])}. Пополните баланс в кошельке.")
        oid = place_order(conn, user["id"], g, 0, "Заказ с биржи", of["price"], of["days"], TIERS[0][3], "bal")
        conn.execute("UPDATE requests SET status = ?, order_id = ? WHERE id = ?", (REQ_HIRED, oid, r["id"]))
        conn.execute("UPDATE offers SET status = CASE WHEN id = ? THEN 'accepted' ELSE 'declined' END"
                     " WHERE request_id = ? AND status = 'open'", (offer_id, r["id"]))
        notify(conn, of["seller_id"], "sale", f"Ваш отклик на задание «{r['title']}» принят. Заказ оплачен.", f"#/order/{oid}")
        return {"order": oid, **user_state(conn, user["id"])}


# ---------- Подписки на поиск ----------

class SearchIn(BaseModel):
    q: str = ""
    cat: str = ""
    sub: str = ""
    max: int = 100000


def searches(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, q, cat, sub, max, ts FROM saved_searches WHERE user_id = ? ORDER BY ts DESC", (user_id,))]


@router.get("/api/searches")
def list_searches(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    return searches(conn, user["id"])


@router.post("/api/searches")
def save_search(body: SearchIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    q = esc(body.q.strip())[:100]
    cat = body.cat if body.cat in CATS else ""
    sub = body.sub if cat and body.sub in SUBS.get(cat, []) else ""
    mx = max(500, min(100000, int(body.max)))
    if not (q or cat or mx < 100000):
        raise HTTPException(400, "Задайте запрос, категорию или цену — иначе подходить будет каждое объявление")
    with tx(conn):
        if conn.execute("SELECT count(*) FROM saved_searches WHERE user_id = ?", (user["id"],)).fetchone()[0] >= SEARCHES_MAX:
            raise HTTPException(400, f"Подписок может быть не больше {SEARCHES_MAX}")
        if not conn.execute("SELECT 1 FROM saved_searches WHERE user_id = ? AND q = ? AND cat = ? AND sub = ? AND max = ?",
                            (user["id"], q, cat, sub, mx)).fetchone():
            conn.execute("INSERT INTO saved_searches(id, user_id, q, cat, sub, max, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (new_id("ss"), user["id"], q, cat, sub, mx, now_ms()))
        return searches(conn, user["id"])


@router.delete("/api/searches/{sid}")
def delete_search(sid: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    conn.execute("DELETE FROM saved_searches WHERE id = ? AND user_id = ?", (sid, user["id"]))
    return searches(conn, user["id"])


def stem(w: str) -> str:
    return w[:-2] if len(w) > 5 else w[:-1] if len(w) > 4 else w


def notify_searches(conn: sqlite3.Connection, gig: dict) -> None:
    """Новое объявление: уведомить подписчиков, чьи условия оно подходит (кроме самого продавца)."""
    hay = " ".join([gig["title"], gig["desc"], gig["sub"], " ".join(gig["tags"])]).lower().replace("ё", "е")
    for s in conn.execute("SELECT * FROM saved_searches WHERE user_id != ?", (gig.get("owner") or "",)).fetchall():
        if s["cat"] and s["cat"] != gig["cat"] or s["sub"] and s["sub"] != gig["sub"] or gig["base"] > s["max"]:
            continue
        words = [w for w in re.split(r"[^a-zа-я0-9+#]+", s["q"].lower().replace("ё", "е")) if len(w) > 1]
        if all(stem(w) in hay for w in words):
            notify(conn, s["user_id"], "search", f"Новое объявление по вашей подписке: «{gig['title']}», от {rub(gig['base'])}",
                   f"#/gig/{gig['id']}")


# ---------- Портфолио ----------

def portfolio(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, gig_id, name, title, ts FROM portfolio WHERE user_id = ? ORDER BY ts DESC", (user_id,))]


@router.get("/api/portfolio/{user_id}")
def get_portfolio(user_id: str, conn: sqlite3.Connection = Depends(get_db)):
    return portfolio(conn, user_id)


@router.post("/api/portfolio")
async def add_portfolio(request: Request, title: str = "", gig: str = "", user=Depends(auth.require_user),
                        conn: sqlite3.Connection = Depends(get_db)):
    """Работа в портфолио: картинка (браузер заранее сжимает её) и подпись; можно привязать к своему объявлению."""
    data = await request.body()
    if not data or len(data) > COVER_MAX:
        raise HTTPException(400, "Картинка должна быть не больше 3 МБ")
    ext = cover_ext(data, "Работа")
    if conn.execute("SELECT count(*) FROM portfolio WHERE user_id = ?", (user["id"],)).fetchone()[0] >= PORTFOLIO_MAX:
        raise HTTPException(400, f"В портфолио может быть не больше {PORTFOLIO_MAX} работ")
    if gig and not conn.execute("SELECT 1 FROM gigs WHERE id = ? AND owner_id = ?", (gig, user["id"])).fetchone():
        gig = ""
    name = f"{user['id']}-{secrets.token_hex(4)}{ext}"
    (UPLOAD_DIR / "portfolio" / name).write_bytes(data)
    conn.execute("INSERT INTO portfolio(id, user_id, gig_id, name, title, ts) VALUES (?, ?, ?, ?, ?, ?)",
                 (new_id("pf"), user["id"], gig or None, name, esc(title.strip())[:80], now_ms()))
    return portfolio(conn, user["id"])


@router.delete("/api/portfolio/{pid}")
def delete_portfolio(pid: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    r = conn.execute("SELECT name FROM portfolio WHERE id = ? AND user_id = ?", (pid, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Работа не найдена")
    conn.execute("DELETE FROM portfolio WHERE id = ?", (pid,))
    if NAME_RE.match(r["name"]):
        (UPLOAD_DIR / "portfolio" / r["name"]).unlink(missing_ok=True)
    return portfolio(conn, user["id"])


@router.get("/media/portfolio/{name}", include_in_schema=False)
def portfolio_file(name: str):
    path = UPLOAD_DIR / "portfolio" / name
    if not NAME_RE.match(name) or not path.is_file():
        raise HTTPException(404, "Не найдено")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable",
                                       "X-Content-Type-Options": "nosniff"})


# ---------- Просмотры и статистика продавца ----------

@router.post("/api/gigs/{gig_id}/view")
def count_view(gig_id: str, request: Request, response: Response, user=Depends(auth.optional_user),
               conn: sqlite3.Connection = Depends(get_db)):
    """Просмотр страницы объявления: один зритель считается один раз в сутки, свои просмотры не считаются."""
    g = conn.execute("SELECT owner_id FROM gigs WHERE id = ?", (gig_id,)).fetchone()
    if g and g["owner_id"] and not (user and user["id"] == g["owner_id"]):
        conn.execute("INSERT OR IGNORE INTO gig_views(gig_id, viewer, day) VALUES (?, ?, ?)",
                     (gig_id, chat_owner(request, response, user), now_ms() // DAY_MS))
    return {"ok": True}


@router.get("/api/me/stats")
def my_stats(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Уровень с условиями следующего и воронка по каждому объявлению: просмотры → избранное → чат → заказы."""
    day = now_ms() // DAY_MS
    gigs = []
    for g in conn.execute("SELECT id, title, base, created_at FROM gigs WHERE owner_id = ? ORDER BY created_at DESC", (user["id"],)):
        q = lambda sql, *a: conn.execute(sql, (g["id"], *a)).fetchone()[0]
        views = q("SELECT count(*) FROM gig_views WHERE gig_id = ?")
        orders = q("SELECT count(*) FROM orders WHERE gig_id = ?")
        gigs.append({
            "id": g["id"], "title": g["title"], "views": views,
            "views30": q("SELECT count(*) FROM gig_views WHERE gig_id = ? AND day > ?", day - 30),
            "favs": q("SELECT count(*) FROM favorites WHERE gig_id = ?"),
            "chats": q("SELECT count(DISTINCT owner) FROM chat_messages WHERE gig_id = ? AND role = 'me'"),
            "orders": orders, "done": q("SELECT count(*) FROM orders WHERE gig_id = ? AND status = ?", ST_DONE),
            "cancel": q("SELECT count(*) FROM orders WHERE gig_id = ? AND status = ?", ST_CANCEL),
            "revenue": q("SELECT coalesce(sum(amount), 0) FROM orders WHERE gig_id = ? AND status = ?", ST_DONE),
            "conv": round(100 * orders / views, 1) if views else None,
        })
    return {"level": seller_level(conn, user["id"], full=True), "gigs": gigs}


# ---------- Режим «В отпуске» ----------

class VacationIn(BaseModel):
    until: int | None = None
    note: str = ""


@router.patch("/api/me/vacation")
def set_vacation(body: VacationIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """until — до какого момента (мс) продавец не принимает заказы; None — выключить режим.
    Уже оформленные заказы не затрагиваются."""
    n = now_ms()
    if body.until is not None and not n < body.until <= n + VACATION_MAX_MS:
        raise HTTPException(400, "Выберите дату возвращения в пределах 90 дней")
    conn.execute("UPDATE users SET vacation_until = ?, vacation_note = ? WHERE id = ?",
                 (body.until, esc(body.note.strip())[:200] if body.until else "", user["id"]))
    return {"me": auth.public_user(conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())}
