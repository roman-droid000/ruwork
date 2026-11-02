"""Инструменты админ-панели: сводка «Требует внимания», журнал действий, сверка балансов, ручная корректировка,
графики, глобальный поиск, сообщения пользователям, карточка спора, просмотр чатов, премодерация, роли
и второй фактор входа (код из Telegram)."""

import hashlib
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, guard, jobs, tg
from ..db import get_db, now_ms, tx
from ..logic import (
    DAY_MS, ST_DISPUTE, ST_REVIEW, ST_WAIT, ST_WORK, add_coins, add_op, audit, deadline, get_wallet, nf, notify,
    seller_ratings, setting,
)
from .admin import linked

router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_staff)])
mfa_router = APIRouter(prefix="/api/staff/mfa")
admin_only = [Depends(auth.require_admin)]

MSK = 3 * 3600 * 1000  # дни в графиках — по московскому времени


# ---------- Журнал ----------

@router.get("/log", dependencies=admin_only)
def log(user: str = "", limit: int = 300, conn: sqlite3.Connection = Depends(get_db)):
    q, a = "SELECT l.*, u.name AS user_name FROM admin_log l LEFT JOIN users u ON u.id = l.user_id", []
    if user:
        q, a = q + " WHERE l.user_id = ?", [user]
    return [dict(r) for r in conn.execute(q + " ORDER BY l.id DESC LIMIT ?", (*a, min(max(limit, 1), 1000)))]


# ---------- Сверка балансов ----------

# Как каждая операция меняет весь баланс (wallets.coins). Вывод и возврат на карту удерживаются при создании
# заявки и возвращаются при отказе, поэтому учитываются в статусах «ожидает» и «выполнено».
LEDGER_SQL = """
SELECT user_id, sum(CASE
    WHEN t IN ('dep', 'ref', 'sale', 'comp', 'adj_in', 'bonus', 'refb') AND status = 'done' THEN coins
    WHEN t IN ('pay', 'promo', 'adj_out') AND status = 'done' THEN -coins
    WHEN t IN ('wd', 'rf') AND status IN ('pending', 'done') THEN -coins
    ELSE 0 END) AS expected
FROM wallet_ops GROUP BY user_id
"""


def reconcile_rows(conn: sqlite3.Connection) -> list[dict]:
    exp = {r["user_id"]: r["expected"] for r in conn.execute(LEDGER_SQL)}
    have = {r["user_id"]: r["coins"] + r["bonus"] for r in conn.execute("SELECT user_id, coins, bonus FROM wallets")}
    out = []
    for uid in set(exp) | set(have):
        e, h = exp.get(uid, 0), have.get(uid, 0)
        if e != h:
            u = conn.execute("SELECT name, email FROM users WHERE id = ?", (uid,)).fetchone()
            out.append({"user_id": uid, "name": u["name"] if u else "удалён", "email": auth.shown_email(u["email"]) if u else "",
                        "coins": h, "expected": e, "diff": h - e})
    return sorted(out, key=lambda r: -abs(r["diff"]))


@router.get("/reconcile", dependencies=admin_only)
def reconcile(conn: sqlite3.Connection = Depends(get_db)):
    rows = reconcile_rows(conn)
    total = conn.execute("SELECT count(*) FROM wallets").fetchone()[0]
    return {"checked": total, "bad": rows, "ts": now_ms()}


# ---------- Ручная корректировка баланса ----------

class AdjustIn(BaseModel):
    coins: int
    reason: str
    part: str = "earned"  # earned — заработок (можно вывести), adv — аванс


@router.post("/users/{user_id}/adjust", dependencies=admin_only)
def adjust(user_id: str, body: AdjustIn, staff=Depends(auth.require_admin), conn: sqlite3.Connection = Depends(get_db)):
    reason = body.reason.strip()[:200]
    if not body.coins or abs(body.coins) > 1_000_000:
        raise HTTPException(400, "Укажите сумму: плюс — начислить, минус — списать")
    if len(reason) < 5:
        raise HTTPException(400, "Напишите причину — её увидит пользователь")
    if body.part not in ("earned", "adv"):
        raise HTTPException(400, "Выберите часть баланса")
    with tx(conn):
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise HTTPException(404, "Пользователь не найден")
        coins, earned = get_wallet(conn, user_id)
        part_now = earned if body.part == "earned" else coins - earned
        if body.coins < 0 and -body.coins > part_now:
            raise HTTPException(400, f"Списать можно не больше {nf(part_now)} мон. из этой части баланса")
        add_coins(conn, user_id, body.coins, body.coins if body.part == "earned" else 0)
        add_op(conn, user_id, "adj_in" if body.coins > 0 else "adj_out", "adm", abs(body.coins), "done", reason)
        what = "Начислено" if body.coins > 0 else "Списано"
        notify(conn, user_id, "wallet", f"{what} администрацией RUWORK: {nf(abs(body.coins))} мон. Причина: {reason}",
               "#/dashboard/wallet")
        audit(conn, staff, "adjust", f"{what} {nf(abs(body.coins))} мон. ({'заработок' if body.part == 'earned' else 'аванс'}): {reason}",
              user_id, "", body.coins)
    return {"ok": True}


