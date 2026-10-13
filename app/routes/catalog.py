"""Справочники, каталог услуг и объявления пользователей."""

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from ..db import SEED, get_db, now_ms
from ..logic import CATS, MINP, SUBS, esc, gig_out, rub

router = APIRouter(prefix="/api")


class GigIn(BaseModel):
    cat: str
    sub: str
    title: str
    desc: str
    base: float
    tags: str = ""


@router.get("/meta")
def meta(conn: sqlite3.Connection = Depends(get_db)):
    sellers = {
        r["name"]: [r["about"], json.loads(r["reviews_json"])]
        for r in conn.execute("SELECT * FROM sellers")
    }
    return {"cats": SEED["cats"], "subs": SUBS, "minp": MINP, "syn": SEED["syn"], "sellers": sellers}


@router.get("/gigs")
def gigs(conn: sqlite3.Connection = Depends(get_db)):
    # Объявления заблокированных пользователей скрыты из каталога до разблокировки.
    rows = conn.execute(
        "SELECT g.* FROM gigs g LEFT JOIN users u ON u.id = g.owner_id WHERE u.blocked_at IS NULL"
        " ORDER BY g.owner_id IS NOT NULL, g.created_at, g.id"
    ).fetchall()
    return [gig_out(r) for r in rows]


@router.post("/gigs")
def create_gig(body: GigIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    title, desc = esc(body.title.strip())[:200], esc(body.desc.strip())[:1000]
    base = round(body.base)
    if body.cat not in CATS or body.sub not in SUBS.get(body.cat, []):
        raise HTTPException(400, "Выберите категорию и подкатегорию")
    if not title or not desc or not 100 <= base <= 1_000_000:
        raise HTTPException(400, "Проверьте название и цену")
    if base < MINP.get(body.sub, 0):
        raise HTTPException(400, f"Минимальная цена в подкатегории «{body.sub}»: {rub(MINP[body.sub])}")
    tags = [t.strip().lower() for t in esc(body.tags).split(",") if t.strip()][:5] or [body.sub.lower()]
    gig_id = f"u-{now_ms()}"
    conn.execute(
        "INSERT INTO gigs(id, cat, sub, title, base, rating, rev, seller, lvl, descr, top, tags_json, owner_id,"
        " details_json, created_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 'Новичок', ?, 0, ?, ?, NULL, ?)",
        (gig_id, body.cat, body.sub, title, base, user["name"], desc, json.dumps(tags, ensure_ascii=False),
         user["id"], now_ms()),
    )
    return gig_out(conn.execute("SELECT * FROM gigs WHERE id = ?", (gig_id,)).fetchone())


@router.delete("/gigs/{gig_id}")
def delete_gig(gig_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    cur = conn.execute("DELETE FROM gigs WHERE id = ? AND owner_id = ?", (gig_id, user["id"]))
    if not cur.rowcount:
        raise HTTPException(404, "Объявление не найдено")
    return {"ok": True}
