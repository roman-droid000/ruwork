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

from .. import auth, guard
from ..db import UPLOAD_DIR, get_db, now_ms, tx
from ..logic import (
    AUTO_CONFIRM_MS, EXT_MAX_DAYS, ST_DISPUTE, ST_REVIEW, ST_WORK, complete_order, deadline, gig_is_top, notify, user_state,
)

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


def cover_ext(data: bytes, what: str = "Обложка") -> str:
    for magic, ext in COVER_TYPES.items():
        if data.startswith(magic) and (ext != ".webp" or data[8:12] == b"WEBP"):
            return ext
    raise HTTPException(400, f"{what} должна быть картинкой JPG, PNG или WebP")


def remove_cover(name: str | None) -> None:
    if name and NAME_RE.match(name):
        (UPLOAD_DIR / "covers" / name).unlink(missing_ok=True)


@router.post("/api/gigs/{gig_id}/cover")
async def upload_cover(gig_id: str, request: Request, user=Depends(auth.require_user),
                       conn: sqlite3.Connection = Depends(get_db)):
    g = conn.execute("SELECT cover FROM gigs WHERE id = ? AND owner_id = ?", (gig_id, user["id"])).fetchone()
    if not g:
        raise HTTPException(404, "Объявление не найдено")
    # У «ТОП» обложку можно только поставить, если её ещё нет (новое объявление продавца со 150+ заказами).
    if g["cover"] and gig_is_top(conn, gig_id):
        raise HTTPException(400, "У объявления в «ТОП» можно менять только цену")
    data = await request.body()
    if not data or len(data) > COVER_MAX:
        raise HTTPException(400, "Обложка должна быть не больше 3 МБ")
    name = f"{gig_id}-{secrets.token_hex(4)}{cover_ext(data)}"
    (UPLOAD_DIR / "covers" / name).write_bytes(data)
    conn.execute("UPDATE gigs SET cover = ? WHERE id = ?", (name, gig_id))
    remove_cover(g["cover"])
    return {"cover": name}


AVATAR_MAX = 1024 * 1024


def remove_avatar(name: str | None) -> None:
    if name and NAME_RE.match(name):
        (UPLOAD_DIR / "avatars" / name).unlink(missing_ok=True)


def set_avatar(conn: sqlite3.Connection, user_id: str, name: str | None) -> None:
    conn.execute("UPDATE users SET avatar = ? WHERE id = ?", (name, user_id))
    conn.execute("UPDATE gigs SET seller_avatar = ? WHERE owner_id = ?", (name, user_id))


