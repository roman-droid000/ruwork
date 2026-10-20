"""Мини-приложение «Мои заказы» в Telegram (кнопка «Мои заказы» у бота поддержки).

Страница /tgapp открывается внутри Telegram и каждым запросом присылает initData в заголовке
X-Telegram-Init-Data. Сайт проверяет его подпись токеном бота (RUWORK_TG_TOKEN) и работает от имени
аккаунта, к которому привязан этот Telegram (users.tg_chat). Cookie сессии здесь не используются:
в Telegram Web приложение открывается во фрейме, и браузер их не отправит.

Действия (подтверждение, отмена, спор, отзыв, чат) вызывают те же функции, что и кнопки на сайте,
поэтому правила и проверки прав у них общие. Администратор здесь — обычный участник своих заказов:
чужие заказы он смотрит на сайте.

Защита: подпись initData (сутки), ограничение частоты входа (по Telegram и по email) и действий,
одинаковое время ответа при входе, уведомление прежнего Telegram о перепривязке, CSP на странице.
"""

import hashlib
import hmac
import html
import json
import re
import sqlite3
import time
from base64 import b64encode
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import auth, tg
from ..db import ROOT, get_db, now_ms, tx
from ..logic import AUTO_CONFIRM_MS, CONFIRM_MS, ST_DONE, TIERS, notify, tick, user_state
from . import chat, money, orders as order_routes, reviews

router = APIRouter()

PAGE = ROOT / "public" / "tgapp.html"
INIT_DATA_TTL = 86_400  # initData старше суток не принимаем: приложение нужно открыть заново
FILE_LINK_TTL = 600  # ссылка на скачивание файла результата живёт 10 минут
TIER_DAYS = {t[0]: t[2] for t in TIERS}
# Ограничения частоты: (сколько, за сколько секунд)
LOGIN_PER_TG = (5, 900)  # неудачных входов с одного Telegram
LOGIN_PER_EMAIL = (10, 900)  # неудачных входов в один аккаунт с любых Telegram — против перебора пароля
CHAT_RATE = (15, 60)  # сообщений в чат заказа в минуту
ACTION_RATE = (20, 60)  # действий с заказами и заявками в минуту
hits: dict[str, list[float]] = {}
DUMMY_SALT = b"\0" * 16  # хэш для несуществующего email: время ответа не выдаёт, есть ли аккаунт


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class TextIn(BaseModel):
    text: str = Field(max_length=4000)


def limited(key: str, limit: tuple[int, int]) -> bool:
    count, window = limit
    now = time.time()
    recent = [t for t in hits.get(key, []) if now - t < window]
    if recent:
        hits[key] = recent
    else:
        hits.pop(key, None)
    if len(hits) > 50_000:  # старые ключи чистим, чтобы словарь не рос бесконечно
        for k in [k for k, v in hits.items() if now - v[-1] > 3600]:
            del hits[k]
    return len(recent) >= count


def hit(key: str) -> None:
    hits.setdefault(key, []).append(time.time())


def throttle(key: str, limit: tuple[int, int]) -> None:
    if limited(key, limit):
        raise HTTPException(429, "Слишком часто — подождите немного и попробуйте снова")
    hit(key)


