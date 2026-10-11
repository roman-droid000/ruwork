"""Админ-панель: проверка заявок на пополнение и вывод, модерация объявлений. Доступна только ADMIN_EMAIL."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from .. import auth
from ..db import get_db, tx
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
