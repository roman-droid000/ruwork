"""Уведомления платёжного шлюза Robokassa (app/pay.py).

В настройках магазина Robokassa:
  Result URL  — https://<домен>/api/pay/robokassa/result   (метод POST)
  Success URL — https://<домен>/api/pay/robokassa/success  (GET или POST)
  Fail URL    — https://<домен>/api/pay/robokassa/fail     (GET или POST)
"""

import logging
import sqlite3
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from .. import guard, pay
from ..db import get_db, tx
from ..logic import DEP_METHODS, add_coins, nf, notify

log = logging.getLogger("ruwork.pay")
router = APIRouter(prefix="/api/pay/robokassa", include_in_schema=False)


async def params_of(request: Request) -> dict[str, str]:
    """Параметры из строки запроса и из тела формы (application/x-www-form-urlencoded)."""
    p = dict(request.query_params)
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", "replace")
        p.update(parse_qsl(body, keep_blank_values=True))
    return p


@router.api_route("/result", methods=["GET", "POST"])
async def result(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Robokassa сообщает об оплате. Ответ «OK<InvId>» — уведомление принято, иначе шлюз повторит его."""
    p = await params_of(request)
    if not pay.enabled() or not pay.check_result(p):
        log.warning("Robokassa: неверная подпись уведомления InvId=%s", p.get("InvId"))
        return PlainTextResponse("bad sign", 400)
    try:
        inv, paid = int(p["InvId"]), Decimal(p["OutSum"])
    except (ValueError, InvalidOperation):
        return PlainTextResponse("bad request", 400)
    with tx(conn):
        op = conn.execute("SELECT * FROM wallet_ops WHERE inv_id = ? AND t = 'dep'", (inv,)).fetchone()
        if not op:
            guard.alert(f"Robokassa: оплата счёта {inv} на {p['OutSum']} ₽, но пополнения с таким номером нет", "#/admin/ops")
            return PlainTextResponse("unknown invoice", 400)
        if paid != op["coins"]:
            guard.alert(f"Robokassa: счёт {inv} оплачен на {p['OutSum']} ₽ вместо {op['coins']} ₽ — проверьте вручную",
                        "#/admin/ops")
            return PlainTextResponse("bad sum", 400)
        if op["status"] != "done":  # повторное уведомление ничего не начисляет
            add_coins(conn, op["user_id"], op["coins"])
            conn.execute("UPDATE wallet_ops SET status = 'done', dest = ? WHERE id = ?", (f"Robokassa · счёт {inv}", op["id"]))
            notify(conn, op["user_id"], "wallet", f"Баланс пополнен на {nf(op['coins'])} мон.", "#/dashboard/wallet")
            u = conn.execute("SELECT name FROM users WHERE id = ?", (op["user_id"],)).fetchone()
            guard.alert(f"Пополнение через Robokassa: {u['name'] if u else op['user_id']} — {nf(op['coins'])} мон."
                        f" ({DEP_METHODS.get(op['method'], op['method'])})"
                        + (" — заявка была отменена, монеты всё равно зачислены" if op["status"] == "cancelled" else ""),
                        "#/admin/ops")
    return PlainTextResponse(f"OK{inv}")


@router.api_route("/success", methods=["GET", "POST"])
async def success(request: Request):
    """Покупатель вернулся после оплаты. Монеты зачисляет только /result — здесь лишь возврат на сайт."""
    p = await params_of(request)
    return RedirectResponse("/?pay=" + ("ok" if pay.enabled() and pay.check_success(p) else "wait") + "#/dashboard/wallet", 303)


@router.api_route("/fail", methods=["GET", "POST"])
async def fail(request: Request):
    return RedirectResponse("/?pay=fail#/dashboard/wallet", 303)
