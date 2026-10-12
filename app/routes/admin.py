"""Админ-панель: проверка заявок на пополнение и вывод, модерация объявлений. Доступна только ADMIN_EMAIL."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from .. import auth
from ..db import get_db, now_ms, tx
from ..logic import add_coins, gig_out

router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])

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
        elif act == "reject" and o["t"] == "wd":
            add_coins(conn, o["user_id"], o["coins"])  # монеты были удержаны при создании заявки
        status = "done" if act == "approve" else "cancelled"
        conn.execute("UPDATE wallet_ops SET status = ? WHERE id = ?", (status, op_id))
    return ops(conn)


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
        "SELECT u.id, u.name, u.email, u.created_at, u.last_seen,"
        " COALESCE((SELECT coins FROM wallets w WHERE w.user_id = u.id), 0) AS coins,"
        " (SELECT count(*) FROM orders o WHERE o.user_id = u.id) AS orders,"
        " (SELECT count(*) FROM gigs g WHERE g.owner_id = u.id) AS gigs"
        " FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    since = now_ms() - auth.ONLINE_MS
    return [
        {**dict(r), "online": (r["last_seen"] or 0) >= since, "admin": r["email"] == auth.ADMIN_EMAIL}
        for r in rows
    ]


@router.get("/gigs")
def gigs(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT g.*, u.email AS owner_email FROM gigs g LEFT JOIN users u ON u.id = g.owner_id"
        " ORDER BY g.owner_id IS NULL, g.created_at DESC, g.id"
    ).fetchall()
    return [{**gig_out(r), "owner_email": r["owner_email"]} for r in rows]


@router.delete("/gigs/{gig_id}")
def delete_gig(gig_id: str, conn: sqlite3.Connection = Depends(get_db)):
    with tx(conn):
        if not conn.execute("DELETE FROM gigs WHERE id = ?", (gig_id,)).rowcount:
            raise HTTPException(404, "Объявление не найдено")
        conn.execute("DELETE FROM chat_messages WHERE gig_id = ?", (gig_id,))
    return {"ok": True}