# ---------- Графики ----------

def day_of(ts: int) -> int:
    return (ts + MSK) // DAY_MS


@router.get("/stats", dependencies=admin_only)
def stats(days: int = 30, conn: sqlite3.Connection = Depends(get_db)):
    days = days if days in (7, 30, 90) else 30
    today = day_of(now_ms())
    first = today - days + 1
    since = first * DAY_MS - MSK
    keys = ["turnover", "fee", "orders", "users", "gigs"]
    series = {k: [0] * days for k in keys}

    def put(key: str, rows):
        for ts, v in rows:
            i = day_of(ts) - first
            if 0 <= i < days:
                series[key][i] += v or 0

    put("turnover", conn.execute("SELECT created_at, amount FROM orders WHERE created_at >= ? AND status != 'Отменён'", (since,)))
    put("orders", conn.execute("SELECT created_at, 1 FROM orders WHERE created_at >= ?", (since,)))
    put("fee", conn.execute("SELECT ts, coins FROM platform_ops WHERE ts >= ?", (since,)))
    put("users", conn.execute("SELECT created_at, 1 FROM users WHERE created_at >= ?", (since,)))
    put("gigs", conn.execute("SELECT created_at, 1 FROM gigs WHERE created_at >= ? AND owner_id IS NOT NULL", (since,)))
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]
    funnel = [
        ["Зарегистрировались", one("SELECT count(*) FROM users")],
        ["Разместили объявление", one("SELECT count(DISTINCT owner_id) FROM gigs WHERE owner_id IS NOT NULL")],
        ["Получили первый заказ", one("SELECT count(DISTINCT seller_id) FROM orders WHERE seller_id IS NOT NULL")],
        ["Завершили продажу", one("SELECT count(DISTINCT seller_id) FROM orders WHERE seller_id IS NOT NULL AND status = 'Выполнен'")],
    ]
    buyers = [
        ["Зарегистрировались", one("SELECT count(*) FROM users")],
        ["Сделали заказ", one("SELECT count(DISTINCT user_id) FROM orders")],
        ["Заказали повторно", one("SELECT count(*) FROM (SELECT user_id FROM orders GROUP BY user_id HAVING count(*) > 1)")],
    ]
    return {"days": days, "start": first * DAY_MS - MSK, "series": series, "funnel": funnel, "buyers": buyers}


# ---------- Сводка «Требует внимания» ----------

