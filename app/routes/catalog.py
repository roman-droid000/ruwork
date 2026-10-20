"""Справочники, каталог услуг и объявления пользователей."""

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, tg
from ..db import SEED, get_db, now_ms, tx
from ..logic import (
    BUMP_MS, CATS, MINP, PROMO_MS, PROMO_PRICE, SALE_FEE_PCT, SBP_ORDERS, SOLD_SQL, SUBS, add_coins, add_op, esc, get_coins,
    gig_is_top, gig_out, platform_add, response_times, rub, seller_ratings, user_state,
)
from .orders import remove_cover

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
    return {"cats": SEED["cats"], "subs": SUBS, "minp": MINP, "syn": SEED["syn"], "sellers": sellers,
            "sbp_orders": SBP_ORDERS, "sale_fee_pct": SALE_FEE_PCT, "tg_on": bool(tg.TOKEN), "tg_bot": tg.username,
            "promo_price": PROMO_PRICE, "bump_ms": BUMP_MS}


@router.get("/gigs")
def gigs(conn: sqlite3.Connection = Depends(get_db)):
    # Объявления заблокированных пользователей скрыты из каталога до разблокировки.
    rows = conn.execute(
        f"SELECT g.*, {SOLD_SQL}, u.last_seen AS owner_seen, u.verified AS owner_verified"
        " FROM gigs g LEFT JOIN users u ON u.id = g.owner_id WHERE u.blocked_at IS NULL"
        " ORDER BY g.owner_id IS NOT NULL, g.created_at, g.id"
    ).fetchall()
    return [with_seller(gig_out(r), r, seller_ratings(conn), response_times(conn)) for r in rows]


def with_seller(g: dict, r: sqlite3.Row, ratings: dict, resp: dict) -> dict:
    """Сведения о продавце для карточки: рейтинг по всем объявлениям, «в сети», время ответа, проверка."""
    g["srating"], g["srev"] = ratings.get(r["owner_id"] or "n:" + r["seller"], (0, 0))
    if r["owner_id"]:
        g["online"] = (r["owner_seen"] or 0) >= now_ms() - auth.ONLINE_MS
        g["resp"] = resp.get(r["owner_id"])
        g["verified"] = bool(r["owner_verified"])
    return g


def clean_gig(body: GigIn) -> tuple[str, str, int, str]:
    """Проверяет поля объявления. Возвращает (название, описание, цена, теги в JSON)."""
    title, desc = esc(body.title.strip())[:200], esc(body.desc.strip())[:1000]
    base = round(body.base)
    if body.cat not in CATS or body.sub not in SUBS.get(body.cat, []):
        raise HTTPException(400, "Выберите категорию и подкатегорию")
    if not title or not desc or not 100 <= base <= 1_000_000:
        raise HTTPException(400, "Проверьте название и цену")
    if base < MINP.get(body.sub, 0):
        raise HTTPException(400, f"Минимальная цена в подкатегории «{body.sub}»: {rub(MINP[body.sub])}")
    tags = [t.strip().lower() for t in esc(body.tags).split(",") if t.strip()][:5] or [body.sub.lower()]
    return title, desc, base, json.dumps(tags, ensure_ascii=False)