@router.post("/api/me/avatar")
async def upload_avatar(request: Request, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Фото профиля: браузер заранее обрезает его до квадрата 400×400 и сжимает в JPEG."""
    data = await request.body()
    if not data or len(data) > AVATAR_MAX:
        raise HTTPException(400, "Фото должно быть не больше 1 МБ")
    name = f"{user['id']}-{secrets.token_hex(4)}{cover_ext(data, 'Фотография')}"
    (UPLOAD_DIR / "avatars" / name).write_bytes(data)
    old = conn.execute("SELECT avatar FROM users WHERE id = ?", (user["id"],)).fetchone()["avatar"]
    set_avatar(conn, user["id"], name)
    remove_avatar(old)
    return {"avatar": name}


@router.delete("/api/me/avatar")
def delete_avatar(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    old = conn.execute("SELECT avatar FROM users WHERE id = ?", (user["id"],)).fetchone()["avatar"]
    set_avatar(conn, user["id"], None)
    remove_avatar(old)
    return {"ok": True}


@router.get("/media/avatars/{name}", include_in_schema=False)
def avatar_file(name: str):
    path = UPLOAD_DIR / "avatars" / name
    if not NAME_RE.match(name) or not path.is_file():
        raise HTTPException(404, "Не найдено")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable",
                                       "X-Content-Type-Options": "nosniff"})


@router.get("/media/covers/{name}", include_in_schema=False)
def cover_file(name: str):
    path = UPLOAD_DIR / "covers" / name
    if not NAME_RE.match(name) or not path.is_file():
        raise HTTPException(404, "Не найдено")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable",
                                       "X-Content-Type-Options": "nosniff"})


def load_order(conn: sqlite3.Connection, order_id: str, user) -> tuple[sqlite3.Row, str]:
    """Заказ и роль смотрящего: покупатель, продавец или сотрудник (только просмотр)."""
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if o and o["user_id"] == user["id"]:
        return o, "buyer"
    if o and o["seller_id"] and o["seller_id"] == user["id"]:
        return o, "seller"
    if o and auth.staff_role(user):  # администратор и модератор — для разбора споров
        return o, "admin"
    raise HTTPException(404, "Заказ не найден")


def order_detail(conn: sqlite3.Connection, o: sqlite3.Row, role: str) -> dict:
    from .reviews import order_review  # reviews импортирует этот модуль

    g = conn.execute("SELECT cat, sub, cover FROM gigs WHERE id = ?", (o["gig_id"],)).fetchone()
    buyer = conn.execute("SELECT name FROM users WHERE id = ?", (o["user_id"],)).fetchone()
    seller = conn.execute("SELECT name FROM users WHERE id = ?", (o["seller_id"],)).fetchone() if o["seller_id"] else None
    files = conn.execute(
        "SELECT id, name, mime, size, received, done, ts FROM order_files WHERE order_id = ? ORDER BY ts", (o["id"],)
    ).fetchall()
    return {
        "id": o["id"], "num": o["num"], "gig": o["gig_id"], "title": o["title"], "tier": o["tier_name"],
        "status": o["status"], "amount": o["amount"], "method": o["method"], "ts": o["created_at"],
        "confirmed_at": o["confirmed_at"], "role": role, "seller": o["seller_id"],
        "dispute_at": o["dispute_at"], "dispute_reason": o["dispute_reason"], "dispute_result": o["dispute_result"],
        "auto_at": o["reviewed_at"] + AUTO_CONFIRM_MS if o["status"] == ST_REVIEW and o["reviewed_at"] else None, "buyer": o["user_id"],
        "buyer_name": buyer["name"] if buyer else "—", "seller_name": seller["name"] if seller else None,
        "has_seller": bool(o["seller_id"]), "cat": g["cat"] if g else None, "sub": g["sub"] if g else None,
        "cover": g["cover"] if g else None, "gig_exists": bool(g),
        # Покупатель видит только полностью загруженные файлы; продавец — и недогруженные, чтобы удалить их.
        "files": [dict(f) for f in files if f["done"] or role == "seller"],
        "review": order_review(conn, o["id"]),
        "started_at": o["started_at"], "delivered_at": o["delivered_at"], "reviewed_at": o["reviewed_at"],
        "days": o["days"], "extra_days": o["extra_days"], "deadline": deadline(o),
        "revs": o["revs"], "revs_used": o["revs_used"], "ext_days": o["ext_days"], "ext_reason": o["ext_reason"],
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
    if o["status"] not in (ST_WORK, ST_REVIEW, ST_DISPUTE):
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
            conn.execute("UPDATE orders SET status = ?, reviewed_at = ?, delivered_at = coalesce(delivered_at, ?)"
                         " WHERE id = ? AND status = ?", (ST_REVIEW, now_ms(), now_ms(), o["id"], ST_WORK))
            conn.execute(
                "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
                ("u:" + o["user_id"], o["gig_id"],
                 f"Продавец загрузил результат по заказу {o['num']}: {f['name']}. Проверьте и подтвердите получение.",
                 now_ms()),
            )
            notify(conn, o["user_id"], "result", f"Продавец загрузил результат по заказу {o['num']} «{o['title']}»: {f['name']}",
                   f"#/order/{o['id']}")
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


class DisputeIn(BaseModel):
    reason: str


@router.post("/api/orders/{order_id}/dispute")
def open_dispute(order_id: str, body: DisputeIn, user=Depends(auth.require_user),
                 conn: sqlite3.Connection = Depends(get_db)):
    """Покупатель открывает спор: автоподтверждение останавливается, деньги остаются на платформе до решения."""
    reason = body.reason.strip()[:1000]
    if len(reason) < 10:
        raise HTTPException(400, "Опишите проблему подробнее — хотя бы пару предложений")
    with tx(conn):
        user_state(conn, user["id"])
        o, role = load_order(conn, order_id, user)
        if role != "buyer":
            raise HTTPException(403, "Открыть спор может только покупатель")
        if not o["seller_id"] or o["status"] not in (ST_WORK, ST_REVIEW):
            raise HTTPException(400, "Спор можно открыть, пока заказ в работе или на проверке")
        conn.execute("UPDATE orders SET status = ?, dispute_at = ?, dispute_reason = ?, dispute_result = '' WHERE id = ?",
                     (ST_DISPUTE, now_ms(), reason, o["id"]))
        conn.execute("INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
                     ("u:" + o["user_id"], o["gig_id"],
                      f"Покупатель открыл спор по заказу {o['num']}. Администратор рассмотрит его и примет решение.",
                      now_ms()))
        notify(conn, o["seller_id"], "dispute", f"Покупатель открыл спор по заказу {o['num']} «{o['title']}»: {reason[:120]}",
               f"#/order/{o['id']}")
        guard.alert(f"Новый спор по заказу {o['num']} «{o['title']}» на {o['amount']} мон.: {reason[:200]}",
                    "#/admin/disputes")
        return order_detail(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (o["id"],)).fetchone(), "buyer")


@router.post("/api/orders/{order_id}/dispute/close")
def close_dispute(order_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Покупатель отзывает спор: заказ возвращается «На проверке» (7 дней заново) или «В работе», если файлов нет."""
    with tx(conn):
        o, role = load_order(conn, order_id, user)
        if role != "buyer" or o["status"] != ST_DISPUTE:
            raise HTTPException(400, "Открытого спора нет")
        has = conn.execute("SELECT 1 FROM order_files WHERE order_id = ? AND done = 1", (o["id"],)).fetchone()
        conn.execute("UPDATE orders SET status = ?, reviewed_at = ?, reminded = 0, dispute_result = 'closed' WHERE id = ?",
                     (ST_REVIEW if has else ST_WORK, now_ms() if has else None, o["id"]))
        conn.execute("INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
                     ("u:" + o["user_id"], o["gig_id"], f"Покупатель закрыл спор по заказу {o['num']}.", now_ms()))
        notify(conn, o["seller_id"], "dispute", f"Покупатель закрыл спор по заказу {o['num']} «{o['title']}».",
               f"#/order/{o['id']}")
        return order_detail(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (o["id"],)).fetchone(), "buyer")


