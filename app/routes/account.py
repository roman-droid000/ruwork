"""Регистрация, вход, профиль и общее состояние аккаунта."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth
from ..db import get_db, now_ms, tx
from ..logic import esc, new_id, user_state

router = APIRouter(prefix="/api")


class RegisterIn(BaseModel):
    name: str
    email: str
    password: str
    password2: str
    terms: bool


class LoginIn(BaseModel):
    email: str
    password: str


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
def register(body: RegisterIn, response: Response, conn: sqlite3.Connection = Depends(get_db)):
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
    salt, pw_hash = auth.new_password(body.password)
    user_id = new_id("u")
    with tx(conn):
        conn.execute(
            "INSERT INTO users(id, name, email, salt, hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, email, salt, pw_hash, now_ms()),
        )
        auth.start_session(conn, response, user_id)
    return {"me": {"id": user_id, "name": name, "email": email}}


@router.post("/auth/login")
def login(body: LoginIn, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    email = auth.norm_email(body.email)
    if not email or not body.password:
        raise HTTPException(400, "Введите email и пароль")
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or not auth.check_pw(user, body.password):
        raise HTTPException(400, "Неверный email или пароль")
    auth.start_session(conn, response, user["id"])
    return {"me": auth.public_user(user)}


@router.post("/auth/logout")
def logout(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    auth.end_session(conn, request, response)
    return {"ok": True}


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
