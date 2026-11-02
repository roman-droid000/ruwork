"""Регистрация, вход, профиль и общее состояние аккаунта."""

import hashlib
import html
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import auth, guard, tg
from ..db import get_db, now_ms, tx
from ..logic import WELCOME_BONUS, add_bonus, add_op, esc, new_id, notify, unread_notes, user_state

router = APIRouter(prefix="/api")

# Ограничения частоты: (сколько, за сколько секунд). Ключ по email общий с входом в мини-приложении.
LOGIN_PER_IP = (20, 900)  # неудачных входов с одного адреса
LOGIN_PER_EMAIL = (10, 900)  # неудачных входов в один аккаунт с любых адресов — против перебора пароля
REGISTER_PER_IP = (5, 3600)
RESET_PER_IP = (5, 3600)
RESET_PER_EMAIL = (3, 3600)
RESET_TG_TTL_MS = 30 * 60 * 1000  # ссылка подтверждения сброса из Telegram действует 30 минут
DUMMY_SALT = b"\0" * 16  # хэш для несуществующего email: время ответа не выдаёт, есть ли аккаунт


REG_COOKIE = "ruwork_reg"
TG_START_PER_IP = (10, 3600)


class RegisterIn(BaseModel):
    name: str
    email: str = ""  # только если бот не настроен — иначе регистрация по коду из Telegram
    code: str = ""
    check_only: bool = False  # проверить имя и пароль до кода: окно с кодом открывается только после этого
    password: str
    password2: str
    terms: bool
    ref: str = ""  # id пригласившего из реферальной ссылки ?ref=…


class LoginIn(BaseModel):
    email: str
    password: str


class LoginCodeIn(BaseModel):
    ticket: str
    code: str = ""


LOGIN_CODE_TTL_MS = 10 * 60 * 1000
LOGIN_CODE_TRIES = 5
LOGIN_RESEND = (3, 600)


class ResetIn(BaseModel):
    email: str
    password: str
    password2: str
    contact: str


class NameIn(BaseModel):
    name: str


class PasswordIn(BaseModel):
    current: str
    password: str
    password2: str


def name_taken(conn: sqlite3.Connection, name: str, except_id: str = "", admin: bool = False) -> bool:
    if name.lower() == auth.ADMIN_NAME and not admin:
        return True  # имя зарезервировано за администратором, даже если его аккаунт ещё не создан
    if conn.execute("SELECT 1 FROM sellers WHERE lower(name) = lower(?)", (name,)).fetchone():
        return True
    return bool(conn.execute("SELECT 1 FROM users WHERE name = ? AND id != ?", (name, except_id)).fetchone())


def tg_reg_on() -> bool:
    """Регистрация только через Telegram, когда настроен бот; без бота (локально) — по email."""
    return bool(tg.TOKEN)


