"""Админ-панель: заявки на пополнение и вывод, сброс паролей, пользователи, объявления и споры.

Доступ — сотрудникам (auth.require_staff: роль и код из Telegram в этой сессии). Деньги, заявки на сброс
пароля и баланс платформы — только владельцу (auth.require_admin). Каждое действие пишется в admin_log.
Сводка, журнал, поиск, графики и прочие инструменты — в admin_tools.py.
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from ..db import get_db, now_ms, tx
from ..logic import (
    DAY_MS, SOLD_SQL, ST_DISPUTE, ST_DONE, add_coins, audit, gig_out, nf, notify, platform_add, resolve_dispute,
)
from .market import notify_searches
from .orders import remove_cover

router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_staff)])
admin_only = [Depends(auth.require_admin)]

BLOCK_DAYS = (0, 1, 7, 30)  # 0 — бессрочно
SHARED_IP_MAX = 10  # адрес, с которого заходят больше пользователей, — скорее всего общий (оператор, офис), не улика


class BlockIn(BaseModel):
    reason: str = ""
    days: int = 0


class HideIn(BaseModel):
    reason: str


# ---------- Заявки: пополнения, возвраты, выводы ----------

OPS_SQL = (
    "SELECT o.id, o.user_id, o.t, o.coins, o.fee, o.status, o.dest, o.phone, o.ts, u.name, u.email"
    " FROM wallet_ops o JOIN users u ON u.id = o.user_id WHERE o.t IN ('dep', 'wd', 'rf')"
)


def wd_flags(conn: sqlite3.Connection, o: sqlite3.Row) -> list[str]:
    """Признаки риска у заявки на вывод — повод проверить внимательнее, а не отказ."""
    flags = []
    uid = o["user_id"]
    u = conn.execute("SELECT created_at FROM users WHERE id = ?", (uid,)).fetchone()
    if u and u["created_at"] > now_ms() - 7 * DAY_MS:
        flags.append("аккаунт моложе 7 дней")
    prev = conn.execute("SELECT count(*), avg(coins) FROM wallet_ops WHERE user_id = ? AND t = 'wd' AND status = 'done'",
                        (uid,)).fetchone()
    if not prev[0]:
        flags.append("первый вывод")
    elif o["coins"] > 3 * prev[1]:
        flags.append(f"сумма в {o['coins'] / prev[1]:.1f} раза больше обычной ({nf(round(prev[1]))} мон.)")
    buyers = [r[0] for r in conn.execute(
        "SELECT DISTINCT user_id FROM orders WHERE seller_id = ? AND status = ?", (uid, ST_DONE))]
    sold = conn.execute("SELECT count(*) FROM orders WHERE seller_id = ? AND status = ?", (uid, ST_DONE)).fetchone()[0]
    if len(buyers) == 1 and sold >= 2:
        flags.append(f"все {sold} продажи — одному покупателю")
    if buyers and conn.execute(
        f"SELECT 1 FROM user_ips a JOIN user_ips b ON a.ip = b.ip WHERE a.user_id = ?"
        f" AND b.user_id IN ({','.join('?' * len(buyers))}) AND b.user_id != a.user_id"
        " AND (SELECT count(DISTINCT user_id) FROM user_ips c WHERE c.ip = a.ip) <= ? LIMIT 1",
        (uid, *buyers, SHARED_IP_MAX),
    ).fetchone():
        flags.append("покупатель заходил с того же адреса, что продавец")
    if o["phone"] and conn.execute("SELECT 1 FROM wallet_ops WHERE t = 'wd' AND phone = ? AND user_id != ? LIMIT 1",
                                   (o["phone"], uid)).fetchone():
        flags.append("этот телефон указывал другой аккаунт")
    return flags


@router.get("/ops", dependencies=admin_only)
def ops(conn: sqlite3.Connection = Depends(get_db)):
    pending = conn.execute(OPS_SQL + " AND o.status = 'pending' ORDER BY o.ts").fetchall()
    done = conn.execute(OPS_SQL + " AND o.status != 'pending' ORDER BY o.ts DESC LIMIT 100").fetchall()
    return [{**dict(r), "flags": wd_flags(conn, r) if r["t"] == "wd" and r["status"] == "pending" else []}
            for r in pending + done]


@router.post("/ops/{op_id}/{act}", dependencies=admin_only)
def op_decide(op_id: str, act: str, staff=Depends(auth.require_admin), conn: sqlite3.Connection = Depends(get_db)):
    if act not in ("approve", "reject"):
        raise HTTPException(400, "Неизвестное действие")
    with tx(conn):
        o = conn.execute("SELECT * FROM wallet_ops WHERE id = ? AND t IN ('dep', 'wd', 'rf')", (op_id,)).fetchone()
        if not o or o["status"] != "pending":
            raise HTTPException(400, "Заявка уже обработана")
        if act == "approve" and o["t"] == "dep":
            add_coins(conn, o["user_id"], o["coins"])
        elif act == "approve" and o["t"] == "wd":
            platform_add(conn, "wd_fee", o["fee"], o["user_id"], None, o["dest"])  # комиссия за вывод
        elif act == "reject" and o["t"] in ("wd", "rf"):
            # Монеты были удержаны при создании заявки: вывод — из заработка, возврат на карту — из аванса.
            add_coins(conn, o["user_id"], o["coins"], o["coins"] if o["t"] == "wd" else 0)
        status = "done" if act == "approve" else "cancelled"
        conn.execute("UPDATE wallet_ops SET status = ? WHERE id = ?", (status, op_id))
        what = {"dep": "пополнение", "wd": "вывод", "rf": "возврат на карту"}[o["t"]]
        notify(conn, o["user_id"], "wallet", f"Заявка на {what} {nf(o['coins'])} мон. " + ("одобрена" if act == "approve" else "отклонена")
               + (" — монеты возвращены на баланс" if act == "reject" and o["t"] != "dep" else "") + ".", "#/dashboard/wallet")
        audit(conn, staff, f"op_{act}", f"{'Одобрена' if act == 'approve' else 'Отклонена'} заявка на {what}"
              + (f" ({o['dest']})" if o["dest"] else ""), o["user_id"], op_id, o["coins"])
    return ops(conn)


@router.get("/platform", dependencies=admin_only)
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


# ---------- Сброс пароля ----------

RESETS_SQL = (
    "SELECT r.id, r.contact, r.status, r.ts, u.name, u.email, r.user_id"
    " FROM password_resets r JOIN users u ON u.id = r.user_id"
)


@router.get("/resets", dependencies=admin_only)
def resets(conn: sqlite3.Connection = Depends(get_db)):
    pending = conn.execute(RESETS_SQL + " WHERE r.status = 'pending' ORDER BY r.ts").fetchall()
    done = conn.execute(RESETS_SQL + " WHERE r.status != 'pending' ORDER BY r.ts DESC LIMIT 50").fetchall()
    return [dict(r) for r in pending + done]


@router.post("/resets/{reset_id}/{act}", dependencies=admin_only)
def reset_decide(reset_id: str, act: str, staff=Depends(auth.require_admin), conn: sqlite3.Connection = Depends(get_db)):
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
        audit(conn, staff, f"reset_{act}", ("Одобрен" if act == "approve" else "Отклонён") + f" сброс пароля, контакт: {r['contact']}",
              r["user_id"], reset_id)
    return resets(conn)


# ---------- Пользователи ----------

@router.get("/users")
def users(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT u.id, u.name, u.email, u.created_at, u.last_seen, u.blocked_at, u.block_reason, u.blocked_until,"
        " u.verified, u.role, u.tg_chat IS NOT NULL AS tg,"
        " COALESCE((SELECT coins FROM wallets w WHERE w.user_id = u.id), 0) AS coins,"
        " (SELECT count(*) FROM orders o WHERE o.user_id = u.id) AS orders,"
        " (SELECT count(*) FROM gigs g WHERE g.owner_id = u.id) AS gigs"
        " FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    since = now_ms() - auth.ONLINE_MS
    return [
        {**dict(r), "tg": bool(r["tg"]), "online": r["blocked_at"] is None and (r["last_seen"] or 0) >= since,
         "admin": r["email"] == auth.ADMIN_EMAIL}
        for r in rows
    ]


DEALS_SQL = (
    "SELECT o.id, o.num, o.gig_id, o.title, o.tier_name AS tier, o.status, o.amount, o.method, o.created_at,"
    " (SELECT count(*) FROM order_files f WHERE f.order_id = o.id AND f.done = 1) AS files,"
    " b.name AS buyer_name, s.name AS seller_name, o.user_id AS buyer_id, o.seller_id FROM orders o JOIN users b ON b.id = o.user_id"
    " LEFT JOIN users s ON s.id = o.seller_id"
)


def deal(r: sqlite3.Row) -> dict:
    return dict(r)


def linked(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """Аккаунты, связанные с пользователем: общий адрес входа (кроме массовых) или общий телефон вывода."""
    why: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT b.user_id, a.ip FROM user_ips a JOIN user_ips b ON a.ip = b.ip AND b.user_id != a.user_id"
        " WHERE a.user_id = ? AND (SELECT count(DISTINCT user_id) FROM user_ips c WHERE c.ip = a.ip) <= ?",
        (user_id, SHARED_IP_MAX),
    ):
        why.setdefault(r[0], []).append(f"адрес {r[1]}")
    for r in conn.execute(
        "SELECT DISTINCT b.user_id, b.phone FROM wallet_ops a JOIN wallet_ops b ON a.phone = b.phone AND b.user_id != a.user_id"
        " WHERE a.user_id = ? AND a.phone IS NOT NULL", (user_id,),
    ):
        why.setdefault(r[0], []).append(f"телефон вывода {r[1]}")
    out = []
    for uid, reasons in why.items():
        u = conn.execute("SELECT id, name, email, blocked_at FROM users WHERE id = ?", (uid,)).fetchone()
        if u:
            out.append({**dict(u), "why": sorted(set(reasons))})
    return out


@router.get("/users/{user_id}")
def user_profile(user_id: str, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    """Профиль для проверки: сделки пользователя, объявления, связанные аккаунты, адреса входа и журнал администрации."""
    u = next((x for x in users(conn) if x["id"] == user_id), None)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    sales = conn.execute(DEALS_SQL + " WHERE o.seller_id = ? ORDER BY o.created_at DESC LIMIT 200", (user_id,))
    buys = conn.execute(DEALS_SQL + " WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT 200", (user_id,))
    gigs = conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.owner_id = ? ORDER BY g.created_at DESC", (user_id,))
    ips = conn.execute(
        "SELECT ip, first_ts, last_ts, (SELECT count(DISTINCT user_id) FROM user_ips c WHERE c.ip = i.ip) AS users"
        " FROM user_ips i WHERE user_id = ? ORDER BY last_ts DESC LIMIT 20", (user_id,)).fetchall()
    out = {"user": u, "sales": [deal(r) for r in sales], "purchases": [deal(r) for r in buys],
           "gigs": [gig_out(r) for r in gigs], "linked": linked(conn, user_id), "ips": [dict(r) for r in ips]}
    if auth.is_admin(staff):
        coins, earned = conn.execute("SELECT coalesce(sum(coins), 0), coalesce(sum(earned), 0) FROM wallets WHERE user_id = ?",
                                     (user_id,)).fetchone()
        out["wallet"] = {"coins": coins, "earned": earned, "adv": coins - earned}
        out["log"] = [dict(r) for r in conn.execute(
            "SELECT * FROM admin_log WHERE user_id = ? ORDER BY id DESC LIMIT 100", (user_id,))]
    return out


def target_user(conn: sqlite3.Connection, user_id: str, staff) -> sqlite3.Row:
    """Пользователь, над которым сотрудник выполняет действие. Модератор не трогает других сотрудников."""
    u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    if auth.is_admin(u) or (auth.staff_role(u) and not auth.is_admin(staff)):
        raise HTTPException(400, "Это действие нельзя применить к сотруднику")
    return u


@router.post("/users/{user_id}/block")
def block_user(user_id: str, body: BlockIn, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    if body.days not in BLOCK_DAYS:
        raise HTTPException(400, "Выберите срок блокировки")
    reason = body.reason.strip()[:200]
    until = now_ms() + body.days * DAY_MS if body.days else None
    with tx(conn):
        u = target_user(conn, user_id, staff)
        conn.execute("UPDATE users SET blocked_at = ?, block_reason = ?, blocked_until = ? WHERE id = ?",
                     (now_ms(), reason, until, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # выход на всех устройствах
        audit(conn, staff, "block", f"Блокировка {'на ' + str(body.days) + ' дн.' if body.days else 'бессрочно'}"
              + (f": {reason}" if reason else ""), u["id"])
    return users(conn)


@router.post("/users/{user_id}/unblock")
def unblock_user(user_id: str, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    target_user(conn, user_id, staff)
    conn.execute("UPDATE users SET blocked_at = NULL, block_reason = '', blocked_until = NULL WHERE id = ?", (user_id,))
    audit(conn, staff, "unblock", "Разблокировка", user_id)
    return users(conn)


@router.post("/users/{user_id}/verify/{on}")
def verify_user(user_id: str, on: int, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    """Значок «Проверенный продавец» ставит и снимает администрация."""
    if not conn.execute("UPDATE users SET verified = ? WHERE id = ?", (1 if on else 0, user_id)).rowcount:
        raise HTTPException(404, "Пользователь не найден")
    if on:
        notify(conn, user_id, "verify", "Администратор отметил вас как проверенного продавца: значок ✓ виден на ваших объявлениях.",
               "#/seller/" + user_id)
    audit(conn, staff, "verify", "Выдан значок «Проверенный продавец»" if on else "Снят значок «Проверенный продавец»", user_id)
    return users(conn)


# ---------- Объявления ----------

@router.get("/gigs")
def gigs(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        f"SELECT g.*, {SOLD_SQL}, u.email AS owner_email, u.blocked_at IS NOT NULL AS owner_blocked"
        " FROM gigs g LEFT JOIN users u ON u.id = g.owner_id"
        " ORDER BY g.mod = 'pending' DESC, g.owner_id IS NULL, g.created_at DESC, g.id"
    ).fetchall()
    return [{**gig_out(r), "owner_email": r["owner_email"], "owner_blocked": bool(r["owner_blocked"]),
             "mod": r["mod"], "mod_note": r["mod_note"]} for r in rows]


def load_gig(conn: sqlite3.Connection, gig_id: str) -> sqlite3.Row:
    g = conn.execute("SELECT * FROM gigs WHERE id = ?", (gig_id,)).fetchone()
    if not g:
        raise HTTPException(404, "Объявление не найдено")
    return g


@router.delete("/gigs/{gig_id}")
def delete_gig(gig_id: str, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    with tx(conn):
        g = load_gig(conn, gig_id)
        conn.execute("DELETE FROM gigs WHERE id = ?", (gig_id,))
        conn.execute("DELETE FROM chat_messages WHERE gig_id = ?", (gig_id,))
        audit(conn, staff, "gig_delete", f"Удалено объявление «{g['title']}»", g["owner_id"], gig_id)
    remove_cover(g["cover"])
    return {"ok": True}


@router.post("/gigs/{gig_id}/approve")
def approve_gig(gig_id: str, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    """Объявление с проверки или скрытое — в каталог."""
    g = load_gig(conn, gig_id)
    conn.execute("UPDATE gigs SET mod = 'ok', mod_note = '' WHERE id = ?", (gig_id,))
    notify(conn, g["owner_id"], "verify", f"Объявление «{g['title']}» проверено и опубликовано в каталоге.", f"#/gig/{gig_id}")
    if g["mod"] == "pending":
        notify_searches(conn, gig_out(conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.id = ?", (gig_id,)).fetchone()))
    audit(conn, staff, "gig_approve", f"Объявление «{g['title']}» опубликовано", g["owner_id"], gig_id)
    return gigs(conn)


@router.post("/gigs/{gig_id}/hide")
def hide_gig(gig_id: str, body: HideIn, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    """Скрыть из каталога с причиной: автор её видит и, исправив объявление, отправляет на повторную проверку."""
    reason = body.reason.strip()[:300]
    if len(reason) < 5:
        raise HTTPException(400, "Напишите причину — её увидит автор")
    g = load_gig(conn, gig_id)
    conn.execute("UPDATE gigs SET mod = 'hidden', mod_note = ? WHERE id = ?", (reason, gig_id))
    notify(conn, g["owner_id"], "verify", f"Объявление «{g['title']}» скрыто из каталога: {reason}. Исправьте его в кабинете — "
                                          "после сохранения оно уйдёт на повторную проверку.", "#/dashboard/gigs")
    audit(conn, staff, "gig_hide", f"Объявление «{g['title']}» скрыто: {reason}", g["owner_id"], gig_id)
    return gigs(conn)


# ---------- Споры ----------

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
def dispute_decide(order_id: str, act: str, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    R = {"seller": "в пользу продавца", "buyer": "в пользу покупателя", "split": "пополам"}
    if act not in R:
        raise HTTPException(400, "Неизвестное решение")
    with tx(conn):
        o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not o or o["status"] != ST_DISPUTE:
            raise HTTPException(400, "Спор уже закрыт")
        resolve_dispute(conn, o, act)
        audit(conn, staff, "dispute", f"Спор по заказу {o['num']} решён {R[act]}", o["user_id"], order_id, o["amount"])
        if o["seller_id"]:
            audit(conn, staff, "dispute", f"Спор по заказу {o['num']} решён {R[act]}", o["seller_id"], order_id, o["amount"])
    return disputes(conn)
