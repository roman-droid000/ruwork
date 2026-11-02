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
# Загруженные файлы: обложки объявлений и результаты заказов. Папка не в git, выкладка её не трогает.
UPLOAD_DIR = Path(os.environ.get("RUWORK_UPLOADS", ROOT / "uploads"))

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
CREATE INDEX IF NOT EXISTS orders_gig ON orders(gig_id, status);
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
CREATE TABLE IF NOT EXISTS order_files (
    id       TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    mime     TEXT NOT NULL,
    size     INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    done     INTEGER NOT NULL DEFAULT 0,
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS order_files_order ON order_files(order_id, ts);
CREATE TABLE IF NOT EXISTS notifications (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,
    text    TEXT NOT NULL,
    link    TEXT NOT NULL DEFAULT '',
    read    INTEGER NOT NULL DEFAULT 0,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, id);
CREATE TABLE IF NOT EXISTS favorites (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gig_id  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (user_id, gig_id)
);
CREATE TABLE IF NOT EXISTS platform_ops (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,
    coins    INTEGER NOT NULL,
    user_id  TEXT,
    order_id TEXT,
    note     TEXT NOT NULL DEFAULT '',
    ts       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id        TEXT PRIMARY KEY,
    order_id  TEXT NOT NULL UNIQUE,
    gig_id    TEXT NOT NULL,
    seller_id TEXT,
    buyer_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stars     INTEGER NOT NULL,
    text      TEXT NOT NULL,
    photo     TEXT,
    reply     TEXT NOT NULL DEFAULT '',
    reply_at  INTEGER,
    ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_gig ON reviews(gig_id, ts);
CREATE INDEX IF NOT EXISTS reviews_seller ON reviews(seller_id, ts);
CREATE TABLE IF NOT EXISTS password_resets (
    id      TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    contact TEXT NOT NULL,
    salt    TEXT NOT NULL,
    hash    TEXT NOT NULL,
    status  TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gig_views (
    gig_id TEXT NOT NULL,
    viewer TEXT NOT NULL,
    day    INTEGER NOT NULL,
    PRIMARY KEY (gig_id, viewer, day)
);
CREATE TABLE IF NOT EXISTS saved_searches (
    id      TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    q       TEXT NOT NULL DEFAULT '',
    cat     TEXT NOT NULL DEFAULT '',
    sub     TEXT NOT NULL DEFAULT '',
    max     INTEGER NOT NULL DEFAULT 100000,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS saved_searches_user ON saved_searches(user_id);
CREATE TABLE IF NOT EXISTS portfolio (
    id      TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gig_id  TEXT,
    name    TEXT NOT NULL,
    title   TEXT NOT NULL DEFAULT '',
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS portfolio_user ON portfolio(user_id, ts);
CREATE TABLE IF NOT EXISTS requests (
    id       TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cat      TEXT NOT NULL,
    sub      TEXT NOT NULL,
    title    TEXT NOT NULL,
    descr    TEXT NOT NULL,
    budget   INTEGER NOT NULL,
    days     INTEGER NOT NULL,
    status   TEXT NOT NULL,
    order_id TEXT,
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status, ts);
CREATE TABLE IF NOT EXISTS offers (
    id         TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    seller_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gig_id     TEXT NOT NULL,
    price      INTEGER NOT NULL,
    days       INTEGER NOT NULL,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    UNIQUE (request_id, seller_id)
);
CREATE TABLE IF NOT EXISTS reports (
    id      TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,
    target  TEXT NOT NULL,
    reason  TEXT NOT NULL,
    text    TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    UNIQUE (user_id, kind, target)
);
CREATE INDEX IF NOT EXISTS reports_status ON reports(status, ts);
CREATE TABLE IF NOT EXISTS share_visits (
    gig_id TEXT NOT NULL,
    ip     TEXT NOT NULL,
    ts     INTEGER NOT NULL,
    PRIMARY KEY (gig_id, ip)
);
CREATE TABLE IF NOT EXISTS ideas (
    id        TEXT PRIMARY KEY,
    user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text      TEXT NOT NULL,
    status    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    status_at INTEGER
);
CREATE INDEX IF NOT EXISTS ideas_status ON ideas(status, ts);
CREATE TABLE IF NOT EXISTS admin_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    staff_id   TEXT NOT NULL,
    staff_name TEXT NOT NULL,
    action     TEXT NOT NULL,
    user_id    TEXT,
    target     TEXT NOT NULL DEFAULT '',
    coins      INTEGER,
    text       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS admin_log_user ON admin_log(user_id, ts);
CREATE TABLE IF NOT EXISTS user_ips (
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ip       TEXT NOT NULL,
    first_ts INTEGER NOT NULL,
    last_ts  INTEGER NOT NULL,
    PRIMARY KEY (user_id, ip)
);
CREATE INDEX IF NOT EXISTS user_ips_ip ON user_ips(ip);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   INTEGER NOT NULL,
    path TEXT NOT NULL,
    msg  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_regs (
    token     TEXT PRIMARY KEY,
    ts        INTEGER NOT NULL,
    chat_id   INTEGER,
    phone     TEXT,
    code      TEXT,
    code_ts   INTEGER,
    tries     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_codes (
    ticket  TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code    TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    tries   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tg_support (
    admin_msg   INTEGER PRIMARY KEY,
    client_chat INTEGER NOT NULL,
    ts          INTEGER NOT NULL
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
        conn.execute("CREATE INDEX IF NOT EXISTS orders_seller_status ON orders(seller_id, status)")
        # Миграция: время подтверждения получения результата покупателем.
        if "confirmed_at" not in ocols:
            conn.execute("ALTER TABLE orders ADD COLUMN confirmed_at INTEGER")
        # Миграция: когда заказ перешёл «На проверке» — от этого времени считается автоподтверждение.
        if "reviewed_at" not in ocols:
            conn.execute("ALTER TABLE orders ADD COLUMN reviewed_at INTEGER")
            conn.execute("UPDATE orders SET reviewed_at = ? WHERE status = 'На проверке'", (now_ms(),))
        # Миграция: баланс платформы начинается с комиссий уже одобренных выводов.
        if not conn.execute("SELECT 1 FROM platform_ops LIMIT 1").fetchone():
            conn.execute(
                "INSERT INTO platform_ops(kind, coins, user_id, note, ts) SELECT 'wd_fee', fee, user_id, dest, ts"
                " FROM wallet_ops WHERE t = 'wd' AND status = 'done' AND fee > 0"
            )
        # Миграция: название тарифа хранится в заказе. Старые заказы — по прежней тройке тарифов.
        if "tier_name" not in {r[1] for r in conn.execute("PRAGMA table_info(orders)")}:
            conn.execute("ALTER TABLE orders ADD COLUMN tier_name TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE orders SET tier_name = CASE tier WHEN 0 THEN 'Эконом' WHEN 1 THEN 'Стандарт'"
                         " WHEN 2 THEN 'Бизнес' ELSE '' END")
        # Миграция: спор по заказу и напоминание покупателю за сутки до автоподтверждения.
        ocols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        if "dispute_at" not in ocols:
            conn.execute("ALTER TABLE orders ADD COLUMN dispute_at INTEGER")
            conn.execute("ALTER TABLE orders ADD COLUMN dispute_reason TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE orders ADD COLUMN dispute_result TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE orders ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0")
        # Миграция: «О себе» для публичного профиля продавца.
        if "about" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN about TEXT NOT NULL DEFAULT ''")
        # Миграция: обложка объявления (имя файла в UPLOAD_DIR/covers).
        if "cover" not in {r[1] for r in conn.execute("PRAGMA table_info(gigs)")}:
            conn.execute("ALTER TABLE gigs ADD COLUMN cover TEXT")
        # Миграция: фото профиля (имя файла в UPLOAD_DIR/avatars); в gigs — копия для карточек, как и имя продавца.
        if "avatar" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
        if "seller_avatar" not in {r[1] for r in conn.execute("PRAGMA table_info(gigs)")}:
            conn.execute("ALTER TABLE gigs ADD COLUMN seller_avatar TEXT")
        # Миграция: привязка Telegram (chat_id) и одноразовый код для /start.
        if "tg_chat" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN tg_chat INTEGER")
            conn.execute("ALTER TABLE users ADD COLUMN tg_code TEXT")
        # Миграция: поднятие объявления (раз в сутки бесплатно) и платное продвижение до promo_until.
        gcols = {r[1] for r in conn.execute("PRAGMA table_info(gigs)")}
        if "bumped_at" not in gcols:
            conn.execute("ALTER TABLE gigs ADD COLUMN bumped_at INTEGER")
            conn.execute("ALTER TABLE gigs ADD COLUMN promo_until INTEGER")
        # Миграция: значок «Проверенный продавец», который ставит администратор.
        if "verified" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        # Миграция: баланс делится на аванс (пополнения, возвращаются только на исходный платёж) и заработок
        # (продажи и компенсации, его можно вывести). Старым балансам заработок — доход минус выводы, не больше баланса.
        if "earned" not in {r[1] for r in conn.execute("PRAGMA table_info(wallets)")}:
            conn.execute("ALTER TABLE wallets ADD COLUMN earned INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "UPDATE wallets SET earned = max(0, min(coins, coalesce((SELECT sum(CASE WHEN t IN ('sale', 'comp')"
                " THEN coins ELSE -coins END) FROM wallet_ops o WHERE o.user_id = wallets.user_id"
                " AND (t IN ('sale', 'comp') AND status = 'done' OR t = 'wd' AND status IN ('pending', 'done'))), 0)))"
            )
        # Миграция: сколько заказа оплачено из аванса — эта часть при отмене возвращается в аванс, остальное в заработок.
        if "paid_adv" not in {r[1] for r in conn.execute("PRAGMA table_info(orders)")}:
            conn.execute("ALTER TABLE orders ADD COLUMN paid_adv INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE orders SET paid_adv = amount WHERE method = 'bal'")
        # Миграция: возврат на карту (t = 'rf') ссылается на пополнение, к платежу которого он делается.
        if "src" not in {r[1] for r in conn.execute("PRAGMA table_info(wallet_ops)")}:
            conn.execute("ALTER TABLE wallet_ops ADD COLUMN src TEXT")
        # Миграция: срок и правки заказа (по тарифу на момент покупки или из отклика на бирже), продление срока
        # по согласию покупателя и время первой сдачи результата (по нему считается «сдано в срок»).
        ocols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        if "days" not in ocols:
            conn.execute("ALTER TABLE orders ADD COLUMN days INTEGER NOT NULL DEFAULT 5")
            conn.execute("ALTER TABLE orders ADD COLUMN revs INTEGER NOT NULL DEFAULT 3")
            conn.execute("ALTER TABLE orders ADD COLUMN revs_used INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE orders ADD COLUMN extra_days INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE orders ADD COLUMN ext_days INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE orders ADD COLUMN ext_reason TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE orders ADD COLUMN delivered_at INTEGER")
            conn.execute("UPDATE orders SET days = 3 WHERE tier = 1")
            conn.execute("UPDATE orders SET delivered_at = reviewed_at WHERE reviewed_at IS NOT NULL")
        # Миграция: режим «В отпуске» — до какого времени продавец не принимает заказы.
        if "vacation_until" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN vacation_until INTEGER")
            conn.execute("ALTER TABLE users ADD COLUMN vacation_note TEXT NOT NULL DEFAULT ''")
        # Миграция: сброс пароля кнопкой из Telegram (sha256 одноразового токена).
        if "token" not in {r[1] for r in conn.execute("PRAGMA table_info(password_resets)")}:
            conn.execute("ALTER TABLE password_resets ADD COLUMN token TEXT")
        # Миграция: напоминания продавцу о сроке (1 — «остались сутки», 2 — «срок прошёл»).
        if "dl_reminded" not in {r[1] for r in conn.execute("PRAGMA table_info(orders)")}:
            conn.execute("ALTER TABLE orders ADD COLUMN dl_reminded INTEGER NOT NULL DEFAULT 0")
        # Миграция: роль сотрудника (moder), временная блокировка до blocked_until.
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "role" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE users ADD COLUMN blocked_until INTEGER")
        # Миграция: полный телефон вывода (для администратора и проверки совпадений между аккаунтами).
        if "phone" not in {r[1] for r in conn.execute("PRAGMA table_info(wallet_ops)")}:
            conn.execute("ALTER TABLE wallet_ops ADD COLUMN phone TEXT")
        # Миграция: модерация объявлений — ok (в каталоге), pending (ждёт проверки), hidden (скрыто с причиной).
        if "mod" not in {r[1] for r in conn.execute("PRAGMA table_info(gigs)")}:
            conn.execute("ALTER TABLE gigs ADD COLUMN mod TEXT NOT NULL DEFAULT 'ok'")
            conn.execute("ALTER TABLE gigs ADD COLUMN mod_note TEXT NOT NULL DEFAULT ''")
        # Миграция: второй фактор входа в админку (код из Telegram) — на уровне сессии.
        if "mfa_at" not in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}:
            conn.execute("ALTER TABLE sessions ADD COLUMN mfa_at INTEGER")
            conn.execute("ALTER TABLE sessions ADD COLUMN mfa_code TEXT")
            conn.execute("ALTER TABLE sessions ADD COLUMN mfa_sent INTEGER")
            conn.execute("ALTER TABLE sessions ADD COLUMN mfa_tries INTEGER NOT NULL DEFAULT 0")
        # Миграция: телефон из Telegram — регистрация через бота вместо email, вход по телефону.
        if "phone" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_phone ON users(phone) WHERE phone IS NOT NULL")
        # Миграция: удаление аккаунта владельцем (обезличивание) и одноразовый код подтверждения из Telegram.
        if "deleted_at" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN deleted_at INTEGER")
            conn.execute("ALTER TABLE users ADD COLUMN del_code TEXT")
            conn.execute("ALTER TABLE users ADD COLUMN del_code_ts INTEGER")
            conn.execute("ALTER TABLE users ADD COLUMN del_tries INTEGER NOT NULL DEFAULT 0")
        # Миграция: рост площадки. Приветственный бонус (отдельно от монет: тратится только на заказы, не выводится
        # и не возвращается на карту), реферальная ссылка, проверенный опыт на других площадках,
        # бесплатный день продвижения за переходы по ссылке «Поделиться».
        if "bonus" not in {r[1] for r in conn.execute("PRAGMA table_info(wallets)")}:
            conn.execute("ALTER TABLE wallets ADD COLUMN bonus INTEGER NOT NULL DEFAULT 0")
        if "paid_bonus" not in {r[1] for r in conn.execute("PRAGMA table_info(orders)")}:
            conn.execute("ALTER TABLE orders ADD COLUMN paid_bonus INTEGER NOT NULL DEFAULT 0")
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "ref_by" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN ref_by TEXT")
        if "ext_status" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN ext_url TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE users ADD COLUMN ext_site TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE users ADD COLUMN ext_rating REAL")
            conn.execute("ALTER TABLE users ADD COLUMN ext_reviews INTEGER")
            conn.execute("ALTER TABLE users ADD COLUMN ext_status TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE users ADD COLUMN ext_note TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE users ADD COLUMN ext_at INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS users_ref ON users(ref_by)")
        if "share_reward_at" not in {r[1] for r in conn.execute("PRAGMA table_info(gigs)")}:
            conn.execute("ALTER TABLE gigs ADD COLUMN share_reward_at INTEGER")
        for sub in ("covers", "results", "avatars", "reviews", "portfolio"):
            (UPLOAD_DIR / sub).mkdir(parents=True, exist_ok=True)
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
