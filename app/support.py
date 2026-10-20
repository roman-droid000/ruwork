"""Поддержка в Telegram-боте: ответы на частые вопросы и переписка клиента с оператором.

Клиент пишет боту: кнопки меню отвечают шаблонами из FAQ, любое другое сообщение уходит в чат
поддержки (RUWORK_TG_SUPPORT_CHAT — личный чат оператора или группа). Оператор отвечает реплаем
на сообщение клиента — ответ приходит клиенту от имени бота. Реплай «/t <шаблон>» отправляет готовый ответ.

chat_id для RUWORK_TG_SUPPORT_CHAT бот подскажет по команде /id в нужном чате.
"""

import logging
import os
import sqlite3
import time

from . import tg
from .db import now_ms

SUPPORT_CHAT = int(os.environ.get("RUWORK_TG_SUPPORT_CHAT", "0") or 0)
FALLBACK = "@ruwork001"  # куда писать, пока чат поддержки не настроен
ACK_EVERY = 600  # «Сообщение передано оператору» — не чаще раза в 10 минут на клиента

log = logging.getLogger("ruwork.support")
_acked: dict[int, float] = {}

# Шаблоны ответов. public=True — кнопка в меню клиента; все шаблоны доступны оператору через /t <ключ>.
FAQ: dict[str, tuple[str, bool, str]] = {
    "pay": ("💳 Как оплатить заказ", True,
            "Заказы оплачиваются с баланса RUWORK.\n\n"
            "1. Пополните баланс: «Личный кабинет» → «Кошелёк» → «Пополнить».\n"
            "2. Выберите услугу и тариф, нажмите «Купить сейчас» или добавьте в корзину.\n"
            "3. В корзине нажмите «Оплатить».\n\n"
            "Деньги продавец получит только после того, как вы подтвердите получение результата."),
    "topup": ("➕ Пополнение баланса", True,
              "Пополнение: «Личный кабинет» → «Кошелёк» → «Пополнить», сумма от 100 до 100 000.\n\n"
              "После оплаты заявка получает статус «В обработке», монеты зачисляются, когда администратор "
              "подтвердит поступление. Мы пришлём уведомление. Если заявка долго висит в обработке — "
              "напишите сюда и приложите чек."),
    "withdraw": ("💸 Вывод денег", True,
                 "Вывод: «Личный кабинет» → «Кошелёк» → «Вывести».\n\n"
                 "• от 500 монет и не больше баланса;\n"
                 "• комиссия 2 % (минимум 10 монет);\n"
                 "• номер телефона в формате +7XXXXXXXXXX и банк получателя;\n"
                 "• подтверждение паролем от аккаунта.\n\n"
                 "Сумма удерживается сразу, выплату проводит администратор. Если заявку отклонят, "
                 "монеты вернутся на баланс. Ожидающую заявку можно отменить самому."),
    "deal": ("🛡 Как проходит сделка", True,
             "1. Вы оплачиваете заказ — деньги хранятся на платформе, а не у продавца.\n"
             "2. Через минуту заказ переходит «В работе».\n"
             "3. Продавец загружает результат — заказ «На проверке».\n"
             "4. Вы проверяете и нажимаете «Подтвердить получение» — продавец получает оплату.\n\n"
             "Если не подтвердить, заказ подтвердится сам через 7 дней после загрузки результата "
             "(за сутки придёт напоминание). Не устраивает результат — откройте спор до этого срока."),
    "cancel": ("↩️ Отмена и возврат", True,
               "Отменить заказ можно в «Личный кабинет» → «Покупки».\n\n"
               "• в первую минуту («Ожидает подтверждения») — возврат 100 %;\n"
               "• когда заказ уже «В работе» — возврат 90 %, 10 % удерживаются за начатую работу;\n"
               "• когда результат загружен («На проверке») — отмена недоступна, но можно открыть спор.\n\n"
               "Деньги возвращаются на баланс RUWORK сразу."),
    "dispute": ("⚖️ Спор с продавцом", True,
                "Если продавец не выполняет работу или результат не соответствует заказу:\n\n"
                "1. Откройте страницу заказа и нажмите «Открыть спор».\n"
                "2. Опишите проблему (от 10 символов).\n\n"
                "Оплата замораживается, автоподтверждение не сработает. Администратор изучит переписку "
                "и результат и решит: оплата продавцу, полный возврат вам или деление пополам. "
                "Если договорились с продавцом — спор можно закрыть самому."),
    "result": ("📦 Нет результата / сроки", True,
               "Сроки зависят от тарифа: «Стандарт» — 5 дней, «Стандарт+без очереди» — 3 дня.\n\n"
               "Напишите продавцу в чате заказа («Покупки» → «Чат с продавцом»). Если продавец не отвечает "
               "или срок прошёл — откройте спор на странице заказа, оплата будет заморожена до решения."),
    "sell": ("🧑‍💻 Как начать продавать", True,
             "«Личный кабинет» → «Объявления» → «Создать объявление»: категория, название, описание, "
             "цена (от 100 ₽ и не ниже минимума подкатегории), теги и обложка.\n\n"
             "Комиссия платформы — 5 % с завершённой продажи. Оплата зачисляется на баланс, когда покупатель "
             "подтвердит получение (или автоматически через 7 дней после загрузки результата)."),
    "promo": ("🚀 Продвижение объявления", True,
              "В «Моих объявлениях»:\n"
              "• «Поднять» — бесплатно раз в сутки, объявление встаёт выше обычных;\n"
              "• «Продвигать» — 99 монет за сутки, объявление показывается выше всех с меткой «Продвигается».\n\n"
              "Метку «ТОП» объявление получает само после 10 выполненных заказов."),
    "password": ("🔑 Забыл пароль", True,
                 "На странице входа нажмите «Забыли пароль?», укажите email аккаунта, новый пароль и контакт "
                 "для связи. Пароль сменится после проверки администратором — мы можем написать вам, "
                 "чтобы убедиться, что аккаунт ваш."),
    "notify": ("🔔 Уведомления в Telegram", True,
               "«Личный кабинет» → «Профиль» → «Подключить Telegram». Сюда будут приходить новые заказы, "
               "сообщения, результаты работы и решения по спорам. Отключить — командой /stop."),
    # Только для оператора.
    "hello": ("Приветствие", False,
              "Здравствуйте! Это поддержка RUWORK. Чем можем помочь?"),
    "num": ("Нужен номер заказа", False,
            "Подскажите, пожалуйста, номер заказа (например, #RW-1040) и email аккаунта — так мы быстрее найдём сделку."),
    "check": ("Проверяем", False,
              "Спасибо, приняли. Проверяем информацию и вернёмся с ответом в ближайшее время."),
    "block": ("Аккаунт заблокирован", False,
              "Аккаунт заблокирован администратором за нарушение правил площадки. Причина указана при входе. "
              "Если считаете это ошибкой — опишите ситуацию, мы пересмотрим решение."),
    "bye": ("Завершение", False,
            "Рады были помочь! Если появятся вопросы — пишите сюда в любое время."),
}

