"""Админ-панель: проверка заявок на пополнение и вывод, модерация объявлений. Доступна только ADMIN_EMAIL."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from ..db import get_db, now_ms, tx
from ..logic import SOLD_SQL, ST_DISPUTE, add_coins, gig_out, nf, notify, platform_add, resolve_dispute
from .orders import remove_cover

router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])


class BlockIn(BaseModel):
    reason: str = ""

OPS_SQL = (
    "SELECT o.id, o.t, o.coins, o.fee, o.status, o.dest, o.ts, u.name, u.email"
    " FROM wallet_ops o JOIN users u ON u.id = o.user_id WHERE o.t IN ('dep', 'wd')"
)


@router.get("/ops")
def ops(conn: sqlite3.Connection = Depends(get_db)):
    pending = conn.execute(OPS_SQL + " AND o.status = 'pending' ORDER BY o.ts").fetchall()
    done = conn.execute(OPS_SQL + " AND o.status != 'pending' ORDER BY o.ts DESC LIMIT 100").fetchall()
    return [dict(r) for r in pending + done]


@router.post("/ops/{op_id}/{act}")
def op_decide(op_id: str, act: str, conn: sqlite3.Connection = Depends(get_db)):
    if act not in ("approve", "reject"):
        raise HTTPException(400, "Неизвестное действие")
    with tx(conn):
        o = conn.execute("SELECT * FROM wallet_ops WHERE id = ? AND t IN ('dep', 'wd')", (op_id,)).fetchone()
        if not o or o["status"] != "pending":
            raise HTTPException(400, "Заявка уже обработана")
        if act == "approve" and o["t"] == "dep":
            add_coins(conn, o["user_id"], o["coins"])
        elif act == "approve" and o["t"] == "wd":
            platform_add(conn, "wd_fee", o["fee"], o["user_id"], None, o["dest"])  # комиссия за вывод
        elif act == "reject" and o["t"] == "wd":
            add_coins(conn, o["user_id"], o["coins"])  # монеты были удержаны при создании заявки
        status = "done" if act == "approve" else "cancelled"
        conn.execute("UPDATE wallet_ops SET status = ? WHERE id = ?", (status, op_id))
        what = "пополнение" if o["t"] == "dep" else "вывод"
        notify(conn, o["user_id"], "wallet", f"Заявка на {what} {nf(o['coins'])} мон. " + ("одобрена" if act == "approve" else "отклонена")
               + (" — монеты возвращены на баланс" if act == "reject" and o["t"] == "wd" else "") + ".", "#/dashboard/wallet")
    return ops(conn)


@router.get("/platform")
def platform(conn: sqlite3.Connection = Depends(get_db)):
    """Баланс платформы: комиссии за вывод и с продаж, доля удержания при отмене заказов «В работе»."""
    by = {r["kind"]: r["s"] for r in conn.execute("SELECT kind, sum(coins) AS s FROM platform_ops GROUP BY kind")}
    ops = conn.execute(
        "SELECT p.kind, p.coins, p.note, p.ts, u.name FROM platform_ops p LEFT JOIN users u ON u.id = p.user_id"
        " ORDER BY p.id DESC LIMIT 30"
    ).fetchall()
    return {"coins": sum(by.values()), "wd_fee": by.get("wd_fee", 0), "cancel": by.get("cancel", 0), "sale_fee": by.get("sale_fee", 0),
            "promo": by.get("promo", 0),
            "ops": [dict(r) for r in ops]}


RESETS_SQL = (
    "SELECT r.id, r.contact, r.status, r.ts, u.name, u.email"
    " FROM password_resets r JOIN users u ON u.id = r.user_id"
)


@router.get("/resets")
def resets(conn: sqlite3.Connection = Depends(get_db)):
    pending = conn.execute(RESETS_SQL + " WHERE r.status = 'pending' ORDER BY r.ts").fetchall()
    done = conn.execute(RESETS_SQL + " WHERE r.status != 'pending' ORDER BY r.ts DESC LIMIT 50").fetchall()
    return [dict(r) for r in pending + done]


@router.post("/resets/{reset_id}/{act}")
def reset_decide(reset_id: str, act: str, conn: sqlite3.Connection = Depends(get_db)):
    if act not in ("approve", "reject"):
        raise HTTPException(400, "Неизвестное действие")
    with tx(conn):
        r = conn.execute("SELECT * FROM password_resets WHERE id = ?", (reset_id,)).fetchone()
        if not r or r["status"] != "pending":
            raise HTTPException(400, "Заявка уже обработана")
        if act == "approve":
            conn.execute("UPDATE users SET salt = ?, hash = ? WHERE id = ?", (r["salt"], r["hash"], r["user_id"]))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (r["user_id"],))  # выход на всех устройствах
        conn.execute("UPDATE password_resets SET status = ? WHERE id = ?",
                     ("done" if act == "approve" else "cancelled", reset_id))
    return resets(conn)


@router.get("/users")
def users(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT u.id, u.name, u.email, u.created_at, u.last_seen, u.blocked_at, u.block_reason, u.verified,"
        " COALESCE((SELECT coins FROM wallets w WHERE w.user_id = u.id), 0) AS coins,"
        " (SELECT count(*) FROM orders o WHERE o.user_id = u.id) AS orders,"
        " (SELECT count(*) FROM gigs g WHERE g.owner_id = u.id) AS gigs"
        " FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    since = now_ms() - auth.ONLINE_MS
    return [
        {**dict(r), "online": r["blocked_at"] is None and (r["last_seen"] or 0) >= since,
         "admin": r["email"] == auth.ADMIN_EMAIL}
        for r in rows
    ]


DEALS_SQL = (
    "SELECT o.id, o.num, o.gig_id, o.title, o.tier_name AS tier, o.status, o.amount, o.method, o.created_at,"
    " (SELECT count(*) FROM order_files f WHERE f.order_id = o.id AND f.done = 1) AS files,"
    " b.name AS buyer_name, s.name AS seller_name FROM orders o JOIN users b ON b.id = o.user_id"
    " LEFT JOIN users s ON s.id = o.seller_id"
)


def deal(r: sqlite3.Row) -> dict:
    return dict(r)


@router.get("/users/{user_id}")
def user_profile(user_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Профиль для проверки: сделки пользователя как продавца и как покупателя, его объявления."""
    u = next((x for x in users(conn) if x["id"] == user_id), None)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    sales = conn.execute(DEALS_SQL + " WHERE o.seller_id = ? ORDER BY o.created_at DESC LIMIT 200", (user_id,))
    buys = conn.execute(DEALS_SQL + " WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT 200", (user_id,))
    gigs = conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.owner_id = ? ORDER BY g.created_at DESC", (user_id,))
    return {"user": u, "sales": [deal(r) for r in sales], "purchases": [deal(r) for r in buys],
            "gigs": [gig_out(r) for r in gigs]}


