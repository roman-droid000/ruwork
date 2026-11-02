"""Справочники, каталог услуг и объявления пользователей."""

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, guard, tg
from ..db import SEED, get_db, now_ms, tx
from ..logic import (
    BUMP_MS, CATS, DEMO_CATALOG, WELCOME_BONUS, PREMOD_FIRST, setting, MINP, PROMO_MS, PROMO_PRICE, SALE_FEE_PCT, SBP_ORDERS, SOLD_SQL, SUBS, VISIBLE_SQL, add_op, esc, get_coins,
    gig_is_top, gig_out, platform_add, response_times, rub, seller_level, seller_ratings, spend, user_state,
)
from .market import notify_searches
from .orders import remove_cover
from .reports import REASONS

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
    } if DEMO_CATALOG else {}
    return {"cats": SEED["cats"], "subs": SUBS, "minp": MINP, "syn": SEED["syn"], "sellers": sellers,
            "sbp_orders": SBP_ORDERS, "sale_fee_pct": SALE_FEE_PCT, "tg_on": bool(tg.TOKEN), "tg_bot": tg.username,
            "promo_price": PROMO_PRICE, "welcome_bonus": WELCOME_BONUS, "bump_ms": BUMP_MS, "report_reasons": REASONS,
            "reg_tg": bool(tg.TOKEN)}


@router.get("/gigs")
def gigs(user=Depends(auth.optional_user), conn: sqlite3.Connection = Depends(get_db)):
    # Объявления заблокированных пользователей скрыты из каталога до разблокировки. Свои объявления
    # на проверке или скрытые администратором автор получает тоже — с полями mod и mod_note.
    rows = conn.execute(
        f"SELECT g.*, {SOLD_SQL}, u.last_seen AS owner_seen, u.verified AS owner_verified, u.vacation_until AS owner_away"
        f" FROM gigs g LEFT JOIN users u ON u.id = g.owner_id WHERE ({VISIBLE_SQL}) OR g.owner_id = ?"
        " ORDER BY g.owner_id IS NOT NULL, g.created_at, g.id", (user["id"] if user else "",)
    ).fetchall()
    return [with_seller(conn, gig_out(r), r, seller_ratings(conn), response_times(conn)) for r in rows]


def with_seller(conn: sqlite3.Connection, g: dict, r: sqlite3.Row, ratings: dict, resp: dict) -> dict:
    """Сведения о продавце для карточки: рейтинг по всем объявлениям, «в сети», время ответа, проверка."""
    g["srating"], g["srev"] = ratings.get(r["owner_id"] or "n:" + r["seller"], (0, 0))
    if r["owner_id"]:
        g["online"] = (r["owner_seen"] or 0) >= now_ms() - auth.ONLINE_MS
        g["resp"] = resp.get(r["owner_id"])
        g["verified"] = bool(r["owner_verified"])
        g["away"] = r["owner_away"] if (r["owner_away"] or 0) > now_ms() else None
        g["level"] = seller_level(conn, r["owner_id"])[1]
    return g


def clean_gig(body: GigIn) -> tuple[str, str, int, str]:
    """Проверяет поля объявления. Возвращает (название, описание, цена, теги в JSON)."""
    guard.check_banned(body.title, body.desc, body.tags)
    title = esc(guard.mask_contacts(body.title.strip()))[:200]
    desc = esc(guard.mask_contacts(body.desc.strip()))[:1000]
    base = round(body.base)
    if body.cat not in CATS or body.sub not in SUBS.get(body.cat, []):
        raise HTTPException(400, "Выберите категорию и подкатегорию")
    if not title or not desc or not 100 <= base <= 1_000_000:
        raise HTTPException(400, "Проверьте название и цену")
    if base < MINP.get(body.sub, 0):
        raise HTTPException(400, f"Минимальная цена в подкатегории «{body.sub}»: {rub(MINP[body.sub])}")
    tags = [t.strip().lower() for t in esc(body.tags).split(",") if t.strip()][:5] or [body.sub.lower()]
    return title, desc, base, json.dumps(tags, ensure_ascii=False)


def needs_premod(conn: sqlite3.Connection, user) -> bool:
    """Премодерация включена в админке: первые PREMOD_FIRST объявлений непроверенного продавца ждут проверки."""
    if setting(conn, "premod", "0") != "1" or user["verified"] or auth.staff_role(user):
        return False
    ok = conn.execute("SELECT count(*) FROM gigs WHERE owner_id = ? AND mod = 'ok'", (user["id"],)).fetchone()[0]
    return ok < PREMOD_FIRST


@router.post("/gigs")
def create_gig(body: GigIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    title, desc, base, tags = clean_gig(body)
    gig_id = f"u-{now_ms()}"
    mod = "pending" if needs_premod(conn, user) else "ok"
    conn.execute(
        "INSERT INTO gigs(id, cat, sub, title, base, rating, rev, seller, lvl, descr, top, tags_json, owner_id,"
        " details_json, created_at, seller_avatar, mod) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 'Новичок', ?, 0, ?, ?, NULL, ?, ?, ?)",
        (gig_id, body.cat, body.sub, title, base, user["name"], desc, tags, user["id"], now_ms(), user["avatar"], mod),
    )
    g = gig_out(conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.id = ?", (gig_id,)).fetchone())
    if mod == "ok":
        notify_searches(conn, g)
    else:
        guard.alert(f"Новое объявление на проверке: «{title}» от {user['name']}", "#/admin/gigs")
    return g


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
    # Скрытое администратором объявление после исправления снова уходит на проверку.
    if conn.execute("UPDATE gigs SET mod = 'pending' WHERE id = ? AND mod = 'hidden'", (gig_id,)).rowcount:
        guard.alert(f"Исправленное объявление ждёт проверки: «{title}» от {user['name']}", "#/admin/gigs")
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
    """Платное продвижение на неделю (PROMO_MS): объявление показывается выше всех с меткой «Продвигается».
    Повторная покупка продлевает срок. Монеты уходят на баланс платформы."""
    with tx(conn):
        g = own_gig(conn, gig_id, user)
        if get_coins(conn, user["id"]) < PROMO_PRICE:
            raise HTTPException(400, f"Недостаточно монет: продвижение стоит {PROMO_PRICE} мон. Пополните баланс в кошельке.")
        n = now_ms()
        spend(conn, user["id"], PROMO_PRICE)
        add_op(conn, user["id"], "promo", "bal", PROMO_PRICE, "done", g["title"])
        platform_add(conn, "promo", PROMO_PRICE, user["id"], None, g["title"])
        conn.execute("UPDATE gigs SET promo_until = ?, bumped_at = ? WHERE id = ?",
                     (max(n, g["promo_until"] or 0) + PROMO_MS, n, gig_id))
        return {"gig": gig_full(conn, gig_id), **user_state(conn, user["id"])}
