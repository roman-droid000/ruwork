"""Заказы и кошелёк. Все суммы считаются на сервере по ценам из БД."""

import re
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, guard
from ..db import get_db, now_ms, tx
from ..logic import (
    BANKS, DEP_AMOUNTS, DEP_AMOUNTS_TEXT, MIN_WD, PLATFORM_CANCEL_PCT, REFUND_WORK_PCT, ST_CANCEL, ST_WAIT, ST_WORK, TIERS,
    add_bonus, add_coins, add_op, fee_of, get_bonus, get_coins, get_wallet, linked, mk_tiers, new_id, nf, notify, platform_add,
    platform_bonus, refund_order, rub, spend,
    user_state, VISIBLE_SQL,
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


class RefundIn(BaseModel):
    amount: float


class WithdrawIn(BaseModel):
    amount: float
    phone: str
    bank: str
    password: str = ""


def int_amount(x: float) -> int | None:
    return int(x) if float(x).is_integer() else None


def usable_bonus(conn: sqlite3.Connection, user_id: str, seller_ids) -> int:
    """Сколько бонуса можно потратить на заказ у этих продавцов: 0, если хоть один из них — связанный аккаунт."""
    b = get_bonus(conn, user_id)
    return 0 if not b or any(linked(conn, user_id, s) for s in seller_ids) else b


def place_order(conn: sqlite3.Connection, user_id: str, g: sqlite3.Row, tier: int, tier_name: str, price: int,
                days: int, revs: int, method: str, k: int = 0) -> str:
    """Создаёт один заказ: списывает оплату (сначала приветственный бонус, потом монеты), пишет сообщение в диалог
    и уведомляет продавца. Номер — по числу заказов покупателя, k — сдвиг для нескольких заказов из одной корзины."""
    paid_adv = paid_bonus = 0
    if method == "bal":
        paid_bonus = min(price, usable_bonus(conn, user_id, [g["owner_id"]]))
        if paid_bonus:
            add_bonus(conn, user_id, -paid_bonus)
        paid_adv = spend(conn, user_id, price - paid_bonus)
        add_op(conn, user_id, "pay", "bal", price, "done", g["title"] + (f" · бонусом {paid_bonus}" if paid_bonus else ""))
    count = conn.execute("SELECT count(*) FROM orders WHERE user_id = ?", (user_id,)).fetchone()[0]
    num, oid, ts = f"#RW-{1040 + count}", new_id(), now_ms() + k
    conn.execute(
        "INSERT INTO orders(id, user_id, num, gig_id, title, tier, tier_name, status, amount, method, created_at,"
        " started_at, seller_id, seller_seen, paid_adv, days, revs, paid_bonus)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (oid, user_id, num, g["id"], g["title"], tier, tier_name,
         ST_WAIT, price, method, ts, None, g["owner_id"], 0 if g["owner_id"] else 1, paid_adv, days, revs, paid_bonus),
    )
    if paid_bonus:
        platform_bonus(conn, -paid_bonus, user_id, oid, f"{num} · {g['title']}")
    # Заказ открывает диалог покупателя с продавцом: продавец видит его в «Сообщениях покупателей».
    conn.execute(
        "INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
        ("u:" + user_id, g["id"],
         f"Оформлен заказ {num}: «{tier_name}», {rub(price)}, срок {days} дн."
         + (", оплачен с баланса" if method == "bal" else ", оплата через СБП"), ts),
    )
    notify(conn, g["owner_id"], "sale", f"Новый заказ {num}: «{g['title']}», «{tier_name}», {rub(price)}", f"#/order/{oid}")
    return oid


def seller_away(conn: sqlite3.Connection, owner_id) -> int | None:
    """До какого времени продавец в отпуске (None — принимает заказы)."""
    if not owner_id:
        return None
    r = conn.execute("SELECT vacation_until FROM users WHERE id = ?", (owner_id,)).fetchone()
    return r["vacation_until"] if r and r["vacation_until"] and r["vacation_until"] > now_ms() else None


