"""Чат под объявлением. История хранится в БД.

Встроенный каталог: отвечает ИИ-ответчик от имени продавца (Claude или заготовки).
Объявления пользователей: живой диалог покупателя с продавцом. Сообщения покупателя хранятся
с owner = "u:<id покупателя>" и role = "me", ответы продавца — в том же диалоге с role = "seller".
"""

import logging
import os
import secrets
import sqlite3

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth
from ..db import get_db, now_ms
from ..logic import SOLD_SQL, ai_prompt, gig_out, greeting, local_reply, notify

router = APIRouter(prefix="/api")
log = logging.getLogger("ruwork.chat")

VISITOR_COOKIE = "ruwork_vid"
MODEL = os.environ.get("RUWORK_AI_MODEL", "claude-opus-5-5")
# ИИ включается, только если задан ключ; иначе работают автоответы по ключевым словам.
client = anthropic.AsyncAnthropic(timeout=30) if os.environ.get("ANTHROPIC_API_KEY") else None


class MessageIn(BaseModel):
    text: str


class ReplyIn(BaseModel):
    to: str  # id покупателя
    text: str


def chat_owner(request: Request, response: Response, user) -> str:
    """Вошедший пользователь — по id, гость — по отдельной cookie, чтобы история не смешивалась."""
    if user is not None:
        return "u:" + user["id"]
    vid = request.cookies.get(VISITOR_COOKIE)
    if not vid or len(vid) > 64:
        vid = secrets.token_urlsafe(16)
        response.set_cookie(VISITOR_COOKIE, vid, max_age=365 * 24 * 3600, httponly=True, samesite="lax", path="/")
    return "v:" + vid


def load_gig(conn: sqlite3.Connection, gig_id: str) -> dict:
    row = conn.execute(f"SELECT g.*, {SOLD_SQL} FROM gigs g WHERE g.id = ?", (gig_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Кворк не найден")
    return gig_out(row)


def clean_text(text: str) -> str:
    text = text.strip()[:1000]
    if not text:
        raise HTTPException(400, "Введите сообщение")
    return text


def threads(conn: sqlite3.Connection, gig_id: str) -> dict:
    """Все диалоги покупателей по объявлению — для его продавца. Свежие диалоги сверху."""
    rows = conn.execute(
        "SELECT m.owner, m.role, m.text, m.ts, u.id AS buyer, u.name FROM chat_messages m"
        " JOIN users u ON m.owner = 'u:' || u.id WHERE m.gig_id = ? ORDER BY m.id",
        (gig_id,),
    ).fetchall()
    by_buyer: dict[str, dict] = {}
    for r in rows:
        t = by_buyer.setdefault(r["buyer"], {"buyer": r["buyer"], "name": r["name"], "msgs": []})
        t["msgs"].append({"r": r["role"], "t": r["text"], "ts": r["ts"]})
    out = sorted(by_buyer.values(), key=lambda t: t["msgs"][-1]["ts"], reverse=True)
    for t in out:
        t["msgs"] = t["msgs"][-50:]
    return {"seller": True, "threads": out}


def history(conn: sqlite3.Connection, owner: str, gig_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, text FROM chat_messages WHERE owner = ? AND gig_id = ? ORDER BY id DESC LIMIT 50",
        (owner, gig_id),
    ).fetchall()
    return [{"r": r["role"], "t": r["text"]} for r in reversed(rows)]


async def ai_reply(g: dict, about: str, msgs: list[dict]) -> str:
    if client is None:
        return ""
    try:
        resp = await client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": ai_prompt(g, about, [(m["r"], m["t"]) for m in msgs])}],
        )
    except anthropic.APIError as e:
        log.warning("Claude API недоступен: %s", e)
        return ""
    if resp.stop_reason == "refusal":
        return ""
    return "".join(b.text for b in resp.content if b.type == "text").strip()[:600]


@router.get("/chat/{gig_id}")
def get_chat(gig_id: str, request: Request, response: Response,
             user=Depends(auth.optional_user), conn: sqlite3.Connection = Depends(get_db)):
    g = load_gig(conn, gig_id)
    if g.get("owner"):
        if user is None:
            return []
        if g["owner"] == user["id"]:
            return threads(conn, gig_id)
    owner = chat_owner(request, response, user)
    return history(conn, owner, gig_id)


@router.post("/chat/{gig_id}")
async def send_message(gig_id: str, body: MessageIn, request: Request, response: Response,
                       user=Depends(auth.optional_user), conn: sqlite3.Connection = Depends(get_db)):
    text = clean_text(body.text)
    g = load_gig(conn, gig_id)
    if g.get("owner"):
        # Объявление пользователя: сообщение уходит продавцу, ИИ не отвечает.
        if user is None:
            raise HTTPException(401, "Войдите, чтобы написать продавцу")
        if g["owner"] == user["id"]:
            raise HTTPException(400, "Это ваше объявление: отвечайте покупателям в их диалогах")
        owner = "u:" + user["id"]
        conn.execute(
            "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'me', ?, ?)",
            (owner, gig_id, text, now_ms()),
        )
        notify(conn, g["owner"], "msg", f"Новое сообщение от {user['name']} по «{g['title']}»: {text[:120]}",
               f"#/gig/{gig_id}")
        return history(conn, owner, gig_id)
    owner = chat_owner(request, response, user)
    conn.execute(
        "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'me', ?, ?)",
        (owner, gig_id, text, now_ms()),
    )
    seller = conn.execute("SELECT about FROM sellers WHERE name = ?", (g["seller"],)).fetchone()
    about = seller["about"] if seller and not g.get("mine") else (
        "Новый продавец RUWORK: недавно присоединился к платформе и только начинает набирать отзывы."
    )
    msgs = [{"r": "ai", "t": greeting(g)}] + history(conn, owner, gig_id)
    answer = await ai_reply(g, about, msgs) or local_reply(g, text)
    conn.execute(
        "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'ai', ?, ?)",
        (owner, gig_id, answer, now_ms()),
    )
    return history(conn, owner, gig_id)


@router.post("/chat/{gig_id}/reply")
def seller_reply(gig_id: str, body: ReplyIn, user=Depends(auth.require_user),
                 conn: sqlite3.Connection = Depends(get_db)):
    """Ответ продавца покупателю в диалоге под своим объявлением."""
    text = clean_text(body.text)
    g = load_gig(conn, gig_id)
    if g.get("owner") != user["id"]:
        raise HTTPException(403, "Отвечать может только автор объявления")
    owner = "u:" + body.to
    if not conn.execute("SELECT 1 FROM chat_messages WHERE owner = ? AND gig_id = ? LIMIT 1", (owner, gig_id)).fetchone():
        raise HTTPException(404, "Диалог не найден")
    conn.execute(
        "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'seller', ?, ?)",
        (owner, gig_id, text, now_ms()),
    )
    notify(conn, body.to, "msg", f"Продавец {g['seller']} ответил по «{g['title']}»: {text[:120]}", f"#/gig/{gig_id}")
    return threads(conn, gig_id)