def order_msg(conn: sqlite3.Connection, o: sqlite3.Row, text: str) -> None:
    conn.execute("INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
                 ("u:" + o["user_id"], o["gig_id"], text, now_ms()))


def fresh(conn: sqlite3.Connection, o: sqlite3.Row, role: str) -> dict:
    return order_detail(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (o["id"],)).fetchone(), role)


class RevisionIn(BaseModel):
    text: str


@router.post("/api/orders/{order_id}/revision")
def send_revision(order_id: str, body: RevisionIn, user=Depends(auth.require_user),
                  conn: sqlite3.Connection = Depends(get_db)):
    """Покупатель отправляет результат на доработку: тратится одна правка из тарифа, заказ снова «В работе»,
    автоподтверждение останавливается до новой загрузки результата."""
    text = body.text.strip()[:1000]
    if len(text) < 5:
        raise HTTPException(400, "Напишите, что нужно исправить")
    with tx(conn):
        user_state(conn, user["id"])
        o, role = load_order(conn, order_id, user)
        if role != "buyer" or not o["seller_id"]:
            raise HTTPException(403, "Отправить на доработку может только покупатель")
        if o["status"] != ST_REVIEW:
            raise HTTPException(400, "На доработку можно отправить результат, который ждёт проверки")
        if o["revs_used"] >= o["revs"]:
            raise HTTPException(400, "Все правки по тарифу использованы. Договоритесь с продавцом в чате или откройте спор.")
        conn.execute("UPDATE orders SET status = ?, reviewed_at = NULL, reminded = 0, revs_used = revs_used + 1 WHERE id = ?",
                     (ST_WORK, o["id"]))
        n = o["revs_used"] + 1
        order_msg(conn, o, f"Покупатель отправил заказ {o['num']} на доработку (правка {n} из {o['revs']}): {text}")
        notify(conn, o["seller_id"], "result", f"Заказ {o['num']} «{o['title']}» отправлен на доработку: {text[:120]}",
               f"#/order/{o['id']}")
        return fresh(conn, o, "buyer")


