"""Страница заказа: обложки объявлений, результат работы и подтверждение получения.

Продавец загружает файлы результата частями (CHUNK), чтобы не упираться в лимит размера запроса на прокси.
Первый загруженный файл переводит заказ «На проверке». Покупатель подтверждает получение (или через 7 дней
это происходит автоматически, см. tick) — заказ «Выполнен», и только тогда монеты за заказ, оплаченный с баланса,
переводятся продавцу. До этого они хранятся на платформе.
"""

import mimetypes
import re
import secrets
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import auth
from ..db import UPLOAD_DIR, get_db, now_ms, tx
from ..logic import AUTO_CONFIRM_MS, ST_REVIEW, ST_WORK, TIERS, complete_order, user_state

router = APIRouter()

COVER_MAX = 3 * 1024 * 1024
FILE_MAX = 50 * 1024 * 1024
FILES_PER_ORDER = 10
CHUNK = 512 * 1024
COVER_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"RIFF": ".webp"}
# Эти типы безопасно показывать прямо в браузере; всё остальное отдаётся только как скачивание.
INLINE = {"image/jpeg", "image/png", "image/webp", "image/gif", "video/mp4", "video/webm", "audio/mpeg", "audio/ogg",
          "application/pdf"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}\.(jpg|png|webp)$")


def cover_ext(data: bytes) -> str:
    for magic, ext in COVER_TYPES.items():
        if data.startswith(magic) and (ext != ".webp" or data[8:12] == b"WEBP"):
            return ext
    raise HTTPException(400, "Обложка должна быть картинкой JPG, PNG или WebP")


def remove_cover(name: str | None) -> None:
    if name and NAME_RE.match(name):
        (UPLOAD_DIR / "covers" / name).unlink(missing_ok=True)


@router.post("/api/gigs/{gig_id}/cover")
async def upload_cover(gig_id: str, request: Request, user=Depends(auth.require_user),
                       conn: sqlite3.Connection = Depends(get_db)):
    g = conn.execute("SELECT cover FROM gigs WHERE id = ? AND owner_id = ?", (gig_id, user["id"])).fetchone()
    if not g:
        raise HTTPException(404, "Объявление не найдено")
    data = await request.body()
    if not data or len(data) > COVER_MAX:
        raise HTTPException(400, "Обложка должна быть не больше 3 МБ")
    name = f"{gig_id}-{secrets.token_hex(4)}{cover_ext(data)}"
    (UPLOAD_DIR / "covers" / name).write_bytes(data)
    conn.execute("UPDATE gigs SET cover = ? WHERE id = ?", (name, gig_id))
    remove_cover(g["cover"])
    return {"cover": name}


@router.get("/media/covers/{name}", include_in_schema=False)
def cover_file(name: str):
    path = UPLOAD_DIR / "covers" / name
    if not NAME_RE.match(name) or not path.is_file():
        raise HTTPException(404, "Не найдено")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable",
                                       "X-Content-Type-Options": "nosniff"})


def load_order(conn: sqlite3.Connection, order_id: str, user) -> tuple[sqlite3.Row, str]:
    """Заказ и роль смотрящего: покупатель, продавец или администратор (только просмотр)."""
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if o and o["user_id"] == user["id"]:
        return o, "buyer"
    if o and o["seller_id"] and o["seller_id"] == user["id"]:
        return o, "seller"
    if o and auth.is_admin(user):
        return o, "admin"
    raise HTTPException(404, "Заказ не найден")


def order_detail(conn: sqlite3.Connection, o: sqlite3.Row, role: str) -> dict:
    g = conn.execute("SELECT cat, sub, cover FROM gigs WHERE id = ?", (o["gig_id"],)).fetchone()
    buyer = conn.execute("SELECT name FROM users WHERE id = ?", (o["user_id"],)).fetchone()
    seller = conn.execute("SELECT name FROM users WHERE id = ?", (o["seller_id"],)).fetchone() if o["seller_id"] else None
    files = conn.execute(
        "SELECT id, name, mime, size, received, done, ts FROM order_files WHERE order_id = ? ORDER BY ts", (o["id"],)
    ).fetchall()
    return {
        "id": o["id"], "num": o["num"], "gig": o["gig_id"], "title": o["title"], "tier": TIERS[o["tier"]][0],
        "status": o["status"], "amount": o["amount"], "method": o["method"], "ts": o["created_at"],
        "confirmed_at": o["confirmed_at"], "role": role,
        "auto_at": o["reviewed_at"] + AUTO_CONFIRM_MS if o["status"] == ST_REVIEW and o["reviewed_at"] else None, "buyer": o["user_id"],
        "buyer_name": buyer["name"] if buyer else "—", "seller_name": seller["name"] if seller else None,
        "has_seller": bool(o["seller_id"]), "cat": g["cat"] if g else None, "sub": g["sub"] if g else None,
        "cover": g["cover"] if g else None, "gig_exists": bool(g),
        # Покупатель видит только полностью загруженные файлы; продавец — и недогруженные, чтобы удалить их.
        "files": [dict(f) for f in files if f["done"] or role == "seller"],
    }