@router.post("/gigs")
def create_gig(body: GigIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    title, desc, base, tags = clean_gig(body)
    gig_id = f"u-{now_ms()}"
    conn.execute(
        "INSERT INTO gigs(id, cat, sub, title, base, rating, rev, seller, lvl, descr, top, tags_json, owner_id,"
        " details_json, created_at, seller_avatar) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 'Новичок', ?, 0, ?, ?, NULL, ?, ?)",
        (gig_id, body.cat, body.sub, title, base, user["name"], desc, tags, user["id"], now_ms(), user["avatar"]),
    )
    return gig_out(conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.id = ?", (gig_id,)).fetchone())


@router.put("/gigs/{gig_id}")
def update_gig(gig_id: str, body: GigIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Редактирование своего объявления. Уже оформленные заказы не меняются: у них своя цена и название.
    У объявления в «ТОП» меняется только цена: название, описание, категория, теги и обложка закреплены."""
    cur = conn.execute("SELECT sub FROM gigs WHERE id = ? AND owner_id = ?", (gig_id, user["id"])).fetchone()
    if not cur:
        raise HTTPException(404, "Объявление не найдено")
    if gig_is_top(conn, gig_id):
        base = round(body.base)
        if not 100 <= base <= 1_000_000 or base < MINP.get(cur["sub"], 0):
            raise HTTPException(400, f"Минимальная цена в подкатегории «{cur['sub']}»: {rub(max(100, MINP.get(cur['sub'], 0)))}")
        conn.execute("UPDATE gigs SET base = ? WHERE id = ?", (base, gig_id))
        return gig_out(conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.id = ?", (gig_id,)).fetchone())
    title, desc, base, tags = clean_gig(body)
    if not conn.execute(
        "UPDATE gigs SET cat = ?, sub = ?, title = ?, base = ?, descr = ?, tags_json = ? WHERE id = ? AND owner_id = ?",
        (body.cat, body.sub, title, base, desc, tags, gig_id, user["id"]),
    ).rowcount:
        raise HTTPException(404, "Объявление не найдено")
    return gig_out(conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.id = ?", (gig_id,)).fetchone())


@router.delete("/gigs/{gig_id}")
def delete_gig(gig_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    g = conn.execute("SELECT cover FROM gigs WHERE id = ? AND owner_id = ?", (gig_id, user["id"])).fetchone()
    if not g:
        raise HTTPException(404, "Объявление не найдено")
    conn.execute("DELETE FROM gigs WHERE id = ?", (gig_id,))
    remove_cover(g["cover"])
    return {"ok": True}


def own_gig(conn: sqlite3.Connection, gig_id: str, user) -> sqlite3.Row:
    g = conn.execute("SELECT * FROM gigs WHERE id = ? AND owner_id = ?", (gig_id, user["id"])).fetchone()
    if not g:
        raise HTTPException(404, "Объявление не найдено")
    return g


def gig_full(conn: sqlite3.Connection, gig_id: str) -> dict:
    return gig_out(conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.id = ?", (gig_id,)).fetchone())


@router.post("/gigs/{gig_id}/bump")
def bump_gig(gig_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Бесплатное поднятие: объявление встаёт первым среди обычных, не чаще раза в сутки."""
    g = own_gig(conn, gig_id, user)
    wait = (g["bumped_at"] or 0) + BUMP_MS - now_ms()
    if wait > 0:
        h, m = divmod(-(-wait // 60_000), 60)
        raise HTTPException(400, f"Поднять снова можно через {h} ч {m} мин" if h else f"Поднять снова можно через {m} мин")
    conn.execute("UPDATE gigs SET bumped_at = ? WHERE id = ?", (now_ms(), gig_id))
    return gig_full(conn, gig_id)


@router.post("/gigs/{gig_id}/promote")
def promote_gig(gig_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Платное продвижение на сутки: объявление показывается выше всех с меткой «Продвигается».
    Повторная покупка продлевает срок. Монеты уходят на баланс платформы."""
    with tx(conn):
        g = own_gig(conn, gig_id, user)
        if get_coins(conn, user["id"]) < PROMO_PRICE:
            raise HTTPException(400, f"Недостаточно монет: продвижение стоит {PROMO_PRICE} мон. Пополните баланс в кошельке.")
        n = now_ms()
        add_coins(conn, user["id"], -PROMO_PRICE)
        add_op(conn, user["id"], "promo", "bal", PROMO_PRICE, "done", g["title"])
        platform_add(conn, "promo", PROMO_PRICE, user["id"], None, g["title"])
        conn.execute("UPDATE gigs SET promo_until = ?, bumped_at = ? WHERE id = ?",
                     (max(n, g["promo_until"] or 0) + PROMO_MS, n, gig_id))
        return {"gig": gig_full(conn, gig_id), **user_state(conn, user["id"])}
