"""Защита и модерация: ограничение частоты запросов, скрытие контактов в переписке, стоп-слова в объявлениях
и оповещения администратору в Telegram.

Счётчики частоты живут в памяти процесса: после перезапуска сервера они обнуляются, этого достаточно,
чтобы перебор пароля или спам растянулись на недели.
"""

import logging
import os
import re
import time

from fastapi import HTTPException

from . import auth, tg

log = logging.getLogger("ruwork.guard")

# ---------- Частота запросов ----------

hits: dict[str, list[float]] = {}


def limited(key: str, limit: tuple[int, int]) -> bool:
    count, window = limit
    now = time.time()
    recent = [t for t in hits.get(key, []) if now - t < window]
    if recent:
        hits[key] = recent
    else:
        hits.pop(key, None)
    if len(hits) > 50_000:  # старые ключи чистим, чтобы словарь не рос бесконечно
        for k in [k for k, v in hits.items() if now - v[-1] > 3600]:
            del hits[k]
    return len(recent) >= count


def hit(key: str) -> None:
    hits.setdefault(key, []).append(time.time())


def throttle(key: str, limit: tuple[int, int], text: str = "Слишком часто — подождите немного и попробуйте снова") -> None:
    if limited(key, limit):
        raise HTTPException(429, text)
    hit(key)


client_ip = auth.client_ip


# ---------- Контакты в переписке ----------

# Сделка вне платформы лишает покупателя защиты, поэтому телефоны, почта, @юзернеймы и ссылки
# в чатах заменяются заглушкой. Ссылки на сам RUWORK остаются.
CONTACT_STUB = "[контакт скрыт]"
CONTACT_RES = [
    re.compile(r"(?:https?://|www\.)\S+", re.I),
    re.compile(r"\b(?:t\.me|telegram\.me|wa\.me|vk\.com|vk\.me|instagram\.com|ok\.ru|discord\.gg|max\.ru)/\S*", re.I),
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}\b"),
    re.compile(r"(?<!\d)(?:\+?[78][\s\-()]*)?\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"),
]
OWN_LINK = re.compile(r"^(?:https?://)?(?:www\.)?ruwork\.shop(?:/\S*)?$", re.I)


def mask_contacts(text: str) -> str:
    for rx in CONTACT_RES:
        text = rx.sub(lambda m: m.group(0) if OWN_LINK.match(m.group(0)) else CONTACT_STUB, text)
    return text


# ---------- Запрещённые услуги ----------

# Основы слов (после замены ё → е). Совпадение ищется с начала слова: «взломать» найдётся,
# а «защита от взлома» — нет, потому что в списке «взломат», а не «взлом».
BANNED = [
    "наркот", "закладк", "кладмен", "мефедрон", "амфетамин", "спайс", "гашиш", "марихуан",
    "оружи", "боеприпас", "взрывчат", "поддельн", "фальшив", "липов", "купить диплом", "диплом без",
    "права без экзамен", "пробив", "обнал", "дроповод", "взломат", "взлом аккаунт",
    "взлом страниц", "ддос", "ddos", "казино", "порно", "эскорт", "интим-услуг", "интим услуг", "вебкам", "даркнет",
]
BANNED_RE = re.compile(r"(?<![а-яa-z])(?:" + "|".join(re.escape(w) for w in BANNED) + ")", re.I)


def check_banned(*texts: str) -> None:
    """Отклоняет объявление или задание с запрещённой услугой."""
    m = BANNED_RE.search(" ".join(texts).lower().replace("ё", "е"))
    if m:
        raise HTTPException(400, f"Такие услуги запрещены правилами RUWORK (найдено: «{m.group(0)}»). "
                                 "Если это ошибка — напишите в поддержку.")


# ---------- Оповещения администратору ----------

ADMIN_CHAT = int(os.environ.get("RUWORK_TG_ADMIN_CHAT", "0") or 0)
ALERT_GAP = 600  # одинаковые оповещения (например, ошибки одного адреса) — не чаще раза в 10 минут
alerted: dict[str, float] = {}


def admin_chat() -> int:
    """Куда слать оповещения: RUWORK_TG_ADMIN_CHAT, иначе Telegram, привязанный к аккаунту администратора,
    иначе чат поддержки."""
    if ADMIN_CHAT:
        return ADMIN_CHAT
    from .db import connect
    conn = connect()
    try:
        row = conn.execute("SELECT tg_chat FROM users WHERE email = ?", (auth.ADMIN_EMAIL,)).fetchone()
    finally:
        conn.close()
    if row and row["tg_chat"]:
        return row["tg_chat"]
    return int(os.environ.get("RUWORK_TG_SUPPORT_CHAT", "0") or 0)


def alert(text: str, link: str = "", key: str = "") -> None:
    """Сообщение администратору в Telegram. key — ключ для подавления повторов."""
    if key:
        now = time.time()
        if now - alerted.get(key, 0) < ALERT_GAP:
            return
        alerted[key] = now
    try:
        tg.send(admin_chat(), "🛎 " + text, link)
    except Exception as e:  # оповещение не должно ломать запрос пользователя
        log.warning("Не удалось отправить оповещение: %s", e)