@router.get("/attention")
def attention(staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    n, adm = now_ms(), auth.is_admin(staff)
    items = []

    def add(level: str, text: str, link: str, count: int = 0):
        items.append({"level": level, "text": text, "link": link, "n": count})

    if adm:
        old = conn.execute("SELECT count(*), min(ts) FROM wallet_ops WHERE status = 'pending' AND t IN ('dep', 'wd', 'rf') AND ts < ?",
                           (n - DAY_MS,)).fetchone()
        if old[0]:
            add("bad", f"Заявки ждут больше суток (самая старая — {(n - old[1]) // 3_600_000} ч)", "#/admin/ops", old[0])
        new = conn.execute("SELECT count(*) FROM wallet_ops WHERE status = 'pending' AND t IN ('dep', 'wd', 'rf')").fetchone()[0]
        if new - old[0] > 0:
            add("warn", "Новые заявки на пополнение, возврат или вывод", "#/admin/ops", new - old[0])
        risky = conn.execute("SELECT count(*) FROM wallet_ops WHERE status = 'pending' AND t = 'wd'").fetchone()[0]
        if risky:
            add("warn", "Выводы на проверке — посмотрите флаги риска", "#/admin/ops", risky)
        r = conn.execute("SELECT count(*) FROM password_resets WHERE status = 'pending'").fetchone()[0]
        if r:
            add("warn", "Заявки на сброс пароля", "#/admin/resets", r)
    d = conn.execute("SELECT count(*), min(dispute_at) FROM orders WHERE status = ?", (ST_DISPUTE,)).fetchone()
    if d[0]:
        add("bad" if n - d[1] > DAY_MS else "warn", f"Открытые споры (самый старый — {(n - d[1]) // 3_600_000} ч)", "#/admin/disputes", d[0])
    rp = conn.execute("SELECT count(*) FROM reports WHERE status = 'open'").fetchone()[0]
    if rp:
        add("warn", "Жалобы без разбора", "#/admin/reports", rp)
    pm = conn.execute("SELECT count(*) FROM gigs WHERE mod = 'pending'").fetchone()[0]
    if pm:
        add("warn", "Объявления ждут проверки", "#/admin/gigs", pm)
    late = [o for o in conn.execute("SELECT * FROM orders WHERE status = ? AND seller_id IS NOT NULL AND started_at IS NOT NULL",
                                    (ST_WORK,)) if deadline(o) < n]
    if late:
        add("warn", "Просроченные заказы: " + ", ".join(o["num"] for o in late[:5]) + ("…" if len(late) > 5 else ""),
            f"#/order/{late[0]['id']}", len(late))
    if adm:
        errs = conn.execute("SELECT path, count(*) AS c FROM errors WHERE ts > ? GROUP BY path ORDER BY c DESC", (n - DAY_MS,)).fetchall()
        if errs:
            add("bad", "Ошибки 500 за сутки: " + "; ".join(f"{e['path']} ×{e['c']}" for e in errs[:4]), "", sum(e["c"] for e in errs))
        if jobs.status["backup_error"]:
            add("bad", "Резервная копия не создана: " + jobs.status["backup_error"], "")
        bad = reconcile_rows(conn)
        if bad:
            add("bad", "Расхождения балансов с операциями", "#/admin/ops", len(bad))
    active = conn.execute("SELECT count(*) FROM orders WHERE status IN (?, ?, ?)", (ST_WAIT, ST_WORK, ST_REVIEW)).fetchone()[0]
    return {"items": items, "active_orders": active, "premod": setting(conn, "premod", "0") == "1",
            "users": conn.execute("SELECT count(*) FROM users").fetchone()[0]}


# ---------- Настройки ----------

class SettingsIn(BaseModel):
    premod: bool


@router.post("/settings", dependencies=admin_only)
def save_settings(body: SettingsIn, staff=Depends(auth.require_admin), conn: sqlite3.Connection = Depends(get_db)):
    conn.execute("INSERT INTO settings(key, value) VALUES ('premod', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 ("1" if body.premod else "0",))
    audit(conn, staff, "settings", "Премодерация новых продавцов " + ("включена" if body.premod else "выключена"))
    return {"premod": body.premod}


# ---------- Поиск ----------

@router.get("/search")
def search(q: str, conn: sqlite3.Connection = Depends(get_db)):
    q = q.strip()
    if len(q) < 2:
        return {"users": [], "orders": [], "gigs": [], "requests": []}
    like = f"%{q.lower()}%"
    digits = "".join(c for c in q if c.isdigit())
    phone_users = ([r[0] for r in conn.execute("SELECT DISTINCT user_id FROM wallet_ops WHERE phone LIKE ?", (f"%{digits[-10:]}%",))]
                   if len(digits) >= 4 else [])
    users = conn.execute(
        "SELECT id, name, CASE WHEN email LIKE ? THEN coalesce(phone, '') ELSE email END AS email, blocked_at, created_at"
        " FROM users WHERE lower(name) LIKE ? OR lower(email) LIKE ? OR id = ?"
        f" OR (? != '' AND phone LIKE ?) OR id IN ({','.join('?' * len(phone_users)) or 'NULL'}) LIMIT 20",
        ("%" + auth.TG_EMAIL_DOMAIN, like, like, q, digits, f"%{digits[-10:]}%", *phone_users)).fetchall()
    num = q.upper().lstrip("#")
    orders = conn.execute(
        "SELECT o.id, o.num, o.title, o.status, o.amount, b.name AS buyer FROM orders o JOIN users b ON b.id = o.user_id"
        " WHERE upper(o.num) LIKE ? OR o.id = ? ORDER BY o.created_at DESC LIMIT 20", (f"%{num}%", q)).fetchall()
    gigs = conn.execute("SELECT id, title, seller, mod FROM gigs WHERE id = ? OR lower(title) LIKE ? LIMIT 20", (q, like)).fetchall()
    reqs = conn.execute("SELECT id, title, status FROM requests WHERE id = ? OR lower(title) LIKE ? LIMIT 10", (q, like)).fetchall()
    return {"users": [dict(r) for r in users], "orders": [dict(r) for r in orders],
            "gigs": [dict(r) for r in gigs], "requests": [dict(r) for r in reqs]}


# ---------- Сообщение пользователю ----------

class MessageIn(BaseModel):
    text: str
    link: str = ""


@router.post("/users/{user_id}/message")
def message(user_id: str, body: MessageIn, staff=Depends(auth.require_staff), conn: sqlite3.Connection = Depends(get_db)):
    """Сообщение от администрации: в колокольчик и в Telegram, если привязан."""
    text = body.text.strip()[:1000]
    if len(text) < 3:
        raise HTTPException(400, "Введите текст сообщения")
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        raise HTTPException(404, "Пользователь не найден")
    link = body.link if body.link.startswith("#/") else ""
    notify(conn, user_id, "admin", "Сообщение от администрации RUWORK: " + text, link)
    audit(conn, staff, "message", "Сообщение: " + text, user_id)
    return {"ok": True}


# ---------- Карточка спора ----------

@router.get("/dispute/{order_id}")
def dispute_detail(order_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Всё для решения спора на одном экране: переписка, файлы, правки и продления, история сторон."""
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not o:
        raise HTTPException(404, "Заказ не найден")
    chat = conn.execute("SELECT role, text, ts FROM chat_messages WHERE owner = ? AND gig_id = ? ORDER BY id",
                        ("u:" + o["user_id"], o["gig_id"])).fetchall()
    files = conn.execute("SELECT id, name, size, ts FROM order_files WHERE order_id = ? AND done = 1 ORDER BY ts", (order_id,)).fetchall()
    ratings = seller_ratings(conn)

    def side(uid: str | None, role: str) -> dict | None:
        if not uid:
            return None
        u = conn.execute("SELECT id, name, created_at, verified FROM users WHERE id = ?", (uid,)).fetchone()
        col = "user_id" if role == "buyer" else "seller_id"
        st = conn.execute(f"SELECT count(*), sum(dispute_at IS NOT NULL), sum(status = 'Выполнен') FROM orders WHERE {col} = ?",
                          (uid,)).fetchone()
        rating, rev = ratings.get(uid, (0, 0))
        return {**dict(u), "orders": st[0], "disputes": (st[1] or 0) - (1 if o["dispute_at"] else 0), "done": st[2] or 0,
                "rating": rating, "rev": rev}

    pair = linked(conn, o["user_id"])
    return {"order": {k: o[k] for k in ("id", "num", "title", "tier_name", "status", "amount", "method", "days", "revs",
                                         "revs_used", "extra_days", "created_at", "started_at", "delivered_at",
                                         "dispute_at", "dispute_reason")},
            "deadline": deadline(o), "chat": [dict(r) for r in chat], "files": [dict(r) for r in files],
            "buyer": side(o["user_id"], "buyer"), "seller": side(o["seller_id"], "seller"),
            "related": any(x["id"] == o["seller_id"] for x in pair)}


# ---------- Чаты ----------

CHAT_SQL = (
    "SELECT m.id, m.role, m.text, m.ts, m.gig_id, g.title, g.owner_id, s.name AS seller, b.id AS buyer_id, b.name AS buyer"
    " FROM chat_messages m JOIN users b ON m.owner = 'u:' || b.id JOIN gigs g ON g.id = m.gig_id"
    " LEFT JOIN users s ON s.id = g.owner_id WHERE g.owner_id IS NOT NULL AND m.role IN ('me', 'seller')"
)


@router.get("/chats")
def chats(q: str = "", flagged: int = 0, conn: sqlite3.Connection = Depends(get_db)):
    """Переписка покупателей и продавцов. flagged=1 — только сообщения, где был скрыт контакт."""
    sql, a = CHAT_SQL, []
    if flagged:
        sql += " AND m.text LIKE ?"
        a.append(f"%{guard.CONTACT_STUB}%")
    if q.strip():
        sql += " AND (lower(m.text) LIKE ? OR lower(b.name) LIKE ? OR lower(s.name) LIKE ?)"
        a += [f"%{q.strip().lower()}%"] * 3
    rows = conn.execute(sql + " ORDER BY m.id DESC LIMIT 200", a).fetchall()
    # Кто чаще всех пытается обменяться контактами за 30 дней (отправитель: покупатель или автор объявления).
    top = conn.execute(
        "SELECT CASE m.role WHEN 'me' THEN b.id ELSE g.owner_id END AS uid, count(*) AS c FROM chat_messages m"
        " JOIN users b ON m.owner = 'u:' || b.id JOIN gigs g ON g.id = m.gig_id"
        " WHERE m.text LIKE ? AND m.ts > ? AND m.role IN ('me', 'seller') GROUP BY uid ORDER BY c DESC LIMIT 10",
        (f"%{guard.CONTACT_STUB}%", now_ms() - 30 * DAY_MS)).fetchall()
    names = {r["id"]: r["name"] for r in conn.execute(
        f"SELECT id, name FROM users WHERE id IN ({','.join('?' * len(top)) or 'NULL'})", [t["uid"] for t in top])}
    return {"messages": [dict(r) for r in rows],
            "offenders": [{"id": t["uid"], "name": names.get(t["uid"], "?"), "count": t["c"]} for t in top]}


@router.get("/chats/thread")
def chat_thread(gig: str, buyer: str, conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT role, text, ts FROM chat_messages WHERE owner = ? AND gig_id = ? ORDER BY id",
                        ("u:" + buyer, gig)).fetchall()
    return [dict(r) for r in rows]


# ---------- Роли ----------

class RoleIn(BaseModel):
    role: str


@router.post("/users/{user_id}/role", dependencies=admin_only)
def set_role(user_id: str, body: RoleIn, staff=Depends(auth.require_admin), conn: sqlite3.Connection = Depends(get_db)):
    if body.role not in ("", *auth.ROLES):
        raise HTTPException(400, "Неизвестная роль")
    u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not u or auth.is_admin(u):
        raise HTTPException(400, "Роль владельца не меняется")
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
    conn.execute("UPDATE sessions SET mfa_at = NULL WHERE user_id = ?", (user_id,))
    if body.role:
        notify(conn, user_id, "verify", f"Вам выдана роль «{auth.ROLES[body.role]}». Админ-панель — в меню; для входа "
                                        "нужен Telegram, привязанный в профиле.", "#/admin")
    audit(conn, staff, "role", f"Роль: {auth.ROLES.get(body.role, 'обычный пользователь')}", user_id)
    from .admin import users
    return users(conn)


# ---------- Второй фактор: код из Telegram ----------

MFA_CODE_TTL_MS = 10 * 60 * 1000
MFA_SEND_RATE = (3, 600)
MFA_TRIES = 5


def code_hash(code: str) -> str:
    return hashlib.sha256(("ruwork-mfa:" + code).encode()).hexdigest()


@mfa_router.get("")
def mfa_state(request: Request, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if not auth.staff_role(user):
        raise HTTPException(403, "Нет доступа")
    return {"on": auth.mfa_on(), "ok": auth.mfa_ok(conn, request), "tg": bool(user["tg_chat"])}


@mfa_router.post("/send")
def mfa_send(request: Request, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if not auth.staff_role(user):
        raise HTTPException(403, "Нет доступа")
    if not user["tg_chat"]:
        raise HTTPException(400, "Сначала подключите Telegram в профиле")
    guard.throttle("mfa:" + user["id"], MFA_SEND_RATE, "Код уже отправлен — подождите несколько минут")
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute("UPDATE sessions SET mfa_code = ?, mfa_sent = ?, mfa_tries = 0 WHERE token = ?",
                 (code_hash(code), now_ms(), request.cookies.get(auth.SESSION_COOKIE)))
    tg.send(user["tg_chat"], f"Код входа в админ-панель RUWORK: {code}\nДействует 10 минут. Если это не вы — "
                             "срочно смените пароль.")
    return {"ok": True}


class CodeIn(BaseModel):
    code: str


@mfa_router.post("/verify")
def mfa_verify(body: CodeIn, request: Request, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    s = auth.session_row(conn, request)
    if not auth.staff_role(user) or s is None:
        raise HTTPException(403, "Нет доступа")
    if not s["mfa_code"] or s["mfa_sent"] < now_ms() - MFA_CODE_TTL_MS or s["mfa_tries"] >= MFA_TRIES:
        raise HTTPException(400, "Код устарел — запросите новый")
    if not secrets.compare_digest(code_hash(body.code.strip()), s["mfa_code"]):
        conn.execute("UPDATE sessions SET mfa_tries = mfa_tries + 1 WHERE token = ?", (s["token"],))
        raise HTTPException(400, f"Неверный код. Осталось попыток: {MFA_TRIES - s['mfa_tries'] - 1}")
    conn.execute("UPDATE sessions SET mfa_at = ?, mfa_code = NULL WHERE token = ?", (now_ms(), s["token"]))
    audit(conn, user, "login", "Вход в админ-панель с кодом из Telegram")
    guard.alert(f"Вход в админ-панель: {user['name']}", key="mfa:" + user["id"])
    return {"ok": True}