@router.get("/api/orders/{order_id}")
def get_order(order_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    user_state(conn, user["id"])  # таймеры статусов
    o, role = load_order(conn, order_id, user)
    if role != "buyer":
        user_state(conn, o["user_id"])
        o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return order_detail(conn, o, role)


class FileIn(BaseModel):
    name: str
    size: int
    mime: str = ""


def seller_order(conn: sqlite3.Connection, order_id: str, user) -> sqlite3.Row:
    user_state(conn, user["id"])
    o, role = load_order(conn, order_id, user)
    if role != "seller":
        raise HTTPException(403, "Загружать результат может только продавец")
    if o["status"] not in (ST_WORK, ST_REVIEW):
        raise HTTPException(400, "Результат можно загрузить, когда заказ в работе и ещё не подтверждён")
    return o


@router.post("/api/orders/{order_id}/files")
def start_file(order_id: str, body: FileIn, user=Depends(auth.require_user),
               conn: sqlite3.Connection = Depends(get_db)):
    o = seller_order(conn, order_id, user)
    name = re.sub(r"[\x00-\x1f/\\]", "_", body.name.strip())[:120] or "file"
    if not 0 < body.size <= FILE_MAX:
        raise HTTPException(400, "Файл должен быть не больше 50 МБ")
    if conn.execute("SELECT count(*) FROM order_files WHERE order_id = ?", (o["id"],)).fetchone()[0] >= FILES_PER_ORDER:
        raise HTTPException(400, f"К заказу можно приложить не больше {FILES_PER_ORDER} файлов")
    mime = (body.mime or mimetypes.guess_type(name)[0] or "application/octet-stream")[:100]
    fid = "f" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO order_files(id, order_id, name, mime, size, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (fid, o["id"], name, mime, body.size, now_ms()),
    )
    (UPLOAD_DIR / "results" / fid).write_bytes(b"")
    return {"id": fid, "chunk": CHUNK}


@router.put("/api/orders/{order_id}/files/{file_id}")
async def put_chunk(order_id: str, file_id: str, offset: int, request: Request, user=Depends(auth.require_user),
                    conn: sqlite3.Connection = Depends(get_db)):
    o = seller_order(conn, order_id, user)
    f = conn.execute("SELECT * FROM order_files WHERE id = ? AND order_id = ?", (file_id, o["id"])).fetchone()
    if not f or f["done"]:
        raise HTTPException(404, "Файл не найден")
    if offset != f["received"]:
        raise HTTPException(409, "Загрузка сбилась, начните заново")
    data = await request.body()
    if not data or len(data) > CHUNK or f["received"] + len(data) > f["size"]:
        raise HTTPException(400, "Неверная часть файла")
    with open(UPLOAD_DIR / "results" / file_id, "ab") as fh:
        fh.write(data)
    received = f["received"] + len(data)
    done = received == f["size"]
    with tx(conn):
        conn.execute("UPDATE order_files SET received = ?, done = ? WHERE id = ?", (received, int(done), file_id))
        if done:
            conn.execute("UPDATE orders SET status = ?, reviewed_at = ? WHERE id = ? AND status = ?",
                         (ST_REVIEW, now_ms(), o["id"], ST_WORK))
            conn.execute(
                "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
                ("u:" + o["user_id"], o["gig_id"],
                 f"Продавец загрузил результат по заказу {o['num']}: {f['name']}. Проверьте и подтвердите получение.",
                 now_ms()),
            )
    return {"received": received, "done": done}


@router.delete("/api/orders/{order_id}/files/{file_id}")
def delete_file(order_id: str, file_id: str, user=Depends(auth.require_user),
                conn: sqlite3.Connection = Depends(get_db)):
    o = seller_order(conn, order_id, user)
    with tx(conn):
        if not conn.execute("DELETE FROM order_files WHERE id = ? AND order_id = ?", (file_id, o["id"])).rowcount:
            raise HTTPException(404, "Файл не найден")
        # Удалили последний файл — заказ снова «В работе», подтверждать нечего.
        if not conn.execute("SELECT 1 FROM order_files WHERE order_id = ? AND done = 1", (o["id"],)).fetchone():
            conn.execute("UPDATE orders SET status = ?, reviewed_at = NULL WHERE id = ? AND status = ?",
                         (ST_WORK, o["id"], ST_REVIEW))
    (UPLOAD_DIR / "results" / file_id).unlink(missing_ok=True)
    return order_detail(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (o["id"],)).fetchone(), "seller")


@router.get("/api/orders/{order_id}/files/{file_id}")
def get_file(order_id: str, file_id: str, dl: int = 0, user=Depends(auth.require_user),
             conn: sqlite3.Connection = Depends(get_db)):
    o, role = load_order(conn, order_id, user)
    f = conn.execute("SELECT * FROM order_files WHERE id = ? AND order_id = ?", (file_id, o["id"])).fetchone()
    path = UPLOAD_DIR / "results" / file_id
    if not f or not (f["done"] or role == "seller") or not path.is_file():
        raise HTTPException(404, "Файл не найден")
    inline = f["mime"] in INLINE and not dl
    disp = "inline" if inline else "attachment"
    return FileResponse(
        path, media_type=f["mime"] if inline else "application/octet-stream",
        headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(f['name'])}",
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600",
                 "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"},
    )


@router.post("/api/orders/{order_id}/confirm")
def confirm(order_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Покупатель подтверждает получение: заказ «Выполнен», оплата с баланса переводится продавцу."""
    with tx(conn):
        user_state(conn, user["id"])
        o, role = load_order(conn, order_id, user)
        if role != "buyer":
            raise HTTPException(403, "Подтвердить получение может только покупатель")
        if o["status"] != ST_REVIEW:
            raise HTTPException(400, "Подтвердить можно после того, как продавец загрузит результат")
        complete_order(conn, o)
        return order_detail(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (o["id"],)).fetchone(), "buyer")
