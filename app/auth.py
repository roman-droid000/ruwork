"""Пароли (PBKDF2-SHA256), сессии в cookie и зависимости для текущего пользователя."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time

from fastapi import Depends, HTTPException, Request, Response

from .db import get_db, now_ms

SESSION_COOKIE = "ruwork_sid"
SESSION_TTL_MS = 30 * 24 * 3600 * 1000
ONLINE_MS = 2 * 60 * 1000  # пользователь «онлайн», если был активен за последние 2 минуты
SEEN_THROTTLE_MS = 30 * 1000  # last_seen пишем не чаще раза в 30 секунд
PBKDF2_ITERATIONS = 150_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
# Единственный аккаунт администратора. Пароль задаётся при первом запуске через RUWORK_ADMIN_PASSWORD.
ADMIN_EMAIL = "phantom.crypta@mail.ru"
ADMIN_NAME = "ruwork.shop"
# Модератор (users.role = 'moder'): жалобы, объявления, споры, пользователи и чаты, но не деньги.
ROLES = {"moder": "Модератор"}
# Второй фактор для админ-панели: код из Telegram, подтверждение действует MFA_TTL_MS в этой сессии.
# Работает, только если настроен бот; RUWORK_ADMIN_2FA=0 выключает (например, для локальной разработки).
MFA_TTL_MS = 12 * 3600 * 1000


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_pw(password: str, salt: bytes) -> str:
    return b64(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS, 32))


def new_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    return b64(salt), hash_pw(password, salt)


def check_pw(user: sqlite3.Row, password: str) -> bool:
    return hmac.compare_digest(hash_pw(password, base64.b64decode(user["salt"])), user["hash"])


def password_error(password: str) -> str | None:
    if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "минимум 8 символов, буквы и цифры"
    return None


def norm_email(email: str) -> str:
    """Gmail игнорирует точки и +метки, поэтому приводим адрес к одному виду: так нельзя завести дубль."""
    email = email.strip().lower()
    parts = email.split("@")
    if len(parts) != 2:
        return email
    if parts[1] in ("gmail.com", "googlemail.com"):
        local = parts[0].split("+")[0].replace(".", "")
        return (local or parts[0]) + "@gmail.com"
    return email


def start_session(conn: sqlite3.Connection, response: Response, user_id: str) -> None:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, now_ms())
    )
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_MS // 1000, httponly=True, samesite="lax", path="/"
    )


def end_session(conn: sqlite3.Connection, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    response.delete_cookie(SESSION_COOKIE, path="/")


def client_ip(request: Request) -> str:
    """Адрес посетителя. За nginx настоящий адрес приходит в X-Real-IP / X-Forwarded-For."""
    ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return (ip or (request.client.host if request.client else "?"))[:64]


def remember_ip(conn: sqlite3.Connection, user_id: str, ip: str) -> None:
    """Адреса, с которых заходил пользователь: по общим адресам админка ищет связанные аккаунты."""
    n = now_ms()
    conn.execute("INSERT INTO user_ips(user_id, ip, first_ts, last_ts) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT(user_id, ip) DO UPDATE SET last_ts = excluded.last_ts", (user_id, ip, n, n))


def optional_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ? AND s.created_at > ? AND u.blocked_at IS NULL",
        (token, now_ms() - SESSION_TTL_MS),
    ).fetchone()
    if user is not None and (user["last_seen"] or 0) < now_ms() - SEEN_THROTTLE_MS and not conn.in_transaction:
        conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now_ms(), user["id"]))
        remember_ip(conn, user["id"], client_ip(request))
    return user


def blocked_message(user: sqlite3.Row) -> str:
    reason, until = user["block_reason"], user["blocked_until"]
    return ("Аккаунт заблокирован администратором"
            + (" до " + time.strftime("%d.%m.%Y %H:%M", time.localtime(until / 1000)) if until else "")
            + (f". Причина: {reason}" if reason else ""))


def require_user(user: sqlite3.Row | None = Depends(optional_user)) -> sqlite3.Row:
    if user is None:
        raise HTTPException(401, "Войдите или зарегистрируйтесь")
    return user


def is_admin(user: sqlite3.Row | None) -> bool:
    return user is not None and user["email"] == ADMIN_EMAIL


def staff_role(user: sqlite3.Row | None) -> str:
    """'admin' — владелец, 'moder' — модератор, '' — обычный пользователь."""
    if user is None:
        return ""
    if is_admin(user):
        return "admin"
    return user["role"] if user["role"] in ROLES else ""


def mfa_on() -> bool:
    from . import tg
    return bool(tg.TOKEN) and os.environ.get("RUWORK_ADMIN_2FA", "1") != "0"


def session_row(conn: sqlite3.Connection, request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    return conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone() if token else None


def mfa_ok(conn: sqlite3.Connection, request: Request) -> bool:
    if not mfa_on():
        return True
    s = session_row(conn, request)
    return bool(s and s["mfa_at"] and s["mfa_at"] > now_ms() - MFA_TTL_MS)


def require_staff(request: Request, user: sqlite3.Row = Depends(require_user),
                  conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    """Админ-панель: сотрудник и подтверждённый в этой сессии код из Telegram (428 — нужен код)."""
    if not staff_role(user):
        raise HTTPException(403, "Нет доступа")
    if not mfa_ok(conn, request):
        raise HTTPException(428, "Подтвердите вход в админ-панель кодом из Telegram")
    return user


def require_admin(user: sqlite3.Row = Depends(require_staff)) -> sqlite3.Row:
    """Только владелец: деньги, заявки, роли, настройки, журнал."""
    if not is_admin(user):
        raise HTTPException(403, "Это действие доступно только администратору")
    return user


def public_user(user: sqlite3.Row | None) -> dict | None:
    if user is None:
        return None
    out = {"id": user["id"], "name": user["name"], "email": user["email"],
           "about": user["about"] if "about" in user.keys() else "",
           "avatar": user["avatar"] if "avatar" in user.keys() else None,
           "tg": bool(user["tg_chat"]) if "tg_chat" in user.keys() else False,
           "vacation_until": user["vacation_until"] if "vacation_until" in user.keys() else None,
           "vacation_note": user["vacation_note"] if "vacation_note" in user.keys() else ""}
    if is_admin(user):
        out["admin"] = True
    if staff_role(user):
        out["staff"] = staff_role(user)
    return out
