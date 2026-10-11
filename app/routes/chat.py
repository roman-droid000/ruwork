"""Чат с ИИ-ответчиком продавца. История хранится в БД."""

import logging
import os
import secrets
import sqlite3

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth
from ..db import get_db, now_ms
from ..logic import ai_prompt, gig_out, greeting, local_reply

router = APIRouter(prefix="/api")
log = logging.getLogger("ruwork.chat")

VISITOR_COOKIE = "ruwork_vid"
MODEL = os.environ.get("RUWORK_AI_MODEL", "claude-opus-5-5")
# ИИ включается, только если задан ключ; иначе работают автоответы по ключевым словам.
client = anthropic.AsyncAnthropic(timeout=30) if os.environ.get("ANTHROPIC_API_KEY") else None


class MessageIn(BaseModel):
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
    row = conn.execute("SELECT * FROM gigs WHERE id = ?", (gig_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Кворк не найден")
    return gig_out(row)


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
    owner = chat_owner(request, response, user)
    return history(conn, owner, gig_id)


@router.post("/chat/{gig_id}")
async def send_message(gig_id: str, body: MessageIn, request: Request, response: Response,
                       user=Depends(auth.optional_user), conn: sqlite3.Connection = Depends(get_db)):
    text = body.text.strip()[:1000]
    if not text:
        raise HTTPException(400, "Введите сообщение")
    g = load_gig(conn, gig_id)
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
