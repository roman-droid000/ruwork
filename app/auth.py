"""Пароли (PBKDF2-SHA256), сессии в cookie и зависимости для текущего пользователя."""

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3

from fastapi import Depends, HTTPException, Request, Response

from .db import get_db, now_ms

SESSION_COOKIE = "ruwork_sid"
SESSION_TTL_MS = 30 * 24 * 3600 * 1000
PBKDF2_ITERATIONS = 150_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
# Единственный аккаунт администратора. Пароль задаётся при первом запуске через RUWORK_ADMIN_PASSWORD.
ADMIN_EMAIL = "phantom.crypta@mail.ru"
ADMIN_NAME = "ruwork.shop"


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


def optional_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.created_at > ?",
        (token, now_ms() - SESSION_TTL_MS),
    ).fetchone()


def require_user(user: sqlite3.Row | None = Depends(optional_user)) -> sqlite3.Row:
    if user is None:
        raise HTTPException(401, "Войдите или зарегистрируйтесь")
    return user


def is_admin(user: sqlite3.Row | None) -> bool:
    return user is not None and user["email"] == ADMIN_EMAIL


def require_admin(user: sqlite3.Row = Depends(require_user)) -> sqlite3.Row:
    if not is_admin(user):
        raise HTTPException(403, "Нет доступа")
    return user


def public_user(user: sqlite3.Row | None) -> dict | None:
    if user is None:
        return None
    out = {"id": user["id"], "name": user["name"], "email": user["email"]}
    if is_admin(user):
        out["admin"] = True
    return out
