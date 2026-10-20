"""Отзывы: покупатель оценивает выполненный заказ (звёзды, текст, фото результата), продавец отвечает.

Один заказ — один отзыв. Оценка сразу входит в рейтинг объявления (add_review_rating), а через него —
в рейтинг продавца (seller_ratings).
"""

import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import auth
from ..db import UPLOAD_DIR, get_db, now_ms, tx
from ..logic import ST_DONE, add_review_rating, esc, new_id, notify
from .orders import COVER_MAX, NAME_RE, cover_ext, load_order

router = APIRouter()

REVIEW_SQL = (
    "SELECT r.*, u.name AS buyer_name, u.avatar AS buyer_avatar FROM reviews r LEFT JOIN users u ON u.id = r.buyer_id"
)


class ReviewIn(BaseModel):
    stars: int
    text: str


class ReplyIn(BaseModel):
    text: str


def review_out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "order": r["order_id"], "gig": r["gig_id"], "seller": r["seller_id"], "stars": r["stars"],
            "text": r["text"], "photo": r["photo"], "reply": r["reply"], "reply_at": r["reply_at"], "ts": r["ts"],
            "buyer_name": r["buyer_name"] or "Покупатель", "buyer_avatar": r["buyer_avatar"]}


def order_review(conn: sqlite3.Connection, order_id: str) -> dict | None:
    r = conn.execute(REVIEW_SQL + " WHERE r.order_id = ?", (order_id,)).fetchone()
    return review_out(r) if r else None


@router.get("/api/gigs/{gig_id}/reviews")
def gig_reviews(gig_id: str, conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(REVIEW_SQL + " WHERE r.gig_id = ? ORDER BY r.ts DESC LIMIT 200", (gig_id,)).fetchall()
    return [review_out(r) for r in rows]


@router.post("/api/orders/{order_id}/review")
def create_review(order_id: str, body: ReviewIn, user=Depends(auth.require_user),
                  conn: sqlite3.Connection = Depends(get_db)):
    text = esc(body.text.strip())[:1000]
    if not 1 <= body.stars <= 5:
        raise HTTPException(400, "Поставьте оценку от 1 до 5 звёзд")
    if len(body.text.strip()) < 5:
        raise HTTPException(400, "Напишите пару слов о работе")
    with tx(conn):
        o, role = load_order(conn, order_id, user)
        if role != "buyer":
            raise HTTPException(403, "Отзыв оставляет покупатель")
        if o["status"] != ST_DONE:
            raise HTTPException(400, "Отзыв можно оставить после выполнения заказа")
        if conn.execute("SELECT 1 FROM reviews WHERE order_id = ?", (o["id"],)).fetchone():
            raise HTTPException(400, "Отзыв по этому заказу уже оставлен")
        conn.execute(
            "INSERT INTO reviews(id, order_id, gig_id, seller_id, buyer_id, stars, text, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("rv"), o["id"], o["gig_id"], o["seller_id"], user["id"], body.stars, text, now_ms()),
        )
        add_review_rating(conn, o["gig_id"], body.stars)
        notify(conn, o["seller_id"], "review", f"Новый отзыв {'★' * body.stars} по заказу {o['num']} «{o['title']}»: {text[:120]}",
               f"#/gig/{o['gig_id']}")
        return order_review(conn, o["id"])


def own_review(conn: sqlite3.Connection, review_id: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Отзыв не найден")
    return r


@router.post("/api/reviews/{review_id}/photo")
async def review_photo(review_id: str, request: Request, user=Depends(auth.require_user),
                       conn: sqlite3.Connection = Depends(get_db)):
    """Фото к отзыву (скриншот результата). Браузер заранее уменьшает картинку, как обложку."""
    r = own_review(conn, review_id)
    if r["buyer_id"] != user["id"]:
        raise HTTPException(403, "Фото добавляет автор отзыва")
    data = await request.body()
    if not data or len(data) > COVER_MAX:
        raise HTTPException(400, "Фото должно быть не больше 3 МБ")
    name = f"{review_id}-{secrets.token_hex(4)}{cover_ext(data, 'Фотография')}"
    (UPLOAD_DIR / "reviews" / name).write_bytes(data)
    conn.execute("UPDATE reviews SET photo = ? WHERE id = ?", (name, review_id))
    if r["photo"] and NAME_RE.match(r["photo"]):
        (UPLOAD_DIR / "reviews" / r["photo"]).unlink(missing_ok=True)
    return order_review(conn, r["order_id"])


@router.post("/api/reviews/{review_id}/reply")
def review_reply(review_id: str, body: ReplyIn, user=Depends(auth.require_user),
                 conn: sqlite3.Connection = Depends(get_db)):
    """Ответ продавца на отзыв; можно исправить, пустой текст убирает ответ."""
    r = own_review(conn, review_id)
    if not r["seller_id"] or r["seller_id"] != user["id"]:
        raise HTTPException(403, "Ответить может только продавец")
    text = esc(body.text.strip())[:1000]
    conn.execute("UPDATE reviews SET reply = ?, reply_at = ? WHERE id = ?", (text, now_ms() if text else None, review_id))
    if text and not r["reply"]:
        notify(conn, r["buyer_id"], "review", f"Продавец ответил на ваш отзыв: {text[:120]}", f"#/gig/{r['gig_id']}")
    return order_review(conn, r["order_id"])


@router.get("/media/reviews/{name}", include_in_schema=False)
def review_file(name: str):
    path = UPLOAD_DIR / "reviews" / name
    if not NAME_RE.match(name) or not path.is_file():
        raise HTTPException(404, "Не найдено")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable",
                                       "X-Content-Type-Options": "nosniff"})
