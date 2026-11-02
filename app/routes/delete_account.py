"""Удаление аккаунта самим пользователем (внизу вкладки «Профиль»).

Повторное подтверждение: пароль и, если привязан Telegram, одноразовый код из бота.
Строка users не удаляется: на неё ссылаются заказы, отзывы и операции других людей (ON DELETE CASCADE
стёр бы их историю). Вместо этого аккаунт обезличивается: имя «Удалённый аккаунт …», почта, телефон,
Telegram, «О себе» и фото стираются, пароль заменяется случайным, вход закрыт (blocked_at, deleted_at).
Объявления с обложками, портфолио, избранное, подписки, сессии и адреса входа удаляются, открытые задания
закрываются, отклики отзываются. Заказы, отзывы и денежные операции остаются у второй стороны.
"""

import hashlib
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth, guard, tg
from ..db import UPLOAD_DIR, get_db, now_ms, tx
from ..logic import (
    ST_DISPUTE, ST_REVIEW, ST_WAIT, ST_WORK, add_op, get_wallet, nf, platform_add, user_state,
)
from .market import NAME_RE as PF_NAME_RE
from .orders import remove_avatar, remove_cover

router = APIRouter(prefix="/api/me/delete")

CODE_TTL_MS = 10 * 60 * 1000
CODE_TRIES = 5
SEND_RATE = (3, 600)
PASSWORD_RATE = (5, 900)


def code_hash(code: str) -> str:
    return hashlib.sha256(("ruwork-del:" + code).encode()).hexdigest()


class DeleteIn(BaseModel):
    password: str
    code: str = ""
    forfeit: bool = False  # согласие, что остаток на балансе сгорит