@router.post("/auth/tg/start")
def tg_reg_start(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    """Начать регистрацию: ссылка на бота с одноразовым токеном; тот же токен — в cookie этого браузера,
    чтобы код из Telegram подошёл только к начатой здесь регистрации."""
    if not (tg_reg_on() and tg.username):
        raise HTTPException(400, "Регистрация через Telegram пока недоступна")
    guard.throttle("tgreg-ip:" + guard.client_ip(request), TG_START_PER_IP, "Слишком много попыток — попробуйте позже")
    token = secrets.token_urlsafe(18)
    conn.execute("DELETE FROM tg_regs WHERE ts < ?", (now_ms() - tg.REG_TTL_MS,))
    conn.execute("INSERT INTO tg_regs(token, ts) VALUES (?, ?)", (tg.reg_hash(token), now_ms()))
    response.set_cookie(REG_COOKIE, token, max_age=tg.REG_TTL_MS // 1000, httponly=True, samesite="lax", path="/")
    return {"url": f"https://t.me/{tg.username}?start=reg_{token}"}


def check_reg_code(conn: sqlite3.Connection, request: Request, code: str) -> sqlite3.Row:
    """Заявка на регистрацию этого браузера с подтверждённым в боте телефоном и верным кодом."""
    token = request.cookies.get(REG_COOKIE, "")
    r = conn.execute("SELECT * FROM tg_regs WHERE token = ?", (tg.reg_hash(token),)).fetchone() if token else None
    if r is None or r["ts"] < now_ms() - tg.REG_TTL_MS:
        raise HTTPException(400, "Нажмите «Получить код в Telegram» и поделитесь контактом в боте")
    if not r["code"]:
        raise HTTPException(400, "Сначала поделитесь контактом в боте — он пришлёт код")
    if r["code_ts"] < now_ms() - tg.REG_CODE_TTL_MS or r["tries"] >= tg.REG_CODE_TRIES:
        raise HTTPException(400, "Код устарел — нажмите «Получить код в Telegram» ещё раз")
    if not secrets.compare_digest(tg.reg_hash(code.strip()), r["code"]):
        conn.execute("UPDATE tg_regs SET tries = tries + 1 WHERE token = ?", (r["token"],))
        raise HTTPException(400, f"Неверный код. Осталось попыток: {tg.REG_CODE_TRIES - r['tries'] - 1}")
    return r


def welcome(conn: sqlite3.Connection, user_id: str, name: str, ref: str, ip: str = "") -> None:
    """Новому аккаунту — приветственный бонус на заказы. Если он пришёл по приглашению (cookie со ссылки /r/<код>
    или ref из ссылки «Поделиться»), закрепляется за пригласившим — кроме регистрации с адреса, с которого входил
    сам пригласивший (второй аккаунт того же человека)."""
    add_bonus(conn, user_id, WELCOME_BONUS)
    add_op(conn, user_id, "bonus", "bal", WELCOME_BONUS, "done", "Приветственный бонус на заказы")
    inviter = conn.execute("SELECT id FROM users WHERE id = ? AND id != ? AND blocked_at IS NULL AND deleted_at IS NULL",
                           (ref[:40], user_id)).fetchone() if ref else None
    if inviter and ip and conn.execute("SELECT 1 FROM user_ips WHERE user_id = ? AND ip = ?", (inviter["id"], ip)).fetchone():
        inviter = None
    if inviter:
        conn.execute("UPDATE users SET ref_by = ? WHERE id = ?", (inviter["id"], user_id))
        notify(conn, inviter["id"], "ref", f"По вашей ссылке зарегистрировался {html.unescape(name)}. Полгода вы будете"
               " получать долю комиссии с его продаж.", "#/dashboard/profile")


@router.post("/auth/register")
def register(body: RegisterIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    if guard.limited("reg-ip:" + guard.client_ip(request), REGISTER_PER_IP):
        raise HTTPException(429, "Слишком много регистраций с этого адреса — попробуйте через час")
    name, email = esc(body.name.strip()), auth.norm_email(body.email)
    via_tg = tg_reg_on()
    err = None
    if len(name) < 2 or len(name) > 40:
        err = "Введите имя (минимум 2 символа)"
    elif name_taken(conn, name):
        err = "Это имя занято, выберите другое"
    elif not via_tg and (not auth.EMAIL_RE.match(body.email.strip()) or len(body.email) > 120):
        err = "Введите корректный email"
    elif auth.password_error(body.password) or len(body.password) > 100:
        err = "Пароль: минимум 8 символов, буквы и цифры"
    elif body.password != body.password2:
        err = "Пароли не совпадают"
    elif not body.terms:
        err = "Подтвердите согласие с условиями"
    elif not via_tg and email == auth.ADMIN_EMAIL:
        err = "Этот email недоступен"
    elif not via_tg and conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        err = "Аккаунт с таким email уже существует. Войдите."
    if err:
        raise HTTPException(400, err)
    if body.check_only:
        return {"ok": True}
    if via_tg and not body.code.strip():
        raise HTTPException(400, "Введите код из Telegram")
    phone = chat = None
    if via_tg:
        r = check_reg_code(conn, request, body.code)
        phone, chat = r["phone"], r["chat_id"]
        if conn.execute("SELECT 1 FROM users WHERE phone = ? OR tg_chat = ?", (phone, chat)).fetchone():
            raise HTTPException(400, "Этот номер или Telegram уже зарегистрирован. Войдите по номеру телефона и паролю.")
        email = f"tg{secrets.token_hex(8)}{auth.TG_EMAIL_DOMAIN}"
    guard.hit("reg-ip:" + guard.client_ip(request))
    salt, pw_hash = auth.new_password(body.password)
    user_id = new_id("u")
    with tx(conn):
        conn.execute(
            "INSERT INTO users(id, name, email, salt, hash, created_at, phone, tg_chat) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, email, salt, pw_hash, now_ms(), phone, chat),
        )
        if via_tg:
            conn.execute("DELETE FROM tg_regs WHERE chat_id = ?", (chat,))
        welcome(conn, user_id, name, request.cookies.get("ruwork_ref", "") or body.ref.strip()[:40],
                guard.client_ip(request))
        auth.start_session(conn, response, user_id)
        auth.remember_ip(conn, user_id, guard.client_ip(request))
    response.delete_cookie("ruwork_ref", path="/")
    if via_tg:
        response.delete_cookie(REG_COOKIE, path="/")
        tg.send(chat, f"Аккаунт RUWORK «{name}» создан. Входите на сайте по номеру {phone} и паролю. Сюда будут "
                      "приходить уведомления о заказах и сообщениях. Отключить: /stop")
    return {"me": {"id": user_id, "name": name, "email": auth.shown_email(email), "phone": phone}}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    key = auth.login_key(body.email)
    if not key or not body.password:
        raise HTTPException(400, "Введите телефон или email и пароль")
    keys = ("login-ip:" + guard.client_ip(request), "login-email:" + key)
    if guard.limited(keys[0], LOGIN_PER_IP) or guard.limited(keys[1], LOGIN_PER_EMAIL):
        raise HTTPException(429, "Слишком много попыток входа — попробуйте через 15 минут или восстановите пароль")
    user = auth.find_user(conn, body.email)
    if user is None:
        auth.hash_pw(body.password, DUMMY_SALT)  # столько же времени, сколько проверка настоящего пароля
    if not user or not auth.check_pw(user, body.password):
        guard.hit(keys[0])
        guard.hit(keys[1])
        raise HTTPException(400, "Неверный логин или пароль")
    if user["blocked_at"] is not None:
        raise HTTPException(403, auth.blocked_message(user))
    if user["tg_chat"] and tg.TOKEN:
        # Второй шаг: код в Telegram. Сессия появится после ввода кода (POST /auth/login/code).
        ticket = secrets.token_urlsafe(24)
        conn.execute("DELETE FROM login_codes WHERE ts < ? OR user_id = ?", (now_ms() - LOGIN_CODE_TTL_MS, user["id"]))
        conn.execute("INSERT INTO login_codes(ticket, user_id, code, ts) VALUES (?, ?, '', ?)",
                     (token_hash(ticket), user["id"], now_ms()))
        send_login_code(conn, user, ticket)
        return {"need_code": True, "ticket": ticket}
    auth.start_session(conn, response, user["id"])
    auth.remember_ip(conn, user["id"], guard.client_ip(request))
    return {"me": auth.public_user(user)}


def send_login_code(conn: sqlite3.Connection, user, ticket: str) -> None:
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute("UPDATE login_codes SET code = ?, ts = ?, tries = 0 WHERE ticket = ?",
                 (token_hash("login:" + code), now_ms(), token_hash(ticket)))
    tg.send(user["tg_chat"], f"Код для входа на RUWORK: {code}\nДействует 10 минут. Если вы не входите на сайт — "
                             "никому не сообщайте код и смените пароль.")


def login_ticket(conn: sqlite3.Connection, ticket: str) -> sqlite3.Row:
    r = conn.execute("SELECT c.*, u.tg_chat, u.blocked_at FROM login_codes c JOIN users u ON u.id = c.user_id"
                     " WHERE c.ticket = ?", (token_hash(ticket),)).fetchone() if ticket else None
    if r is None or r["ts"] < now_ms() - LOGIN_CODE_TTL_MS or r["blocked_at"] is not None:
        raise HTTPException(400, "Код устарел — войдите ещё раз")
    return r


@router.post("/auth/login/code")
def login_code(body: LoginCodeIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    """Второй шаг входа: код из Telegram. Сотруднику он же подтверждает вход в админ-панель."""
    r = login_ticket(conn, body.ticket)
    if r["tries"] >= LOGIN_CODE_TRIES:
        raise HTTPException(400, "Слишком много неверных кодов — нажмите «Отправить ещё раз»")
    if not secrets.compare_digest(token_hash("login:" + body.code.strip()), r["code"]):
        conn.execute("UPDATE login_codes SET tries = tries + 1 WHERE ticket = ?", (r["ticket"],))
        raise HTTPException(400, f"Неверный код. Осталось попыток: {LOGIN_CODE_TRIES - r['tries'] - 1}")
    conn.execute("DELETE FROM login_codes WHERE ticket = ?", (r["ticket"],))
    user = conn.execute("SELECT * FROM users WHERE id = ?", (r["user_id"],)).fetchone()
    token = auth.start_session(conn, response, user["id"])
    if auth.staff_role(user):  # тот же второй фактор, что и для админ-панели — второй раз код не нужен
        conn.execute("UPDATE sessions SET mfa_at = ? WHERE token = ?", (now_ms(), token))
    auth.remember_ip(conn, user["id"], guard.client_ip(request))
    return {"me": auth.public_user(user)}


@router.post("/auth/login/resend")
def login_resend(body: LoginCodeIn, conn: sqlite3.Connection = Depends(get_db)):
    r = login_ticket(conn, body.ticket)
    guard.throttle("loginsend:" + r["user_id"], LOGIN_RESEND, "Код уже отправлен — подождите несколько минут")
    send_login_code(conn, r, body.ticket)
    return {"ok": True}


@router.post("/auth/reset")
def request_reset(body: ResetIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Заявка на сброс пароля. Если к аккаунту привязан Telegram, бот присылает кнопку подтверждения
    и пароль меняется сразу по ней. Иначе (или если кнопку не нажали) решает администратор."""
    contact = esc(body.contact.strip())[:100]
    if not (auth.norm_phone(body.email) or auth.EMAIL_RE.match(body.email.strip())) or len(body.email) > 120:
        raise HTTPException(400, "Введите телефон или email аккаунта")
    if auth.password_error(body.password) or len(body.password) > 100:
        raise HTTPException(400, "Новый пароль: минимум 8 символов, буквы и цифры")
    if body.password != body.password2:
        raise HTTPException(400, "Пароли не совпадают")
    if len(contact) < 3:
        raise HTTPException(400, "Укажите контакт для связи: Telegram или телефон")
    email = auth.login_key(body.email)
    keys = ("reset-ip:" + guard.client_ip(request), "reset-email:" + email)
    if guard.limited(keys[0], RESET_PER_IP) or guard.limited(keys[1], RESET_PER_EMAIL):
        raise HTTPException(429, "Слишком много заявок — попробуйте через час")
    guard.hit(keys[0])
    guard.hit(keys[1])
    salt, pw_hash = auth.new_password(body.password)  # считаем всегда, чтобы время ответа не выдавало наличие аккаунта
    user = auth.find_user(conn, body.email)
    # Ответ одинаковый, есть аккаунт или нет, чтобы по форме нельзя было проверить, зарегистрирован ли email.
    # Пароль администратора через заявку не сбрасывается: он меняется только в профиле.
    if user is not None and not auth.is_admin(user) and user["blocked_at"] is None:
        token = secrets.token_urlsafe(24) if user["tg_chat"] and tg.TOKEN else None
        with tx(conn):
            conn.execute("DELETE FROM password_resets WHERE user_id = ? AND status = 'pending'", (user["id"],))
            conn.execute(
                "INSERT INTO password_resets(id, user_id, contact, salt, hash, status, ts, token)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (new_id("pr"), user["id"], contact, salt, pw_hash, now_ms(), token_hash(token) if token else None),
            )
        if token:
            tg.send_button(user["tg_chat"],
                           "Кто-то запросил смену пароля для вашего аккаунта RUWORK. Если это вы — нажмите кнопку "
                           "в течение 30 минут, новый пароль заработает сразу. Если не вы — ничего не нажимайте "
                           "и не пересылайте это сообщение.",
                           "Да, сменить пароль", f"{tg.PUBLIC_URL}/api/auth/reset/tg/{token}")
        else:
            guard.alert(f"Заявка на сброс пароля: {user['name']} ({auth.shown_email(user['email']) or user['phone']}), контакт: {contact}",
                        "#/admin/resets")
    return {"ok": True}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def reset_page(title: str, text: str) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
        'initial-scale=1"><title>RUWORK</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;'
        'background:#0b0b12;color:#e8ecf4;font:16px/1.5 system-ui,sans-serif;padding:16px}div{max-width:420px;'
        'text-align:center}a{display:inline-block;margin-top:16px;padding:12px 22px;border-radius:12px;background:#6aa9ff;'
        f'color:#0b0b12;text-decoration:none;font-weight:600}}</style></head><body><div><h1>{html.escape(title)}</h1>'
        f'<p>{html.escape(text)}</p><a href="/#/dashboard">Перейти на RUWORK</a></div></body></html>')


@router.get("/auth/reset/tg/{token}", include_in_schema=False)
def confirm_reset_tg(token: str, conn: sqlite3.Connection = Depends(get_db)):
    """Кнопка из Telegram: подтверждает ожидающую заявку на сброс пароля."""
    with tx(conn):
        r = conn.execute("SELECT * FROM password_resets WHERE token = ? AND status = 'pending'",
                         (token_hash(token),)).fetchone()
        if r is None or r["ts"] < now_ms() - RESET_TG_TTL_MS:
            return reset_page("Ссылка недействительна", "Ссылка устарела или уже использована. "
                                                        "Запросите смену пароля ещё раз на странице входа.")
        conn.execute("UPDATE users SET salt = ?, hash = ? WHERE id = ?", (r["salt"], r["hash"], r["user_id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (r["user_id"],))  # выход на всех устройствах
        conn.execute("UPDATE password_resets SET status = 'done', token = NULL WHERE id = ?", (r["id"],))
    return reset_page("Пароль изменён", "Войдите на сайте с новым паролем. На остальных устройствах "
                                        "выполнен выход из аккаунта.")


@router.post("/auth/logout")
def logout(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    auth.end_session(conn, request, response)
    return {"ok": True}


@router.get("/ping")
def ping(user=Depends(auth.optional_user), conn: sqlite3.Connection = Depends(get_db)):
    """Пульс открытой вкладки: optional_user обновляет last_seen, по нему строится список «Онлайн».
    auth=false сообщает странице, что сессия завершена (например, пользователя заблокировали).
    new_sales — сколько покупок у продавца ещё без уведомления, notes — непрочитанные уведомления колокольчика."""
    if user is None:
        return {"ok": True, "auth": False, "new_sales": 0, "notes": 0}
    n = conn.execute("SELECT count(*) FROM orders WHERE seller_id = ? AND seller_seen = 0", (user["id"],)).fetchone()[0]
    return {"ok": True, "auth": True, "new_sales": n, "notes": unread_notes(conn, user["id"])}


@router.get("/state")
def state(user=Depends(auth.optional_user), conn: sqlite3.Connection = Depends(get_db)):
    if user is None:
        return {"me": None, "orders": [], "wallet": {"coins": 0, "ops": []}}
    return {"me": auth.public_user(user), **user_state(conn, user["id"])}


@router.patch("/me/name")
def change_name(body: NameIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    name = esc(body.name.strip())
    if len(name) < 2 or len(name) > 40:
        raise HTTPException(400, "Минимум 2 символа")
    if name == user["name"]:
        raise HTTPException(400, "Имя не изменилось")
    if name_taken(conn, name, user["id"], auth.is_admin(user)):
        raise HTTPException(400, "Это имя занято, выберите другое")
    with tx(conn):
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user["id"]))
        conn.execute("UPDATE gigs SET seller = ? WHERE owner_id = ?", (name, user["id"]))
    return {"me": auth.public_user(conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())}


@router.patch("/me/password")
def change_password(body: PasswordIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if not body.current:
        raise HTTPException(400, "Введите текущий пароль")
    if not auth.check_pw(user, body.current):
        raise HTTPException(400, "Текущий пароль неверный")
    if auth.password_error(body.password) or len(body.password) > 100:
        raise HTTPException(400, "Новый пароль: минимум 8 символов, буквы и цифры")
    if body.password != body.password2:
        raise HTTPException(400, "Пароли не совпадают")
    if body.password == body.current:
        raise HTTPException(400, "Новый пароль совпадает с текущим")
    salt, pw_hash = auth.new_password(body.password)
    conn.execute("UPDATE users SET salt = ?, hash = ? WHERE id = ?", (salt, pw_hash, user["id"]))
    return {"ok": True}
