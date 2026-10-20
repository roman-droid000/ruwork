"""Уведомления в Telegram через бота.

Пользователь нажимает «Подключить Telegram» в профиле, сайт выдаёт ссылку t.me/<бот>?start=<код>,
бот получает /start <код> через webhook и запоминает chat_id. После этого каждое уведомление из колокольчика
(notify в logic.py) дублируется сообщением в Telegram. Тот же бот отвечает клиентам поддержки (support.py).

Без RUWORK_TG_TOKEN всё выключено: кнопка в профиле не показывается, сообщения не отправляются.
"""

import hashlib
import html
import json
import logging
import os
import secrets
import threading
import time
import urllib.request

TOKEN = os.environ.get("RUWORK_TG_TOKEN", "")
# 0 — webhook не ставится: сообщения боту принимает отдельный бот поддержки (ruwork_support, polling),
# сайт только отправляет уведомления и выдаёт ссылку для привязки.
WEBHOOK = os.environ.get("RUWORK_TG_WEBHOOK", "1") != "0"
PUBLIC_URL = os.environ.get("RUWORK_PUBLIC_URL", "https://ruwork.shop").rstrip("/")
# Секрет в адресе webhook и в заголовке: чужие запросы на /api/tg/... отклоняются.
SECRET = hashlib.sha256(("ruwork-tg:" + TOKEN).encode()).hexdigest()[:40] if TOKEN else ""
username = ""

log = logging.getLogger("ruwork.tg")


def call(method: str, payload: dict | None = None, timeout: float = 10) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def setup() -> None:
    """При старте сервера: узнать имя бота и направить webhook на сайт."""
    global username
    if not TOKEN:
        return
    try:
        username = call("getMe")["result"]["username"]
        if not WEBHOOK:
            return
        call("setWebhook", {"url": f"{PUBLIC_URL}/api/tg/{SECRET}", "secret_token": SECRET,
                            "allowed_updates": ["message", "callback_query"]})
        call("setMyCommands", {"commands": [{"command": "menu", "description": "Частые вопросы и поддержка"},
                                            {"command": "stop", "description": "Отключить уведомления"}]})
    except Exception as e:  # сайт должен работать и без Telegram
        log.warning("Telegram не настроен: %s", e)


def send(chat_id: int | None, text: str, link: str = "") -> None:
    """Отправить сообщение в фоне, чтобы запрос пользователя не ждал Telegram."""
    if not (TOKEN and chat_id):
        return
    # Тексты уведомлений хранятся экранированными для innerHTML; Telegram получает обычный текст.
    payload = {"chat_id": chat_id, "text": html.unescape(text)[:4000], "disable_web_page_preview": True}
    if link:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть на RUWORK", "url": f"{PUBLIC_URL}/{link}"}]]}

    def run():
        try:
            call("sendMessage", payload)
        except Exception as e:
            log.warning("Telegram: не удалось отправить сообщение: %s", e)

    threading.Thread(target=run, daemon=True).start()


LINK_CODE_TTL = 15 * 60  # ссылка «Подключить Telegram» действует 15 минут


def new_link_code() -> str:
    """Одноразовый код для t.me/<бот>?start=<код>. В конце — время выдачи (hex), чтобы код устаревал:
    привязанный Telegram управляет заказами в мини-приложении, и старая ссылка не должна давать доступ."""
    return f"{secrets.token_urlsafe(16)}_{int(time.time()):x}"


def link_code_fresh(code: str) -> bool:
    try:
        issued = int(code.rsplit("_", 1)[1], 16)
    except (IndexError, ValueError):
        return False
    return 0 <= time.time() - issued <= LINK_CODE_TTL
