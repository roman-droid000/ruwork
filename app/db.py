"""SQLite: подключение, схема и начальное заполнение каталога."""

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("RUWORK_DB", ROOT / "ruwork.db"))
SEED_PATH = Path(__file__).resolve().parent / "seed.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email      TEXT NOT NULL UNIQUE,
    salt       TEXT NOT NULL,
    hash       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sellers (
    name         TEXT PRIMARY KEY,
    about        TEXT NOT NULL,
    reviews_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gigs (
    id           TEXT PRIMARY KEY,
    cat          TEXT NOT NULL,
    sub          TEXT NOT NULL,
    title        TEXT NOT NULL,
    base         INTEGER NOT NULL,
    rating       REAL NOT NULL DEFAULT 0,
    rev          INTEGER NOT NULL DEFAULT 0,
    seller       TEXT NOT NULL,
    lvl          TEXT NOT NULL,
    descr        TEXT NOT NULL,
    top          INTEGER NOT NULL DEFAULT 0,
    tags_json    TEXT NOT NULL,
    owner_id     TEXT REFERENCES users(id) ON DELETE CASCADE,
    details_json TEXT,
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    num        TEXT NOT NULL,
    gig_id     TEXT NOT NULL,
    title      TEXT NOT NULL,
    tier       INTEGER NOT NULL,
    status     TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    method     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER
);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, created_at);
CREATE TABLE IF NOT EXISTS wallets (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    coins   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wallet_ops (
    id      TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    t       TEXT NOT NULL,
    method  TEXT NOT NULL,
    coins   INTEGER NOT NULL,
    fee     INTEGER NOT NULL DEFAULT 0,
    status  TEXT NOT NULL,
    dest    TEXT NOT NULL DEFAULT '',
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS wallet_ops_user ON wallet_ops(user_id, ts);
CREATE TABLE IF NOT EXISTS chat_messages (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    owner  TEXT NOT NULL,
    gig_id TEXT NOT NULL,
    role   TEXT NOT NULL,
    text   TEXT NOT NULL,
    ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_owner_gig ON chat_messages(owner, gig_id, id);
CREATE TABLE IF NOT EXISTS password_resets (
    id      TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    contact TEXT NOT NULL,
    salt    TEXT NOT NULL,
    hash    TEXT NOT NULL,
    status  TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def connect() -> sqlite3.Connection:
    # isolation_level=None: транзакции открываем явно через tx().
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """Зависимость FastAPI: одно подключение на запрос."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """Транзакция с блокировкой записи: баланс и заказы меняются атомарно."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def load_seed() -> dict:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


SEED = load_seed()


def ensure_admin(conn: sqlite3.Connection) -> None:
    """Создаёт аккаунт администратора, если его ещё нет. Существующий аккаунт не меняется."""
    from . import auth

    if conn.execute("SELECT 1 FROM users WHERE email = ?", (auth.ADMIN_EMAIL,)).fetchone():
        return
    password = os.environ.get("RUWORK_ADMIN_PASSWORD", "")
    if not password:
        logging.getLogger("ruwork").warning(
            "Аккаунт администратора не создан: задайте RUWORK_ADMIN_PASSWORD при запуске"
        )
        return
    if conn.execute("SELECT 1 FROM users WHERE name = ?", (auth.ADMIN_NAME,)).fetchone():
        logging.getLogger("ruwork").warning(
            "Аккаунт администратора не создан: имя %s занято другим аккаунтом", auth.ADMIN_NAME
        )
        return
    salt, pw_hash = auth.new_password(password)
    conn.execute(
        "INSERT INTO users(id, name, email, salt, hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("u-admin", auth.ADMIN_NAME, auth.ADMIN_EMAIL, salt, pw_hash, now_ms()),
    )


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Миграция: время последней активности пользователя (для списка «Онлайн» в админ-панели).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "last_seen" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER")
        # Миграция: блокировка пользователя администратором (время блокировки и причина).
        if "blocked_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN blocked_at INTEGER")
            conn.execute("ALTER TABLE users ADD COLUMN block_reason TEXT NOT NULL DEFAULT ''")
        # Миграция: продавец заказа (автор объявления) и отметка, что он видел уведомление о покупке.
        ocols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        if "seller_id" not in ocols:
            conn.execute("ALTER TABLE orders ADD COLUMN seller_id TEXT")
            conn.execute("ALTER TABLE orders ADD COLUMN seller_seen INTEGER NOT NULL DEFAULT 1")
            conn.execute("UPDATE orders SET seller_id = (SELECT owner_id FROM gigs WHERE gigs.id = orders.gig_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS orders_seller ON orders(seller_id, created_at)")
        with tx(conn):
            for name, (about, reviews) in SEED["sellers"].items():
                conn.execute(
                    "INSERT OR IGNORE INTO sellers(name, about, reviews_json) VALUES (?, ?, ?)",
                    (name, about, json.dumps(reviews, ensure_ascii=False)),
                )
            # Каталог заполняется один раз (отметка в user_version), чтобы удалённые админом услуги не возвращались.
            seeded = conn.execute("PRAGMA user_version").fetchone()[0] >= 1
            if not seeded and not conn.execute("SELECT 1 FROM gigs WHERE owner_id IS NULL LIMIT 1").fetchone():
                ts = now_ms()
                for g in SEED["gigs"]:
                    conn.execute(
                        "INSERT INTO gigs(id, cat, sub, title, base, rating, rev, seller, lvl, descr, top,"
                        " tags_json, owner_id, details_json, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                        (
                            g["id"], g["cat"], g["sub"], g["title"], g["base"], g["rating"], g["rev"],
                            g["seller"], g["lvl"], g["desc"], int(g["top"]),
                            json.dumps(g["tags"], ensure_ascii=False),
                            json.dumps(g["details"], ensure_ascii=False) if g["details"] else None,
                            ts,
                        ),
                    )
            conn.execute("PRAGMA user_version = 1")
            ensure_admin(conn)
    finally:
        conn.close()