@router.post("/orders")
def create_order(body: OrderIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    if not body.items:
        raise HTTPException(400, "Корзина пуста")
    # Заказы оплачиваются только монетами с баланса; через СБП баланс пополняется (/api/wallet/deposit).
    if body.method != "bal":
        raise HTTPException(400, "Заказ оплачивается монетами с баланса. Пополните баланс через СБП в кошельке.")
    with tx(conn):
        items = []
        for it in body.items:
            g = conn.execute(
                "SELECT g.id, g.title, g.base, g.owner_id FROM gigs g LEFT JOIN users u ON u.id = g.owner_id"
                f" WHERE g.id = ? AND {VISIBLE_SQL}",
                (it.id,),
            ).fetchone()
            if not g or not 0 <= it.tier < len(TIERS):
                raise HTTPException(400, "Услуга не найдена. Обновите страницу.")
            if g["owner_id"] == user["id"]:
                raise HTTPException(400, "Нельзя заказать собственную услугу")
            if seller_away(conn, g["owner_id"]):
                raise HTTPException(400, f"Продавец услуги «{g['title']}» в отпуске и сейчас не принимает заказы")
            items.append((g, it.tier, mk_tiers(g["base"])[it.tier]["rub"]))
        total = sum(price for _, _, price in items)
        if body.method == "bal":
            if get_coins(conn, user["id"]) + usable_bonus(conn, user["id"], [g["owner_id"] for g, _, _ in items]) < total:
                raise HTTPException(400, "Недостаточно монет на балансе")
        placed = [place_order(conn, user["id"], g, tier, TIERS[tier][0], price, TIERS[tier][2], TIERS[tier][3], body.method, k)
                  for k, (g, tier, price) in enumerate(items)]
        return {"placed": placed, **user_state(conn, user["id"])}


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
        notify(conn, o["seller_id"], "cancel", f"Покупатель отменил заказ {o['num']} «{o['title']}».", f"#/order/{o['id']}")
        # Монеты возвращаются только за оплату с баланса: до подтверждения 100 %, после перехода «В работе» 90 %.
        # Оплата через СБП не проверяется (демо), поэтому её отмена монеты не начисляет.
        # Удержанные 10 % делятся: 5 % суммы — платформе, остаток — продавцу за начатую работу
        # (у услуг каталога RUWORK продавца нет, там всё удержание уходит платформе).
        refund = 0
        if o["method"] == "bal":
            refund = o["amount"] if o["status"] == ST_WAIT else o["amount"] * REFUND_WORK_PCT // 100
            refund_order(conn, o, refund)
            add_op(conn, user["id"], "ref", "bal", refund, "done", o["title"])
            kept = o["amount"] - refund
            fee = kept if not o["seller_id"] else min(kept, o["amount"] * PLATFORM_CANCEL_PCT // 100)
            platform_add(conn, "cancel", fee, user["id"], o["id"], f"{o['num']} · {o['title']}")
            if o["seller_id"] and kept > fee:
                add_coins(conn, o["seller_id"], kept - fee, kept - fee)
                add_op(conn, o["seller_id"], "comp", "bal", kept - fee, "done", f"{o['num']} · {o['title']}")
        return {"refund": refund, **user_state(conn, user["id"])}


@router.post("/sales/seen")
def sales_seen(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Продавец увидел уведомления о новых покупках."""
    conn.execute("UPDATE orders SET seller_seen = 1 WHERE seller_id = ? AND seller_seen = 0", (user["id"],))
    return {"ok": True}


@router.post("/wallet/deposit")
def deposit(body: DepositIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    a = int_amount(body.amount)
    if a not in DEP_AMOUNTS:
        raise HTTPException(400, f"Выберите сумму пополнения: {DEP_AMOUNTS_TEXT} монет")
    with tx(conn):
        add_op(conn, user["id"], "dep", "sbp", a, "pending")
        guard.alert(f"Заявка на пополнение: {user['name']} — {nf(a)} мон.", "#/admin/ops")
        return user_state(conn, user["id"])


@router.post("/wallet/refund")
def refund_to_card(body: RefundIn, user=Depends(auth.require_user)):
    """Возврат пополнений на карту не производится: пополненные монеты остаются на балансе и тратятся на заказы
    и продвижение, возвраты по заказам тоже приходят на баланс. Заявки «Возврат на карту», созданные раньше,
    администратор по-прежнему одобряет или отклоняет во вкладке «Деньги»."""
    raise HTTPException(400, "Возврат на карту не производится: пополненные монеты остаются на балансе и тратятся "
                             "на заказы и продвижение.")


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
        earned = get_wallet(conn, user["id"])[1]
        if a > earned:
            raise HTTPException(400, f"Вывести можно только заработок с продаж: доступно {nf(earned)} монет."
                                     " Пополненные монеты на карту не возвращаются — их можно потратить на заказы.")
        add_coins(conn, user["id"], -a, -a)
        op_id = add_op(conn, user["id"], "wd", "sbp", a, "pending", f"+7 ••• •• {phone[-4:]} · {body.bank}", fee_of(a))
        conn.execute("UPDATE wallet_ops SET phone = ? WHERE id = ?", (phone, op_id))
        guard.alert(f"Заявка на вывод: {user['name']} — {nf(a)} мон. на {body.bank}", "#/admin/ops")
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
        if o["t"] in ("wd", "rf"):
            add_coins(conn, user["id"], o["coins"], o["coins"] if o["t"] == "wd" else 0)
        conn.execute("UPDATE wallet_ops SET status = 'cancelled' WHERE id = ?", (op_id,))
        return user_state(conn, user["id"])