GREETING = ("Здравствуйте! Это поддержка RUWORK 👋\n\n"
            "Выберите вопрос ниже или просто напишите сообщение — оператор ответит здесь же. "
            "Если вопрос про заказ, укажите его номер, например #RW-1040.")


def menu_kb() -> dict:
    btns = [{"text": title, "callback_data": "faq:" + key} for key, (title, public, _) in FAQ.items() if public]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([{"text": "✍️ Написать оператору", "callback_data": "op"}])
    return {"inline_keyboard": rows}


def after_kb() -> dict:
    return {"inline_keyboard": [[{"text": "◀️ Другой вопрос", "callback_data": "menu"},
                                 {"text": "✍️ Оператору", "callback_data": "op"}]]}


def _call(method: str, payload: dict) -> dict:
    try:
        return tg.call(method, payload)
    except Exception as e:
        log.warning("Telegram %s: %s", method, e)
        return {}


def say(chat: int, text: str, kb: dict | None = None) -> dict:
    p = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = kb
    return _call("sendMessage", p)


def handle(conn: sqlite3.Connection, upd: dict) -> None:
    if cb := upd.get("callback_query"):
        return on_button(cb)
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    if not chat.get("id"):
        return
    if text.split("@")[0] == "/id":
        say(chat["id"], f"chat_id этого чата: {chat['id']}")
    elif SUPPORT_CHAT and chat["id"] == SUPPORT_CHAT:
        on_operator(conn, msg, text)
    elif chat.get("type") == "private":
        on_client(conn, msg, text)


def on_button(cb: dict) -> None:
    _call("answerCallbackQuery", {"callback_query_id": cb["id"]})
    chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
    data = cb.get("data") or ""
    if not chat:
        return
    if data.startswith("faq:") and data[4:] in FAQ:
        say(chat, FAQ[data[4:]][2], after_kb())
    elif data == "op":
        say(chat, "Опишите вопрос одним сообщением (можно с фото или скриншотом). "
                  "Если он про заказ — укажите номер. Оператор ответит здесь.")
    else:
        say(chat, "Выберите вопрос:", menu_kb())