class ExtendIn(BaseModel):
    days: int
    reason: str


@router.post("/api/orders/{order_id}/extend")
def ask_extend(order_id: str, body: ExtendIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Продавец просит продлить срок. Срок меняется, только если покупатель согласится."""
    reason = body.reason.strip()[:500]
    if not 1 <= body.days <= EXT_MAX_DAYS:
        raise HTTPException(400, f"Продлить можно на 1–{EXT_MAX_DAYS} дней")
    if len(reason) < 5:
        raise HTTPException(400, "Объясните покупателю, зачем нужно больше времени")
    with tx(conn):
        user_state(conn, user["id"])
        o, role = load_order(conn, order_id, user)
        if role != "seller":
            raise HTTPException(403, "Попросить продление может только продавец")
        if o["status"] != ST_WORK:
            raise HTTPException(400, "Продлить срок можно, пока заказ в работе")
        if o["ext_days"]:
            raise HTTPException(400, "Запрос уже отправлен — дождитесь ответа покупателя")
        conn.execute("UPDATE orders SET ext_days = ?, ext_reason = ? WHERE id = ?", (body.days, reason, o["id"]))
        order_msg(conn, o, f"Продавец просит продлить срок заказа {o['num']} на {body.days} дн.: {reason}")
        notify(conn, o["user_id"], "remind", f"Продавец просит продлить срок заказа {o['num']} на {body.days} дн. Ответьте на странице заказа.",
               f"#/order/{o['id']}")
        return fresh(conn, o, "seller")


@router.post("/api/orders/{order_id}/extend/{act}")
def answer_extend(order_id: str, act: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if act not in ("accept", "decline"):
        raise HTTPException(400, "Неизвестное действие")
    with tx(conn):
        o, role = load_order(conn, order_id, user)
        if role != "buyer":
            raise HTTPException(403, "Ответить на запрос может только покупатель")
        if not o["ext_days"]:
            raise HTTPException(400, "Запроса на продление нет")
        days = o["ext_days"]
        # Срок сдвинулся — напоминания о нём начинаются заново.
        conn.execute("UPDATE orders SET extra_days = extra_days + ?, ext_days = 0, ext_reason = '',"
                     " dl_reminded = CASE WHEN ? THEN 0 ELSE dl_reminded END WHERE id = ?",
                     (days if act == "accept" else 0, act == "accept", o["id"]))
        text = (f"Покупатель согласился продлить срок заказа {o['num']} на {days} дн." if act == "accept"
                else f"Покупатель отказался продлевать срок заказа {o['num']}.")
        order_msg(conn, o, text)
        notify(conn, o["seller_id"], "remind", text, f"#/order/{o['id']}")
        return fresh(conn, o, "buyer")