@router.post("/users/{user_id}/block")
def block_user(user_id: str, body: BlockIn, conn: sqlite3.Connection = Depends(get_db)):
    with tx(conn):
        u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not u:
            raise HTTPException(404, "Пользователь не найден")
        if auth.is_admin(u):
            raise HTTPException(400, "Нельзя заблокировать администратора")
        conn.execute("UPDATE users SET blocked_at = ?, block_reason = ? WHERE id = ?",
                     (now_ms(), body.reason.strip()[:200], user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # выход на всех устройствах
    return users(conn)


@router.post("/users/{user_id}/unblock")
def unblock_user(user_id: str, conn: sqlite3.Connection = Depends(get_db)):
    if not conn.execute("UPDATE users SET blocked_at = NULL, block_reason = '' WHERE id = ?", (user_id,)).rowcount:
        raise HTTPException(404, "Пользователь не найден")
    return users(conn)


@router.post("/users/{user_id}/verify/{on}")
def verify_user(user_id: str, on: int, conn: sqlite3.Connection = Depends(get_db)):
    """Значок «Проверенный продавец» ставит и снимает администратор."""
    if not conn.execute("UPDATE users SET verified = ? WHERE id = ?", (1 if on else 0, user_id)).rowcount:
        raise HTTPException(404, "Пользователь не найден")
    if on:
        notify(conn, user_id, "verify", "Администратор отметил вас как проверенного продавца: значок ✓ виден на ваших объявлениях.",
               "#/seller/" + user_id)
    return users(conn)


@router.get("/gigs")
def gigs(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        f"SELECT g.*, {SOLD_SQL}, u.email AS owner_email, u.blocked_at IS NOT NULL AS owner_blocked"
        " FROM gigs g LEFT JOIN users u ON u.id = g.owner_id"
        " ORDER BY g.owner_id IS NULL, g.created_at DESC, g.id"
    ).fetchall()
    return [{**gig_out(r), "owner_email": r["owner_email"], "owner_blocked": bool(r["owner_blocked"])} for r in rows]


@router.delete("/gigs/{gig_id}")
def delete_gig(gig_id: str, conn: sqlite3.Connection = Depends(get_db)):
    with tx(conn):
        g = conn.execute("SELECT cover FROM gigs WHERE id = ?", (gig_id,)).fetchone()
        if not g:
            raise HTTPException(404, "Объявление не найдено")
        conn.execute("DELETE FROM gigs WHERE id = ?", (gig_id,))
        conn.execute("DELETE FROM chat_messages WHERE gig_id = ?", (gig_id,))
    remove_cover(g["cover"])
    return {"ok": True}


DISPUTES_SQL = DEALS_SQL.replace(
    "SELECT o.id,", "SELECT o.dispute_at, o.dispute_reason, o.dispute_result, o.confirmed_at, o.id,", 1
) + " WHERE o.dispute_at IS NOT NULL"


@router.get("/disputes")
def disputes(conn: sqlite3.Connection = Depends(get_db)):
    """Открытые споры (сначала старые) и 50 последних решённых."""
    open_ = conn.execute(DISPUTES_SQL + " AND o.status = ? ORDER BY o.dispute_at", (ST_DISPUTE,)).fetchall()
    done = conn.execute(DISPUTES_SQL + " AND o.status != ? ORDER BY o.confirmed_at DESC LIMIT 50", (ST_DISPUTE,)).fetchall()
    return [dict(r) for r in open_ + done]


@router.post("/disputes/{order_id}/{act}")
def dispute_decide(order_id: str, act: str, conn: sqlite3.Connection = Depends(get_db)):
    if act not in ("seller", "buyer", "split"):
        raise HTTPException(400, "Неизвестное решение")
    with tx(conn):
        o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not o or o["status"] != ST_DISPUTE:
            raise HTTPException(400, "Спор уже закрыт")
        resolve_dispute(conn, o, act)
    return disputes(conn)