def check_init_data(init_data: str) -> int:
    """Проверка initData по алгоритму Telegram: HMAC-SHA256 всех полей, кроме hash, ключом от токена бота."""
    if not tg.TOKEN:
        raise HTTPException(503, "Telegram не настроен")
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    got = fields.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", tg.TOKEN.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(got, want):
        raise HTTPException(401, "Откройте приложение из Telegram-бота RUWORK")
    try:
        if time.time() - int(fields.get("auth_date", "0")) > INIT_DATA_TTL:
            raise HTTPException(401, "Сессия устарела — откройте приложение заново")
        return int(json.loads(fields["user"])["id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "Откройте приложение из Telegram-бота RUWORK")


def tg_id(request: Request) -> int:
    return check_init_data(request.headers.get("x-telegram-init-data", ""))


def linked_user(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE tg_chat = ? AND blocked_at IS NULL", (chat_id,)).fetchone()


def tg_user(chat_id: int = Depends(tg_id), conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    user = linked_user(conn, chat_id)
    if user is None:
        raise HTTPException(403, "Telegram не привязан к аккаунту RUWORK")
    return user


# ---------- Страница и общее состояние ----------

def page_csp() -> str:
    """CSP для страницы: встроенный скрипт разрешён только по хэшу, запросы — только к этому сайту."""
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S)
    hashes = " ".join(f"'sha256-{b64encode(hashlib.sha256(s.encode()).digest()).decode()}'" for s in scripts)
    return ("default-src 'none'; "
            f"script-src https://telegram.org {hashes}; "
            "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-src tg:; "  # telegram-web-app.js на iOS открывает tg://-ссылки через скрытый iframe
            "frame-ancestors https://web.telegram.org https://*.telegram.org")


@router.get("/tgapp", include_in_schema=False)
def page():
    return FileResponse(PAGE, headers={"Cache-Control": "no-cache", "Content-Security-Policy": page_csp(),
                                       "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


def order_out(r: sqlite3.Row, user_id: str) -> dict:
    buyer = r["user_id"] == user_id
    started = r["started_at"] or r["created_at"] + CONFIRM_MS
    days = TIER_DAYS.get(r["tier_name"])
    return {
        "id": r["id"], "num": r["num"], "title": r["title"], "tier": r["tier_name"], "status": r["status"],
        "amount": r["amount"], "role": "buy" if buyer else "sell", "created_at": r["created_at"],
        "gig": r["gig_id"], "has_seller": bool(r["seller_id"]),
        "partner": (r["seller_name"] if buyer else r["buyer_name"]) or "",
        "deadline": started + days * 86_400_000 if days else None,
        "auto_confirm": r["reviewed_at"] + AUTO_CONFIRM_MS if r["reviewed_at"] else None,
        "dispute_reason": r["dispute_reason"], "dispute_result": r["dispute_result"],
        "reviewed": bool(r["has_review"]),
    }


def seller_stats(conn: sqlite3.Connection, user_id: str, rows: list[dict]) -> dict:
    """Панель продавца: доход по месяцам (зачисления за продажи), ожидаемые деньги и работа к сдаче."""
    month_start = time.localtime()
    months = []
    y, m = month_start.tm_year, month_start.tm_mon
    for _ in range(6):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    income = {k: 0 for k in months}
    for coins, ts in conn.execute("SELECT coins, ts FROM wallet_ops WHERE user_id = ? AND t = 'sale' AND status = 'done'",
                                  (user_id,)):
        t = time.localtime(ts / 1000)
        if (t.tm_year, t.tm_mon) in income:
            income[(t.tm_year, t.tm_mon)] += coins
    sells = [o for o in rows if o["role"] == "sell"]
    return {
        "chart": [{"y": y, "m": m, "sum": income[(y, m)]} for y, m in months],
        "pending": sum(o["amount"] for o in sells if o["status"] in ("Ожидает подтверждения", "В работе", "На проверке", "Спор")),
        "to_upload": sum(o["status"] == "В работе" for o in sells),
        "review": sum(o["status"] == "На проверке" for o in sells),
    }


@router.get("/api/tgapp/state")
def state(chat_id: int = Depends(tg_id), conn: sqlite3.Connection = Depends(get_db)):
    u = linked_user(conn, chat_id)
    if not u:
        return {"linked": False, "bot": tg.username}
    with tx(conn):
        st = user_state(conn, u["id"])  # таймеры статусов и автоподтверждение — как при заходе на сайт
        buyers = conn.execute("SELECT DISTINCT user_id FROM orders WHERE seller_id = ?", (u["id"],)).fetchall()
        for (buyer,) in buyers:  # у продаж статусы двигаются таймерами покупателей
            tick(conn, buyer)
    rows = conn.execute(
        "SELECT o.*, b.name AS buyer_name, s.name AS seller_name,"
        " EXISTS(SELECT 1 FROM reviews r WHERE r.order_id = o.id) AS has_review FROM orders o"
        " JOIN users b ON b.id = o.user_id LEFT JOIN users s ON s.id = o.seller_id"
        " WHERE o.user_id = ? OR o.seller_id = ? ORDER BY o.created_at DESC, o.rowid DESC LIMIT 200",
        (u["id"], u["id"]),
    ).fetchall()
    out = [order_out(r, u["id"]) for r in rows]
    return {"linked": True, "name": u["name"], "bot": tg.username, "now": now_ms(), "orders": out,
            "wallet": st["wallet"], "seller": seller_stats(conn, u["id"], out)}


# ---------- Вход и привязка прямо из приложения ----------

@router.post("/api/tgapp/login")
def login(body: LoginIn, chat_id: int = Depends(tg_id), conn: sqlite3.Connection = Depends(get_db)):
    email = auth.norm_email(body.email)
    keys = (f"login-tg:{chat_id}", f"login-email:{email}")
    if limited(keys[0], LOGIN_PER_TG) or limited(keys[1], LOGIN_PER_EMAIL):
        raise HTTPException(429, "Слишком много попыток — попробуйте через 15 минут")
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None:
        auth.hash_pw(body.password, DUMMY_SALT)  # столько же времени, сколько проверка настоящего пароля
    if user is None or not body.password or not auth.check_pw(user, body.password):
        hit(keys[0])
        hit(keys[1])
        raise HTTPException(400, "Неверный email или пароль")
    if user["blocked_at"] is not None:
        raise HTTPException(403, auth.blocked_message(user))
    # Владелец должен узнать о новой привязке. notify пишет в колокольчик на сайте и дублирует в Telegram,
    # привязанный сейчас, — поэтому вызываем до перепривязки: предупреждение получит прежний Telegram.
    if user["tg_chat"] != chat_id:
        notify(conn, user["id"], "tg", "К аккаунту привязан новый Telegram. Если это были не вы — смените пароль "
                                       "и отключите Telegram в профиле.", "#/dashboard/profile")
    # Один чат — один аккаунт, как при привязке по ссылке из профиля.
    conn.execute("UPDATE users SET tg_chat = NULL WHERE tg_chat = ?", (chat_id,))
    conn.execute("UPDATE users SET tg_chat = ?, tg_code = NULL WHERE id = ?", (chat_id, user["id"]))
    tg.send(chat_id, f"Готово, {user['name']}! Telegram привязан к аккаунту RUWORK: здесь будут уведомления "
                     "о заказах, а в приложении RUWORK — заказы, кошелёк и чат. Отвязать: /stop")
    return {"ok": True}


# ---------- Заказ: подробности, действия, чат ----------

def file_sig(user_id: str, order_id: str, file_id: str, exp: int) -> str:
    key = hashlib.sha256(("ruwork-tgapp-file:" + tg.TOKEN).encode()).digest()
    return hmac.new(key, f"{user_id}:{order_id}:{file_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def timeline(conn: sqlite3.Connection, o: sqlite3.Row) -> list[dict]:
    """События заказа по датам: из полей заказа и системных сообщений в чате заказа."""
    events = [{"ts": o["created_at"], "t": "Заказ оплачен", "k": "pay"}]
    if o["started_at"] and o["started_at"] <= now_ms():
        events.append({"ts": o["started_at"], "t": "Заказ взят в работу", "k": "work"})
    for text, ts in conn.execute(
        "SELECT text, ts FROM chat_messages WHERE owner = ? AND gig_id = ? AND role = 'order' AND ts >= ?",
        ("u:" + o["user_id"], o["gig_id"], o["created_at"]),
    ):
        if re.search(re.escape(o["num"]) + r"(?!\d)", text):  # #RW-1 не должен совпадать с #RW-10
            events.append({"ts": ts, "t": html.unescape(text), "k": "sys"})
    if o["confirmed_at"]:
        events.append({"ts": o["confirmed_at"], "t": "Заказ завершён" if o["status"] == ST_DONE else "Заказ закрыт", "k": "done"})
    return sorted(events, key=lambda e: e["ts"])


def chat_messages(conn: sqlite3.Connection, o: sqlite3.Row) -> list[dict]:
    rows = conn.execute(
        "SELECT role, text, ts FROM chat_messages WHERE owner = ? AND gig_id = ? ORDER BY id DESC LIMIT 100",
        ("u:" + o["user_id"], o["gig_id"]),
    ).fetchall()
    return [{"r": r["role"], "t": r["text"], "ts": r["ts"]} for r in reversed(rows)]


def detail(conn: sqlite3.Connection, user: sqlite3.Row, order_id: str) -> dict:
    d = order_routes.get_order(order_id, user=user, conn=conn)  # проверка участника и таймеры — как на сайте
    if d["role"] not in ("buyer", "seller"):
        raise HTTPException(404, "Заказ не найден")  # просмотр чужих заказов администратором — только на сайте
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    exp = int(time.time()) + FILE_LINK_TTL
    for f in d["files"]:
        f["url"] = (f"/api/tgapp/file/{o['id']}/{f['id']}?u={user['id']}&exp={exp}"
                    f"&sig={file_sig(user['id'], o['id'], f['id'], exp)}")
    if d["review"]:
        d["review"]["text"] = html.unescape(d["review"]["text"])
        d["review"]["reply"] = html.unescape(d["review"]["reply"] or "")
    d["timeline"] = timeline(conn, o)
    d["chat"] = chat_messages(conn, o) if o["seller_id"] else []
    started = o["started_at"] or o["created_at"] + CONFIRM_MS
    days = TIER_DAYS.get(o["tier_name"])
    d["deadline"] = started + days * 86_400_000 if days else None
    d["now"] = now_ms()
    return d


@router.get("/api/tgapp/orders/{order_id}")
def get_order(order_id: str, user=Depends(tg_user), conn: sqlite3.Connection = Depends(get_db)):
    return detail(conn, user, order_id)


class ReviewIn(BaseModel):
    stars: int
    text: str = Field(max_length=4000)


@router.post("/api/tgapp/orders/{order_id}/review")
def review(order_id: str, body: ReviewIn, user=Depends(tg_user), conn: sqlite3.Connection = Depends(get_db)):
    throttle(f"act:{user['id']}", ACTION_RATE)
    reviews.create_review(order_id, reviews.ReviewIn(stars=body.stars, text=body.text), user=user, conn=conn)
    return detail(conn, user, order_id)


@router.post("/api/tgapp/orders/{order_id}/chat")
async def send_chat(order_id: str, body: TextIn, user=Depends(tg_user), conn: sqlite3.Connection = Depends(get_db)):
    throttle(f"chat:{user['id']}", CHAT_RATE)
    o, role = order_routes.load_order(conn, order_id, user)
    if not o["seller_id"] or role not in ("buyer", "seller"):
        raise HTTPException(400, "У этого заказа нет чата с продавцом")
    if role == "buyer":
        # Объявление пользователя: сообщение покупателя уходит продавцу (request/response нужны только гостям).
        await chat.send_message(o["gig_id"], chat.MessageIn(text=body.text), None, None, user=user, conn=conn)
    else:
        chat.seller_reply(o["gig_id"], chat.ReplyIn(to=o["user_id"], text=body.text), user=user, conn=conn)
    return {"chat": chat_messages(conn, o)}


# Общий маршрут действий — после /review и /chat, иначе он перехватил бы их.
@router.post("/api/tgapp/orders/{order_id}/{act}")
def order_action(order_id: str, act: str, body: TextIn | None = None, user=Depends(tg_user),
                 conn: sqlite3.Connection = Depends(get_db)):
    throttle(f"act:{user['id']}", ACTION_RATE)
    text = body.text if body else ""
    if act == "confirm":
        order_routes.confirm(order_id, user=user, conn=conn)
    elif act == "cancel":
        money.cancel_order(order_id, user=user, conn=conn)
    elif act == "dispute":
        order_routes.open_dispute(order_id, order_routes.DisputeIn(reason=text), user=user, conn=conn)
    elif act == "dispute-close":
        order_routes.close_dispute(order_id, user=user, conn=conn)
    else:
        raise HTTPException(404, "Неизвестное действие")
    return detail(conn, user, order_id)


@router.get("/api/tgapp/file/{order_id}/{file_id}", include_in_schema=False)
def file(order_id: str, file_id: str, u: str, exp: int, sig: str, dl: int = 0,
         conn: sqlite3.Connection = Depends(get_db)):
    """Скачивание результата по подписанной ссылке: открывается во внешнем браузере, где нет входа на сайт."""
    if exp < time.time() or not tg.TOKEN or not hmac.compare_digest(sig, file_sig(u, order_id, file_id, exp)):
        raise HTTPException(403, "Ссылка устарела — откройте файл из приложения ещё раз")
    user = conn.execute("SELECT * FROM users WHERE id = ? AND blocked_at IS NULL", (u,)).fetchone()
    if user is None:
        raise HTTPException(404, "Файл не найден")
    return order_routes.get_file(order_id, file_id, dl=dl, user=user, conn=conn)


# ---------- Кошелёк ----------

@router.post("/api/tgapp/ops/{op_id}/cancel")
def cancel_op(op_id: str, user=Depends(tg_user), conn: sqlite3.Connection = Depends(get_db)):
    throttle(f"act:{user['id']}", ACTION_RATE)
    return {"wallet": money.op_action(op_id, "cancel", user=user, conn=conn)["wallet"]}
