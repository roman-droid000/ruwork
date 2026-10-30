"""Фоновые задачи сервера.

Раз в минуту:
- двигает заказы по таймерам (tick) у всех, у кого есть активные заказы, — автоподтверждение и напоминания
  срабатывают, даже если никто не открывает сайт;
- напоминает продавцу, что до срока сдачи осталось меньше суток, а после срока — обеим сторонам.

Раз в сутки — резервная копия базы в backups/ (сжатый файл, хранятся последние BACKUP_KEEP).
Если задан RUWORK_TG_BACKUP_CHAT, копия ещё и отправляется в этот Telegram-чат — так она хранится
не только на сервере. Загруженные файлы (uploads/) копируются отдельно, см. README.

Задачи выполняет один процесс: если uvicorn запущен с несколькими воркерами, остальные видят
занятую блокировку и ничего не делают.
"""

import fcntl
import gzip
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import guard, tg
from .db import DB_PATH, ROOT, connect, now_ms, tx
from .logic import DAY_MS, ST_REVIEW, ST_WAIT, ST_WORK, deadline, notify, tick

log = logging.getLogger("ruwork.jobs")

BACKUP_DIR = Path(os.environ.get("RUWORK_BACKUPS", ROOT / "backups"))
BACKUP_KEEP = 14
BACKUP_EVERY = 24 * 3600
BACKUP_CHAT = int(os.environ.get("RUWORK_TG_BACKUP_CHAT", "0") or 0)
TG_FILE_MAX = 49 * 1024 * 1024
PERIOD = int(os.environ.get("RUWORK_JOBS_PERIOD", "60"))  # секунд между проходами

status = {"tick_at": 0, "backup_at": 0, "backup_error": ""}
_lock_file = None


def start() -> None:
    global _lock_file
    if os.environ.get("RUWORK_JOBS", "1") == "0":
        return
    _lock_file = open(ROOT / ".jobs.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # задачи уже выполняет другой воркер
    threading.Thread(target=loop, daemon=True, name="ruwork-jobs").start()


def loop() -> None:
    while True:
        try:
            run_once()
        except Exception:
            log.exception("Фоновые задачи: ошибка")
        time.sleep(PERIOD)


def run_once() -> None:
    conn = connect()
    try:
        with tx(conn):
            ids = {r[0] for r in conn.execute(
                "SELECT user_id FROM orders WHERE status IN (?, ?, ?) UNION"
                " SELECT seller_id FROM orders WHERE status IN (?, ?, ?) AND seller_id IS NOT NULL",
                (ST_WAIT, ST_WORK, ST_REVIEW) * 2)}
            for uid in ids:
                tick(conn, uid)
            remind_deadlines(conn)
            conn.execute("UPDATE users SET blocked_at = NULL, block_reason = '', blocked_until = NULL"
                         " WHERE blocked_until IS NOT NULL AND blocked_until <= ?", (now_ms(),))
        status["tick_at"] = now_ms()
    finally:
        conn.close()
    if time.time() - last_backup_time() >= BACKUP_EVERY:
        backup()


def remind_deadlines(conn: sqlite3.Connection) -> None:
    """dl_reminded: 0 — ничего не отправляли, 1 — «осталось меньше суток», 2 — «срок прошёл»."""
    n = now_ms()
    for o in conn.execute(
        "SELECT * FROM orders WHERE status = ? AND seller_id IS NOT NULL AND started_at IS NOT NULL AND dl_reminded < 2",
        (ST_WORK,),
    ).fetchall():
        dl = deadline(o)
        link = f"#/order/{o['id']}"
        if n >= dl:
            conn.execute("UPDATE orders SET dl_reminded = 2 WHERE id = ?", (o["id"],))
            notify(conn, o["seller_id"], "remind", f"Срок сдачи заказа {o['num']} «{o['title']}» прошёл. Загрузите "
                                                   "результат или попросите покупателя продлить срок.", link)
            notify(conn, o["user_id"], "remind", f"Срок по заказу {o['num']} «{o['title']}» прошёл, а результата пока нет. "
                                                 "Напишите продавцу; если он не отвечает — откройте спор.", link)
        elif o["dl_reminded"] == 0 and dl - n <= DAY_MS:
            conn.execute("UPDATE orders SET dl_reminded = 1 WHERE id = ?", (o["id"],))
            notify(conn, o["seller_id"], "remind", f"До срока сдачи заказа {o['num']} «{o['title']}» меньше суток.", link)


def backups() -> list[Path]:
    return sorted(BACKUP_DIR.glob("ruwork-*.db.gz"))


def last_backup_time() -> float:
    b = backups()
    return b[-1].stat().st_mtime if b else 0


def backup() -> Path | None:
    """Копия базы через SQLite backup API (целостна даже во время записи), сжатая gzip."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = time.strftime("ruwork-%Y%m%d-%H%M")
    raw, gz = BACKUP_DIR / f"{name}.db", BACKUP_DIR / f"{name}.db.gz"
    try:
        src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(raw)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with open(raw, "rb") as f, gzip.open(gz, "wb", 6) as out:
            shutil.copyfileobj(f, out)
        raw.unlink()
        for old in backups()[:-BACKUP_KEEP]:
            old.unlink()
        status["backup_at"], status["backup_error"] = now_ms(), ""
    except Exception as e:
        raw.unlink(missing_ok=True)
        status["backup_error"] = str(e)
        log.exception("Резервная копия не создана")
        guard.alert(f"Резервная копия базы не создана: {e}", key="backup")
        return None
    if BACKUP_CHAT and tg.TOKEN:
        try:
            if gz.stat().st_size > TG_FILE_MAX:
                raise ValueError("файл больше 50 МБ — Telegram его не примет")
            tg.send_file(BACKUP_CHAT, gz, f"Резервная копия RUWORK {time.strftime('%d.%m.%Y %H:%M')}")
        except Exception as e:
            log.warning("Копия не отправлена в Telegram: %s", e)
            guard.alert(f"Резервная копия создана на сервере, но не отправлена в Telegram: {e}", key="backup-tg")
    return gz
