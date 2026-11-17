"""Заказы и кошелёк. Все суммы считаются на сервере по ценам из БД."""

import re
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, guard, pay
from ..db import get_db, now_ms, tx
from ..logic import (
    BANKS, DEP_METHODS, MAX_DEP, MIN_DEP, MIN_WD, ST_CANCEL, ST_WAIT, ST_WORK, TIERS, deadline, fee_pct,
    add_bonus, add_coins, add_op, fee_of, get_bonus, get_coins, get_wallet, linked, mk_tiers, new_id, nf, notify,
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
    method: str = "sbp"


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
    # Ставка комиссии фиксируется при оплате: изменения условий не касаются уже оплаченных заказов.
    pct = fee_pct(conn, g["owner_id"]) if g["owner_id"] else None
    conn.execute(
        "INSERT INTO orders(id, user_id, num, gig_id, title, tier, tier_name, status, amount, method, created_at,"
        " started_at, seller_id, seller_seen, paid_adv, days, revs, paid_bonus, fee_pct)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (oid, user_id, num, g["id"], g["title"], tier, tier_name,
         ST_WAIT, price, method, ts, None, g["owner_id"], 0 if g["owner_id"] else 1, paid_adv, days, revs, paid_bonus, pct),
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


class CancelIn(BaseModel):
    reason: str = ""


def order_note(conn: sqlite3.Connection, o, user_id: str, text: str) -> None:
    conn.execute("INSERT INTO chat_messages(owner, gig_id, role, text, ts) VALUES (?, ?, 'order', ?, ?)",
                 ("u:" + user_id, o["gig_id"], text, now_ms()))


def cancel_full(conn: sqlite3.Connection, o) -> int:
    """Отменяет заказ с возвратом всей оплаты покупателю на баланс. Условие на статус не даёт вернуть деньги дважды."""
    if not conn.execute("UPDATE orders SET status = ?, cancel_req_at = NULL WHERE id = ? AND status IN (?, ?)",
                        (ST_CANCEL, o["id"], ST_WAIT, ST_WORK)).rowcount:
        raise HTTPException(400, "Этот заказ нельзя отменить")
    refund = 0
    if o["method"] == "bal":  # оплата через СБП в демо не проверялась, её отмена монеты не начисляет
        refund = o["amount"]
        refund_order(conn, o, refund)
        add_op(conn, o["user_id"], "ref", "bal", refund, "done", o["title"])
    return refund


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, body: CancelIn | None = None, user=Depends(auth.require_user),
                 conn: sqlite3.Connection = Depends(get_db)):
    """Отмена покупателем. Первую минуту («Ожидает подтверждения») и после просрочки срока сдачи — сразу со 100 %.
    Заказ «В работе» в срок — только с согласия продавца: создаётся запрос, продавец его принимает или отклоняет;
    если продавец отказал или не ответил за CANCEL_ANSWER_MS, покупатель открывает спор."""
    reason = re.sub(r"\s+", " ", (body.reason if body else "") or "").strip()[:500]
    with tx(conn):
        user_state(conn, user["id"])  # сначала применяем таймеры: выполненный заказ отменить нельзя
        o = conn.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user["id"])).fetchone()
        if not o or o["status"] not in (ST_WORK, ST_WAIT):
            raise HTTPException(400, "Этот заказ нельзя отменить")
        dl = deadline(o) if o["status"] == ST_WORK else None
        if o["status"] == ST_WORK and not (dl and dl < now_ms()):
            if o["cancel_req_at"]:
                raise HTTPException(400, "Запрос на отмену уже отправлен продавцу")
            conn.execute("UPDATE orders SET cancel_req_at = ?, cancel_reason = ? WHERE id = ?", (now_ms(), reason, o["id"]))
            order_note(conn, o, user["id"], f"Покупатель просит отменить заказ {o['num']}" + (f": {reason}" if reason else ""))
            notify(conn, o["seller_id"], "cancel",
                   f"Покупатель просит отменить заказ {o['num']} «{o['title']}». Примите или отклоните запрос.",
                   f"#/order/{o['id']}")
            return {"requested": True, **user_state(conn, user["id"])}
        refund = cancel_full(conn, o)
        late = o["status"] == ST_WORK
        order_note(conn, o, user["id"], f"Покупатель отменил заказ {o['num']}" + (" — срок сдачи прошёл" if late else ""))
        notify(conn, o["seller_id"], "cancel",
               f"Покупатель отменил заказ {o['num']} «{o['title']}»" + (": срок сдачи прошёл." if late else "."),
               f"#/order/{o['id']}")
        return {"refund": refund, **user_state(conn, user["id"])}