def blockers(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """Что мешает удалить аккаунт прямо сейчас."""
    user_state(conn, user_id)  # таймеры заказов — статусы должны быть свежими
    out = []
    active = (ST_WAIT, ST_WORK, ST_REVIEW, ST_DISPUTE)
    buys = conn.execute(f"SELECT count(*) FROM orders WHERE user_id = ? AND status IN ({','.join('?' * 4)})",
                        (user_id, *active)).fetchone()[0]
    sells = conn.execute(f"SELECT count(*) FROM orders WHERE seller_id = ? AND status IN ({','.join('?' * 4)})",
                         (user_id, *active)).fetchone()[0]
    if buys:
        out.append(f"незавершённые покупки: {buys} — дождитесь результата и подтвердите получение или отмените заказ")
    if sells:
        out.append(f"незавершённые продажи: {sells} — сдайте результат и дождитесь подтверждения")
    ops = conn.execute("SELECT count(*) FROM wallet_ops WHERE user_id = ? AND status = 'pending'", (user_id,)).fetchone()[0]
    if ops:
        out.append(f"заявки в кошельке в обработке: {ops} — дождитесь решения или отмените их")
    return out


@router.get("")
def delete_state(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Для окна удаления: что мешает, сколько монет сгорит, нужен ли код из Telegram."""
    if auth.is_admin(user):
        return {"blockers": ["аккаунт владельца сайта удалить нельзя"], "coins": 0, "tg": bool(user["tg_chat"])}
    return {"blockers": blockers(conn, user["id"]), "coins": get_wallet(conn, user["id"])[0], "tg": bool(user["tg_chat"])}


@router.post("/code")
def send_code(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if not user["tg_chat"]:
        raise HTTPException(400, "Telegram не привязан — достаточно пароля")
    guard.throttle("delcode:" + user["id"], SEND_RATE, "Код уже отправлен — подождите несколько минут")
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute("UPDATE users SET del_code = ?, del_code_ts = ?, del_tries = 0 WHERE id = ?",
                 (code_hash(code), now_ms(), user["id"]))
    tg.send(user["tg_chat"], f"Код для УДАЛЕНИЯ аккаунта RUWORK: {code}\nДействует 10 минут. Если вы не удаляете "
                             "аккаунт — никому не сообщайте код и смените пароль.")
    return {"ok": True}


@router.post("")
def delete_account(body: DeleteIn, request: Request, response: Response, user=Depends(auth.require_user),
                   conn: sqlite3.Connection = Depends(get_db)):
    if auth.is_admin(user):
        raise HTTPException(400, "Аккаунт владельца сайта удалить нельзя")
    key = "delpw:" + user["id"]
    if guard.limited(key, PASSWORD_RATE):
        raise HTTPException(429, "Слишком много попыток — попробуйте через 15 минут")
    if not body.password or not auth.check_pw(user, body.password):
        guard.hit(key)
        raise HTTPException(400, "Неверный пароль")
    if user["tg_chat"]:
        if not user["del_code"] or user["del_code_ts"] < now_ms() - CODE_TTL_MS or user["del_tries"] >= CODE_TRIES:
            raise HTTPException(400, "Нажмите «Отправить код в Telegram» и введите новый код")
        if not secrets.compare_digest(code_hash(body.code.strip()), user["del_code"]):
            conn.execute("UPDATE users SET del_tries = del_tries + 1 WHERE id = ?", (user["id"],))
            raise HTTPException(400, f"Неверный код. Осталось попыток: {CODE_TRIES - user['del_tries'] - 1}")
    uid, chat = user["id"], user["tg_chat"]
    with tx(conn):
        problems = blockers(conn, uid)
        if problems:
            raise HTTPException(400, "Сейчас удалить аккаунт нельзя: " + "; ".join(problems) + ".")
        coins, _ = get_wallet(conn, uid)
        if coins > 0 and not body.forfeit:
            raise HTTPException(400, f"На балансе {nf(coins)} мон. Выведите заработок или верните аванс на карту — "
                                     "или подтвердите, что остаток сгорит.")
        if coins > 0:
            add_op(conn, uid, "adj_out", "adm", coins, "done", "Остаток при удалении аккаунта")
            platform_add(conn, "forfeit", coins, uid, None, "Остаток при удалении аккаунта")
            conn.execute("UPDATE wallets SET coins = 0, earned = 0 WHERE user_id = ?", (uid,))
        covers = [r[0] for r in conn.execute("SELECT cover FROM gigs WHERE owner_id = ?", (uid,))]
        works = [r[0] for r in conn.execute("SELECT name FROM portfolio WHERE user_id = ?", (uid,))]
        conn.execute("DELETE FROM chat_messages WHERE gig_id IN (SELECT id FROM gigs WHERE owner_id = ?)", (uid,))
        for table, col in (("gigs", "owner_id"), ("portfolio", "user_id"), ("favorites", "user_id"),
                           ("saved_searches", "user_id"), ("sessions", "user_id"), ("user_ips", "user_id"),
                           ("password_resets", "user_id"), ("notifications", "user_id")):
            conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (uid,))
        conn.execute("UPDATE requests SET status = 'closed' WHERE user_id = ? AND status = 'open'", (uid,))
        conn.execute("UPDATE offers SET status = 'withdrawn' WHERE seller_id = ? AND status = 'open'", (uid,))
        salt, pw_hash = auth.new_password(secrets.token_urlsafe(32))
        n = now_ms()
        conn.execute(
            "UPDATE users SET name = ?, email = ?, phone = NULL, tg_chat = NULL, tg_code = NULL, salt = ?, hash = ?,"
            " about = '', avatar = NULL, verified = 0, role = '', vacation_until = NULL, vacation_note = '',"
            " blocked_at = ?, block_reason = 'Аккаунт удалён владельцем', blocked_until = NULL, deleted_at = ?,"
            " del_code = NULL WHERE id = ?",
            (f"Удалённый аккаунт {uid[-6:]}", f"deleted-{uid}@deleted.invalid", salt, pw_hash, n, n, uid),
        )
    remove_avatar(user["avatar"])
    for c in covers:
        remove_cover(c)
    for w in works:
        if PF_NAME_RE.match(w):
            (UPLOAD_DIR / "portfolio" / w).unlink(missing_ok=True)
    auth.end_session(conn, request, response)
    tg.send(chat, "Аккаунт RUWORK удалён. Уведомления сюда больше не придут. Спасибо, что были с нами!")
    guard.alert(f"Пользователь удалил аккаунт: {user['name']}" + (f" (сгорело {nf(coins)} мон.)" if coins else ""))
    return {"ok": True}