def on_client(conn: sqlite3.Connection, msg: dict, text: str) -> None:
    chat = msg["chat"]["id"]
    if text.startswith("/start"):
        code = text[6:].strip()
        u = conn.execute("SELECT id, name FROM users WHERE tg_code = ?", (code,)).fetchone() if code and tg.link_code_fresh(code) else None
        if u:
            # Один чат — один аккаунт: со старого аккаунта привязка снимается.
            conn.execute("UPDATE users SET tg_chat = NULL WHERE tg_chat = ?", (chat,))
            conn.execute("UPDATE users SET tg_chat = ?, tg_code = NULL WHERE id = ?", (chat, u["id"]))
            say(chat, f"Готово, {u['name']}! Сюда будут приходить уведомления RUWORK: новые заказы, "
                      "сообщения, результаты работы и решения по спорам. Отключить: /stop\n\n"
                      "А ещё здесь можно задать вопрос поддержке.", menu_kb())
        else:
            say(chat, GREETING, menu_kb())
        return
    if text in ("/menu", "/help"):
        return say(chat, GREETING, menu_kb())
    if text == "/stop":
        if conn.execute("UPDATE users SET tg_chat = NULL WHERE tg_chat = ?", (chat,)).rowcount:
            say(chat, "Уведомления RUWORK отключены. Подключить снова можно в профиле на сайте.")
        return
    if text.startswith("/"):
        return say(chat, "Не знаю такой команды. Выберите вопрос:", menu_kb())
    if not SUPPORT_CHAT:
        return say(chat, f"Сейчас оператор недоступен в боте. Напишите нам: {FALLBACK}", after_kb())

    frm = msg.get("from") or {}
    who = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or "Без имени"
    if frm.get("username"):
        who += f" (@{frm['username']})"
    acc = conn.execute("SELECT name, email FROM users WHERE tg_chat = ?", (chat,)).fetchone()
    head = f"📩 Обращение от {who}\nchat_id: {chat}\n"
    head += f"Аккаунт RUWORK: {acc['name']}, {acc['email']}" if acc else "Аккаунт RUWORK не привязан"
    head += "\n\nОтветьте реплаем. Шаблоны: /t"
    h = say(SUPPORT_CHAT, head).get("result")
    c = _call("copyMessage", {"chat_id": SUPPORT_CHAT, "from_chat_id": chat, "message_id": msg["message_id"],
                              **({"reply_to_message_id": h["message_id"]} if h else {})}).get("result")
    ids = [x["message_id"] for x in (h, c) if x]
    if not ids:
        return say(chat, f"Не получилось передать сообщение. Напишите нам: {FALLBACK}")
    conn.executemany("INSERT OR REPLACE INTO tg_support(admin_msg, client_chat, ts) VALUES (?, ?, ?)",
                     [(i, chat, now_ms()) for i in ids])
    if time.time() - _acked.get(chat, 0) > ACK_EVERY:
        _acked[chat] = time.time()
        say(chat, "Сообщение передано оператору, ответим здесь. Пока ждёте — возможно, ответ уже есть в меню.",
            after_kb())


def on_operator(conn: sqlite3.Connection, msg: dict, text: str) -> None:
    reply = msg.get("reply_to_message")
    if text.split("@")[0] == "/t" and not reply:
        lines = [f"/t {k} — {title}" for k, (title, _, _) in FAQ.items()]
        return say(SUPPORT_CHAT, "Ответьте реплаем на сообщение клиента:\n" + "\n".join(lines))
    if not reply:
        if not text.startswith("/"):
            say(SUPPORT_CHAT, "Чтобы ответить клиенту, сделайте реплай на его сообщение.")
        return
    row = conn.execute("SELECT client_chat FROM tg_support WHERE admin_msg = ?", (reply["message_id"],)).fetchone()
    if not row:
        return say(SUPPORT_CHAT, "Не нашёл клиента для этого сообщения. Ответьте реплаем на «📩 Обращение» или его сообщение.")
    client = row["client_chat"]
    if text.startswith("/t"):
        key = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if key not in FAQ:
            return say(SUPPORT_CHAT, "Нет такого шаблона. Список: /t")
        ok = say(client, FAQ[key][2], after_kb() if FAQ[key][1] else None)
    else:
        ok = _call("copyMessage", {"chat_id": client, "from_chat_id": SUPPORT_CHAT, "message_id": msg["message_id"]})
    if ok.get("ok"):
        _call("setMessageReaction", {"chat_id": SUPPORT_CHAT, "message_id": msg["message_id"],
                                     "reaction": [{"type": "emoji", "emoji": "👍"}]})
    else:
        say(SUPPORT_CHAT, "Не доставлено: клиент мог заблокировать бота.")