@router.post("/orders/{order_id}/cancel/{act}")
def answer_cancel(order_id: str, act: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Продавец принимает запрос отмены (покупателю возвращается 100 %) или отклоняет его (покупатель может открыть спор)."""
    if act not in ("accept", "decline"):
        raise HTTPException(404, "Нет такого действия")
    with tx(conn):
        o = conn.execute("SELECT * FROM orders WHERE id = ? AND seller_id = ?", (order_id, user["id"])).fetchone()
        if not o or o["status"] != ST_WORK or not o["cancel_req_at"]:
            raise HTTPException(400, "Запроса на отмену нет")
        if act == "accept":
            refund = cancel_full(conn, o)
            order_note(conn, o, o["user_id"], f"Продавец согласился отменить заказ {o['num']}")
            notify(conn, o["user_id"], "cancel", f"Продавец согласился отменить заказ {o['num']}."
                   + (f" Возвращено {rub(refund)} на баланс." if refund else ""), f"#/order/{o['id']}")
        else:
            conn.execute("UPDATE orders SET cancel_req_at = NULL, cancel_reason = '' WHERE id = ?", (o["id"],))
            order_note(conn, o, o["user_id"], f"Продавец отклонил запрос на отмену заказа {o['num']}")
            notify(conn, o["user_id"], "cancel", f"Продавец отклонил запрос на отмену заказа {o['num']}. "
                   "Если договориться не получается, откройте спор на странице заказа.", f"#/order/{o['id']}")
        return {"ok": True}


@router.post("/sales/seen")
def sales_seen(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Продавец увидел уведомления о новых покупках."""
    conn.execute("UPDATE orders SET seller_seen = 1 WHERE seller_id = ? AND seller_seen = 0", (user["id"],))
    return {"ok": True}


def gw_pay_url(conn: sqlite3.Connection, user, op: sqlite3.Row) -> str:
    email = user["email"] if user["email"] and "@" in user["email"] else ""
    return pay.payment_url(op["inv_id"], op["coins"], f"Пополнение баланса RUWORK: {nf(op['coins'])} монет",
                           op["method"], email)


@router.post("/wallet/deposit")
def deposit(body: DepositIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Пополнение выбранным способом (DEP_METHODS). Со шлюзом (app/pay.py) создаётся счёт и возвращается
    pay_url — монеты зачисляются по уведомлению шлюза об оплате. Без шлюза — заявка для администратора."""
    a = int_amount(body.amount)
    if a is None or not MIN_DEP <= a <= MAX_DEP:
        raise HTTPException(400, f"Введите целое число от {nf(MIN_DEP)} до {nf(MAX_DEP)}")
    if body.method not in DEP_METHODS:
        raise HTTPException(400, "Выберите способ оплаты")
    guard.throttle("dep:" + user["id"], (10, 600), "Слишком много попыток пополнения — подождите 10 минут")
    with tx(conn):
        if not pay.enabled():
            add_op(conn, user["id"], "dep", body.method, a, "pending")
            guard.alert(f"Заявка на пополнение: {user['name']} — {nf(a)} мон. ({DEP_METHODS[body.method]})", "#/admin/ops")
            return user_state(conn, user["id"])
        inv = conn.execute("SELECT coalesce(max(inv_id), 1000) + 1 FROM wallet_ops").fetchone()[0]
        op_id = add_op(conn, user["id"], "dep", body.method, a, "pending", "Robokassa · ожидает оплаты")
        conn.execute("UPDATE wallet_ops SET inv_id = ? WHERE id = ?", (inv, op_id))
        op = conn.execute("SELECT * FROM wallet_ops WHERE id = ?", (op_id,)).fetchone()
        return {**user_state(conn, user["id"]), "pay_url": gw_pay_url(conn, user, op)}


@router.post("/wallet/deposit/{op_id}/pay")
def deposit_pay(op_id: str, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Ссылка на оплату ещё не оплаченного пополнения — если покупатель закрыл страницу шлюза."""
    op = conn.execute("SELECT * FROM wallet_ops WHERE id = ? AND user_id = ? AND t = 'dep'", (op_id, user["id"])).fetchone()
    if not op or op["status"] != "pending" or op["inv_id"] is None or not pay.enabled():
        raise HTTPException(400, "Это пополнение уже нельзя оплатить. Создайте новое.")
    return {"pay_url": gw_pay_url(conn, user, op)}


@router.post("/wallet/refund")
def refund_to_card(body: RefundIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """Возврат неизрасходованного пополнения по заявке — тем же способом, которым пополняли (через платёжный сервис).
    Сумма раскладывается по пополнениям, начиная с последних, — по одной заявке на каждый исходный платёж
    (шлюз возвращает деньги в пределах суммы платежа). Монеты удерживаются, пока администратор не проведёт возврат."""
    a = int_amount(body.amount)
    if a is None or a < 1:
        raise HTTPException(400, "Введите целое число монет")
    with tx(conn):
        coins, earned = get_wallet(conn, user["id"])
        if a > coins - earned:
            raise HTTPException(400, f"Вернуть можно не больше {nf(max(0, coins - earned))} монет — неистраченное пополнение")
        deps = conn.execute(
            "SELECT d.id, d.ts, d.method, d.coins - coalesce((SELECT sum(r.coins) FROM wallet_ops r WHERE r.src = d.id"
            " AND r.t = 'rf' AND r.status != 'cancelled'), 0) AS free FROM wallet_ops d"
            " WHERE d.user_id = ? AND d.t = 'dep' AND d.status = 'done' ORDER BY d.ts DESC, d.rowid DESC",
            (user["id"],),
        ).fetchall()
        left = a
        for d in deps:
            part = min(left, d["free"])
            if part <= 0:
                continue
            day = time.strftime("%d.%m.%Y", time.localtime(d["ts"] / 1000))
            conn.execute(
                "INSERT INTO wallet_ops(id, user_id, t, method, coins, fee, status, dest, ts, src)"
                " VALUES (?, ?, 'rf', ?, ?, 0, 'pending', ?, ?, ?)",
                (new_id(), user["id"], d["method"] or "sbp", part, f"На платёж пополнения от {day}", now_ms(), d["id"]),
            )
            left -= part
            if not left:
                break
        if left:
            raise HTTPException(400, "Не найдено пополнений, на которые можно сделать возврат. Напишите в поддержку.")
        add_coins(conn, user["id"], -a)
        guard.alert(f"Заявка на возврат пополнения: {user['name']} — {nf(a)} мон.", "#/admin/ops")
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
