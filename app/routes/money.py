"""Заказы и кошелёк. Все суммы считаются на сервере по ценам из БД."""

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from ..db import get_db, now_ms, tx
from ..logic import (
    BANKS, MAX_DEP, MIN_DEP, MIN_WD, ST_CANCEL, ST_WAIT, ST_WORK, TIERS,
    add_coins, add_op, fee_of, get_coins, mk_tiers, new_id, nf, rub, user_state,
)

router = APIRouter(prefix="/api")


class CartItem(BaseModel):
    id: str
    tier: int


class OrderIn(BaseModel):
    items: list[CartItem]
    method: str


class DepositIn(BaseModel):
    amount: float


class WithdrawIn(BaseModel):
    amount: float
    phone: str
    bank: str
    password: str = ""


def int_amount(x: float) -> int | None:
    return int(x) if float(x).is_integer() else None


@router.post("/orders")
def create_order(body: OrderIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if body.method not in ("sbp", "bal") or not body.items:
        raise HTTPException(400, "Корзина пуста")
    with tx(conn):
        items = []
        for it in body.items:
            g = conn.execute(
                "SELECT g.id, g.title, g.base, g.owner_id FROM gigs g LEFT JOIN users u ON u.id = g.owner_id"
                " WHERE g.id = ? AND u.blocked_at IS NULL",
                (it.id,),
            ).fetchone()
            if not g or not 0 <= it.tier < 3:
                raise HTTPException(400, "Услуга не найдена. Обновите страницу.")
            if g["owner_id"] == user["id"]:
                raise HTTPException(400, "Нельзя заказать собственную услугу")
            items.append((g, it.tier, mk_tiers(g["base"])[it.tier]["rub"]))
        total = sum(price for _, _, price in items)
        if body.method == "bal":
            if get_coins(conn, user["id"]) < total:
                raise HTTPException(400, "Недостаточно монет на балансе")
            add_coins(conn, user["id"], -total)
        count = conn.execute("SELECT count(*) FROM orders WHERE user_id = ?", (user["id"],)).fetchone()[0]
        ts = now_ms()
        for k, (g, tier, price) in enumerate(items):
            if body.method == "bal":
                add_op(conn, user["id"], "pay", "bal", price, "done", g["title"])
            num = f"#RW-{1040 + count + k}"
            conn.execute(
                "INSERT INTO orders(id, user_id, num, gig_id, title, tier, status, amount, method, created_at, started_at,"
                " seller_id, seller_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id(), user["id"], num, g["id"], g["title"], tier,
                 ST_WORK if body.method == "bal" else ST_WAIT, price, body.method, ts + k,
                 ts if body.method == "bal" else None, g["owner_id"], 0 if g["owner_id"] else 1),
            )
            # Заказ открывает диалог покупателя с продавцом: продавец видит его в «Сообщениях покупателей».
            conn.execute(
                "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
                ("u:" + user["id"], g["id"],
                 f"Оформлен заказ {num}: тариф «{TIERS[tier][0]}», {rub(price)}"
                 + (", оплачен с баланса" if body.method == "bal" else ", оплата через СБП"), ts + k),
            )
        return user_state(conn, user["id"])


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    with tx(conn):
        user_state(conn, user["id"])  # сначала применяем таймеры: выполненный заказ отменить нельзя
        o = conn.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user["id"])).fetchone()
        if not o or o["status"] not in (ST_WORK, ST_WAIT):
            raise HTTPException(400, "Этот заказ нельзя отменить")
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (ST_CANCEL, order_id))
        conn.execute(
            "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
            ("u:" + user["id"], o["gig_id"], f"Покупатель отменил заказ {o['num']}", now_ms()),
        )
        if o["method"] == "bal":
            add_coins(conn, user["id"], o["amount"])
            add_op(conn, user["id"], "ref", "bal", o["amount"], "done", o["title"])
        return {"refund": o["amount"] if o["method"] == "bal" else 0, **user_state(conn, user["id"])}


@router.post("/sales/seen")
def sales_seen(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Продавец увидел уведомления о новых покупках."""
    conn.execute("UPDATE orders SET seller_seen = 1 WHERE seller_id = ? AND seller_seen = 0", (user["id"],))
    return {"ok": True}


@router.post("/wallet/deposit")
def deposit(body: DepositIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    a = int_amount(body.amount)
    if a is None or not MIN_DEP <= a <= MAX_DEP:
        raise HTTPException(400, f"Введите целое число от {nf(MIN_DEP)} до {nf(MAX_DEP)}")
    with tx(conn):
        add_op(conn, user["id"], "dep", "sbp", a, "pending")
        return user_state(conn, user["id"])


@router.post("/wallet/withdraw")
def withdraw(body: WithdrawIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    a = int_amount(body.amount)
    if a is None or a < MIN_WD:
        raise HTTPException(400, f"Минимальная сумма вывода — {nf(MIN_WD)} монет")
    phone = re.sub(r"[\s()\-]", "", body.phone)
    if not re.fullmatch(r"\+7\d{10}", phone):
        raise HTTPException(400, "Введите телефон в формате +7 900 000 00 00")
    if body.bank not in BANKS:
        raise HTTPException(400, "Выберите банк")
    if not body.password:
        raise HTTPException(400, "Введите пароль")
    if not auth.check_pw(user, body.password):
        raise HTTPException(400, "Неверный пароль")
    with tx(conn):
        if a > get_coins(conn, user["id"]):
            raise HTTPException(400, "Недостаточно монет на балансе")
        add_coins(conn, user["id"], -a)
        add_op(conn, user["id"], "wd", "sbp", a, "pending", f"+7 ••• •• {phone[-4:]} · {body.bank}", fee_of(a))
        return user_state(conn, user["id"])


@router.post("/wallet/ops/{op_id}/{act}")
def op_action(op_id: str, act: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Пользователь может только отменить свою заявку; одобряет её администратор (app/routes/admin.py)."""
    if act != "cancel":
        raise HTTPException(400, "Заявку подтверждает администратор")
    with tx(conn):
        o = conn.execute("SELECT * FROM wallet_ops WHERE id = ? AND user_id = ?", (op_id, user["id"])).fetchone()
        if not o or o["status"] != "pending":
            raise HTTPException(400, "Операция уже обработана")
        if o["t"] == "wd":
            add_coins(conn, user["id"], o["coins"])
        conn.execute("UPDATE wallet_ops SET status = 'cancelled' WHERE id = ?", (op_id,))
        return user_state(conn, user["id"])
