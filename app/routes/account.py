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
from ..logic import esc, new_id, unread_notes, user_state

router = APIRouter(prefix="/api")

# Ограничения частоты: (сколько, за сколько секунд). Ключ по email общий с входом в мини-приложении.
LOGIN_PER_IP = (20, 900)  # неудачных входов с одного адреса
LOGIN_PER_EMAIL = (10, 900)  # неудачных входов в один аккаунт с любых адресов — против перебора пароля
REGISTER_PER_IP = (5, 3600)
RESET_PER_IP = (5, 3600)
RESET_PER_EMAIL = (3, 3600)
RESET_TG_TTL_MS = 30 * 60 * 1000  # ссылка подтверждения сброса из Telegram действует 30 минут
DUMMY_SALT = b"\0" * 16  # хэш для несуществующего email: время ответа не выдаёт, есть ли аккаунт


class RegisterIn(BaseModel):
    name: str
    email: str
    password: str
    password2: str
    terms: bool


class LoginIn(BaseModel):
    email: str
    password: str


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


@router.post("/auth/register")
def register(body: RegisterIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    if guard.limited("reg-ip:" + guard.client_ip(request), REGISTER_PER_IP):
        raise HTTPException(429, "Слишком много регистраций с этого адреса — попробуйте через час")
    name, email = esc(body.name.strip()), auth.norm_email(body.email)
    err = None
    if len(name) < 2 or len(name) > 40:
        err = "Введите имя (минимум 2 символа)"
    elif name_taken(conn, name):
        err = "Это имя занято, выберите другое"
    elif not auth.EMAIL_RE.match(body.email.strip()) or len(body.email) > 120:
        err = "Введите корректный email"
    elif auth.password_error(body.password) or len(body.password) > 100:
        err = "Пароль: минимум 8 символов, буквы и цифры"
    elif body.password != body.password2:
        err = "Пароли не совпадают"
    elif not body.terms:
        err = "Подтвердите согласие с условиями"
    elif email == auth.ADMIN_EMAIL:
        err = "Этот email недоступен"
    elif conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        err = "Аккаунт с таким email уже существует. Войдите."
    if err:
        raise HTTPException(400, err)
    guard.hit("reg-ip:" + guard.client_ip(request))
    salt, pw_hash = auth.new_password(body.password)
    user_id = new_id("u")
    with tx(conn):
        conn.execute(
            "INSERT INTO users(id, name, email, salt, hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, email, salt, pw_hash, now_ms()),
        )
        auth.start_session(conn, response, user_id)
        auth.remember_ip(conn, user_id, guard.client_ip(request))
    return {"me": {"id": user_id, "name": name, "email": email}}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    email = auth.norm_email(body.email)
    if not email or not body.password:
        raise HTTPException(400, "Введите email и пароль")
    keys = ("login-ip:" + guard.client_ip(request), "login-email:" + email)
    if guard.limited(keys[0], LOGIN_PER_IP) or guard.limited(keys[1], LOGIN_PER_EMAIL):
        raise HTTPException(429, "Слишком много попыток входа — попробуйте через 15 минут или восстановите пароль")
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None:
        auth.hash_pw(body.password, DUMMY_SALT)  # столько же времени, сколько проверка настоящего пароля
    if not user or not auth.check_pw(user, body.password):
        guard.hit(keys[0])
        guard.hit(keys[1])
        raise HTTPException(400, "Неверный email или пароль")
    if user["blocked_at"] is not None:
        raise HTTPException(403, auth.blocked_message(user))
    auth.start_session(conn, response, user["id"])
    auth.remember_ip(conn, user["id"], guard.client_ip(request))
    return {"me": auth.public_user(user)}


@router.post("/auth/reset")
def request_reset(body: ResetIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Заявка на сброс пароля. Если к аккаунту привязан Telegram, бот присылает кнопку подтверждения
    и пароль меняется сразу по ней. Иначе (или если кнопку не нажали) решает администратор."""
    contact = esc(body.contact.strip())[:100]
    if not auth.EMAIL_RE.match(body.email.strip()) or len(body.email) > 120:
        raise HTTPException(400, "Введите корректный email")
    if auth.password_error(body.password) or len(body.password) > 100:
        raise HTTPException(400, "Новый пароль: минимум 8 символов, буквы и цифры")
    if body.password != body.password2:
        raise HTTPException(400, "Пароли не совпадают")
    if len(contact) < 3:
        raise HTTPException(400, "Укажите контакт для связи: Telegram или телефон")
    email = auth.norm_email(body.email)
    keys = ("reset-ip:" + guard.client_ip(request), "reset-email:" + email)
    if guard.limited(keys[0], RESET_PER_IP) or guard.limited(keys[1], RESET_PER_EMAIL):
        raise HTTPException(429, "Слишком много заявок — попробуйте через час")
    guard.hit(keys[0])
    guard.hit(keys[1])
    salt, pw_hash = auth.new_password(body.password)  # считаем всегда, чтобы время ответа не выдавало наличие аккаунта
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
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
            guard.alert(f"Заявка на сброс пароля: {user['name']} ({user['email']}), контакт: {contact}",
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
