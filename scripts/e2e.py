"""Сквозной прогон RUWORK перед выкладкой.

Запускает отдельный сервер на временной базе (рабочая ruwork.db не трогается) и проходит путь клиента
и продавца через API: регистрация → объявление с обложкой → чат → пополнение → заказ → результат →
доработка → подтверждение → отзыв → вывод; спор; жалоба; защита входа; бэкап и /api/health.
Таймеры (минута до «В работе», срок сдачи) перематываются прямо во временной базе.

Запуск из корня проекта:  .venv/bin/python scripts/e2e.py
Код выхода 0 — всё прошло, 1 — есть ошибки (список в конце).
"""

import http.cookiejar
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ADMIN_EMAIL, ADMIN_PASSWORD = "phantom.crypta@mail.ru", "Admin-e2e-12345"
failures: list[str] = []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """Отдельная сессия (cookie) — как отдельный браузер."""

    def __init__(self, base: str, ip: str):
        self.base, self.ip = base, ip
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body=None, raw: bytes | None = None, ctype: str = "") -> tuple[int, object]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"X-Real-IP": self.ip}
        if data is not None:
            headers["Content-Type"] = ctype or "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as r:
                code, text = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, text = e.code, e.read()
        try:
            return code, json.loads(text)
        except ValueError:
            return code, text.decode(errors="replace")

    def ok(self, method: str, path: str, body=None, **kw):
        code, data = self.call(method, path, body, **kw)
        if code != 200:
            raise AssertionError(f"{method} {path} → {code}: {data}")
        return data


def check(name: str, cond: bool, info: object = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f" — {info}"))
    if not cond:
        failures.append(name)


def step(title: str) -> None:
    print("\n" + title)


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (640, 400), (90, 140, 230)).save(buf, "PNG")
    return buf.getvalue()


def start(tmp: Path, db: Path, log: str, **extra) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = {**os.environ, "RUWORK_DB": str(db), "RUWORK_UPLOADS": str(tmp / "uploads"),
           "RUWORK_BACKUPS": str(tmp / "backups"), "RUWORK_ADMIN_PASSWORD": ADMIN_PASSWORD,
           "RUWORK_TG_TOKEN": "", "ANTHROPIC_API_KEY": "", "RUWORK_JOBS_PERIOD": "2", **extra}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=ROOT, env=env, stdout=open(tmp / log, "w"), stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    return server, base


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ruwork-e2e-"))
    db = tmp / "e2e.db"
    for log, extra, phase in (("server.log", {}, run), ("server-mfa.log", {"RUWORK_TG_TOKEN": "0:e2e-fake"}, run_mfa)):
        server, base = start(tmp, db, log, **extra)
        try:
            phase(base, db, tmp)
        except Exception as e:
            failures.append(f"прогон прерван: {e}")
            print(f"\n  ✗ Прогон прерван: {e}")
        finally:
            server.terminate()
            server.wait(10)
    print(f"\nЖурналы сервера: {tmp}")
    if failures:
        print(f"\nОШИБКИ ({len(failures)}):\n- " + "\n- ".join(failures))
        return 1
    print("\nВсё прошло успешно.")
    return 0


def run(base: str, db: Path, tmp: Path) -> None:
    def sql(q: str, *a):
        c = sqlite3.connect(db)
        try:
            c.execute(q, a)
            c.commit()
        finally:
            c.close()

    sfx = str(int(time.time()))[-6:]
    seller, buyer, admin = Client(base, "10.0.0.1"), Client(base, "10.0.0.2"), Client(base, "10.0.0.3")

    step("1. Сервер и каталог")
    h = seller.ok("GET", "/api/health")
    check("/api/health отвечает ok", h.get("ok") is True, h)
    check("резервная копия создана при старте", any((tmp / "backups").glob("ruwork-*.db.gz")))
    check("демо-каталог скрыт", seller.ok("GET", "/api/gigs") == [])

    step("2. Регистрация и вход")
    pw = "Passw0rd-e2e"
    for c, name, email in ((seller, "Продавец" + sfx, f"s{sfx}@e2e.test"), (buyer, "Покупатель" + sfx, f"b{sfx}@e2e.test")):
        c.ok("POST", "/api/auth/register", {"name": name, "email": email, "password": pw, "password2": pw, "terms": True})
    me_s = seller.ok("GET", "/api/state")["me"]
    me_b = buyer.ok("GET", "/api/state")["me"]
    check("оба аккаунта созданы", bool(me_s and me_b))
    admin.ok("POST", "/api/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    check("администратор вошёл", admin.ok("GET", "/api/state")["me"].get("admin") is True)

    step("3. Объявление")
    meta = seller.ok("GET", "/api/meta")
    cat = meta["cats"][0][0]
    sub = meta["subs"][cat][0]
    price = max(1000, meta["minp"].get(sub, 0))
    code, err = seller.call("POST", "/api/gigs", {"cat": cat, "sub": sub, "title": "Купить диплом быстро",
                                                  "desc": "Сделаю диплом любого вуза", "base": price})
    check("запрещённая услуга отклонена", code == 400 and "запрещены" in str(err), (code, err))
    gig = seller.ok("POST", "/api/gigs", {
        "cat": cat, "sub": sub, "title": "Тестовая услуга e2e: логотип за 2 дня", "base": price,
        "desc": "Сделаю аккуратно. Пишите в телеграм @seller_e2e или на почту s@e2e.test, тел. +7 900 123-45-67",
        "tags": "логотип, дизайн, брендинг"})
    check("контакты в описании скрыты", "@seller_e2e" not in gig["desc"] and "900" not in gig["desc"]
          and "[контакт скрыт]" in gig["desc"], gig["desc"])
    seller.ok("POST", f"/api/gigs/{gig['id']}/cover", raw=png(), ctype="image/png")
    check("объявление в каталоге", any(g["id"] == gig["id"] for g in buyer.ok("GET", "/api/gigs")))

    step("4. Чат")
    buyer.ok("POST", f"/api/chat/{gig['id']}", {"text": "Здравствуйте! Напишите мне на buyer@mail.ru или в t.me/buyer_e2e"})
    th = seller.ok("GET", f"/api/chat/{gig['id']}")["threads"]
    last = th[0]["msgs"][-1]["t"] if th else ""
    check("продавец видит сообщение с замаскированными контактами",
          "buyer@mail.ru" not in last and "t.me" not in last and "[контакт скрыт]" in last, last)
    seller.ok("POST", f"/api/chat/{gig['id']}/reply", {"to": me_b["id"], "text": "Добрый день, всё обсудим здесь."})

    step("5. Пополнение баланса покупателя")
    buyer.ok("POST", "/api/wallet/deposit", {"amount": price * 4})
    dep = next(o for o in admin.ok("GET", "/api/admin/ops") if o["status"] == "pending" and o["t"] == "dep")
    admin.ok("POST", f"/api/admin/ops/{dep['id']}/approve")
    check("баланс пополнен", buyer.ok("GET", "/api/state")["wallet"]["coins"] == price * 4)

    step("6. Заказ → результат → доработка → подтверждение")
    buyer.ok("POST", "/api/orders", {"items": [{"id": gig["id"], "tier": 0}], "method": "bal"})
    oid = buyer.ok("GET", "/api/state")["orders"][0][5]
    sql("UPDATE orders SET created_at = created_at - 61000 WHERE id = ?", oid)
    check("заказ перешёл «В работе»", buyer.ok("GET", f"/api/orders/{oid}")["status"] == "В работе")

    def deliver(name: bytes):
        f = seller.ok("POST", f"/api/orders/{oid}/files", {"name": "result.txt", "size": len(name), "mime": "text/plain"})
        r = seller.ok("PUT", f"/api/orders/{oid}/files/{f['id']}?offset=0", raw=name, ctype="application/octet-stream")
        return r["done"]

    check("результат загружен", deliver(b"result v1"))
    check("заказ «На проверке»", buyer.ok("GET", f"/api/orders/{oid}")["status"] == "На проверке")
    buyer.ok("POST", f"/api/orders/{oid}/revision", {"text": "Поправьте, пожалуйста, цвет"})
    check("доработка вернула заказ «В работе»", buyer.ok("GET", f"/api/orders/{oid}")["status"] == "В работе")
    deliver(b"result v2")
    buyer.ok("POST", f"/api/orders/{oid}/confirm")
    check("заказ выполнен", buyer.ok("GET", f"/api/orders/{oid}")["status"] == "Выполнен")
    w = seller.ok("GET", "/api/state")["wallet"]
    net = price - -(-price * meta["sale_fee_pct"] // 100)
    check(f"продавцу зачислено {net} (минус комиссия)", w["earned"] == net, w)
    buyer.ok("POST", f"/api/orders/{oid}/review", {"stars": 5, "text": "Всё отлично, спасибо!"})
    check("отзыв опубликован", len(buyer.ok("GET", f"/api/gigs/{gig['id']}/reviews")) == 1)

    step("7. Вывод заработка")
    seller.ok("POST", "/api/wallet/withdraw", {"amount": 500, "phone": "+79001234567", "bank": "Сбербанк", "password": pw})
    wd = next(o for o in admin.ok("GET", "/api/admin/ops") if o["status"] == "pending" and o["t"] == "wd")
    admin.ok("POST", f"/api/admin/ops/{wd['id']}/approve")
    check("вывод одобрен", seller.ok("GET", "/api/state")["wallet"]["earned"] == net - 500)

    step("8. Спор и решение администратора")
    buyer.ok("POST", "/api/orders", {"items": [{"id": gig["id"], "tier": 0}], "method": "bal"})
    oid2 = buyer.ok("GET", "/api/state")["orders"][0][5]
    sql("UPDATE orders SET created_at = created_at - 61000 WHERE id = ?", oid2)
    buyer.ok("GET", "/api/state")
    buyer.ok("POST", f"/api/orders/{oid2}/dispute", {"reason": "Продавец не отвечает уже три дня"})
    check("спор открыт", buyer.ok("GET", f"/api/orders/{oid2}")["status"] == "Спор")
    coins_before = buyer.ok("GET", "/api/state")["wallet"]["coins"]
    admin.ok("POST", f"/api/admin/disputes/{oid2}/buyer")
    check("деньги вернулись покупателю", buyer.ok("GET", "/api/state")["wallet"]["coins"] == coins_before + price)

    step("9. Сроки и напоминания (фоновые задачи)")
    buyer.ok("POST", "/api/orders", {"items": [{"id": gig["id"], "tier": 0}], "method": "bal"})
    oid3 = buyer.ok("GET", "/api/state")["orders"][0][5]
    sql("UPDATE orders SET created_at = created_at - 61000 WHERE id = ?", oid3)
    buyer.ok("GET", "/api/state")
    sql("UPDATE orders SET started_at = started_at - 6 * 86400000 WHERE id = ?", oid3)
    deadline_seen = False
    for _ in range(30):  # фоновый цикл в прогоне — раз в 2 секунды (RUWORK_JOBS_PERIOD)
        notes = seller.ok("GET", "/api/notifications")["items"]
        if any("прошёл" in n["text"] for n in notes):
            deadline_seen = True
            break
        time.sleep(1)
    check("продавец получил напоминание о просроченном сроке", deadline_seen)

    step("10. Жалоба")
    buyer.ok("POST", "/api/reports", {"kind": "gig", "target": gig["id"], "reason": "Мошенничество или обман",
                                      "text": "Просит оплату на карту"})
    reps = admin.ok("GET", "/api/admin/reports")
    check("жалоба видна администратору", any(r["target"] == gig["id"] and r["status"] == "open" for r in reps), reps)
    admin.ok("POST", f"/api/admin/reports/{reps[0]['id']}/close")
    check("жалоба закрыта", admin.ok("GET", "/api/admin/reports")[0]["status"] == "closed")
    code, _ = seller.call("POST", "/api/reports", {"kind": "gig", "target": gig["id"], "reason": "Другое", "text": "на себя"})
    check("на своё объявление пожаловаться нельзя", code == 400)

    step("11. Защита входа")
    attacker = Client(base, "10.0.0.99")
    codes = [attacker.call("POST", "/api/auth/login", {"email": me_s["email"], "password": f"wrong{i}x1"})[0]
             for i in range(11)]
    check("после 10 неверных паролей — 429", codes[:10] == [400] * 10 and codes[10] == 429, codes)
    code, _ = Client(base, "10.0.0.98").call("POST", "/api/auth/login", {"email": me_s["email"], "password": pw})
    check("перебор блокирует вход в аккаунт и с другого адреса", code == 429, code)
    code, _ = buyer.call("POST", "/api/auth/reset", {"email": me_b["email"], "password": "NewPassw0rd", "password2": "NewPassw0rd",
                                                     "contact": "@buyer"})
    check("заявка на сброс пароля принята", code == 200, code)
    check("заявка видна администратору", any(r["status"] == "pending" for r in admin.ok("GET", "/api/admin/resets")))

    step("12. Страницы")
    for path in ("/", "/manifest.json", f"/g/{gig['id']}", f"/s/{me_s['id']}"):
        code, _ = buyer.call("GET", path)
        check(f"GET {path} → 200", code == 200, code)

    # ---------- Админ-панель ----------
    step("13. Журнал, сверка и корректировка баланса")
    log = admin.ok("GET", "/api/admin/log")
    acts = {x["action"] for x in log}
    check("журнал пишет одобрения, споры и жалобы", {"op_approve", "dispute", "report_close"} <= acts, acts)
    rec = admin.ok("GET", "/api/admin/reconcile")
    check("сверка: балансы совпадают с операциями", rec["bad"] == [], rec["bad"])
    before = seller.ok("GET", "/api/state")["wallet"]
    admin.ok("POST", f"/api/admin/users/{me_s['id']}/adjust", {"coins": 100, "reason": "Компенсация e2e", "part": "earned"})
    after = seller.ok("GET", "/api/state")["wallet"]
    check("начисление попало в заработок и в операции", after["earned"] == before["earned"] + 100
          and after["ops"][0]["t"] == "adj_in", after["ops"][:1])
    code, _ = admin.call("POST", f"/api/admin/users/{me_s['id']}/adjust", {"coins": -10**6, "reason": "Списание e2e"})
    check("нельзя списать больше, чем есть", code == 400, code)
    admin.ok("POST", f"/api/admin/users/{me_s['id']}/adjust", {"coins": -40, "reason": "Списание e2e", "part": "earned"})
    check("сверка после корректировок — без расхождений", admin.ok("GET", "/api/admin/reconcile")["bad"] == [])
    sql("UPDATE wallets SET coins = coins + 7 WHERE user_id = ?", me_s["id"])
    bad = admin.ok("GET", "/api/admin/reconcile")["bad"]
    check("ручная правка базы видна в сверке", len(bad) == 1 and bad[0]["diff"] == 7, bad)
    sql("UPDATE wallets SET coins = coins - 7 WHERE user_id = ?", me_s["id"])
    check("корректировка записана в журнал пользователя",
          any(x["action"] == "adjust" for x in admin.ok("GET", f"/api/admin/users/{me_s['id']}")["log"]))

    step("14. Флаги риска и связанные аккаунты")
    twin = Client(base, "10.0.0.1")  # тот же адрес, что у продавца
    twin.ok("POST", "/api/auth/register", {"name": "Двойник" + sfx, "email": f"t{sfx}@e2e.test", "password": pw,
                                           "password2": pw, "terms": True})
    prof = admin.ok("GET", f"/api/admin/users/{me_s['id']}")
    check("связанный аккаунт найден по общему адресу", any(x["name"] == "Двойник" + sfx for x in prof["linked"]), prof["linked"])
    seller.ok("POST", "/api/wallet/withdraw", {"amount": 500, "phone": "+79001234567", "bank": "Сбербанк", "password": pw})
    wd2 = next(o for o in admin.ok("GET", "/api/admin/ops") if o["status"] == "pending" and o["t"] == "wd")
    check("у вывода есть флаги риска и полный телефон", wd2["flags"] and wd2["phone"] == "+79001234567", wd2)
    check("флаг «аккаунт моложе 7 дней»", any("7 дней" in f for f in wd2["flags"]), wd2["flags"])
    admin.ok("POST", f"/api/admin/ops/{wd2['id']}/reject")

    step("15. Сводка, графики, поиск, сообщение, карточка спора, чаты")
    att = admin.ok("GET", "/api/admin/attention")
    check("сводка отвечает", "items" in att and att["users"] >= 3, att)
    st = admin.ok("GET", "/api/admin/stats?days=7")
    check("графики: 7 дней и заказы сегодня", len(st["series"]["orders"]) == 7 and st["series"]["orders"][-1] >= 3, st["series"])
    check("воронка продавца", st["funnel"][1][1] >= 1 and st["funnel"][2][1] >= 1, st["funnel"])
    sr = admin.ok("GET", "/api/admin/search?q=" + urllib.request.quote(me_s["email"]))
    check("поиск по email", any(u["id"] == me_s["id"] for u in sr["users"]))
    num = admin.ok("GET", f"/api/admin/dispute/{oid2}")["order"]["num"]
    check("поиск по номеру заказа", any(o["id"] == oid2 for o in admin.ok("GET", "/api/admin/search?q=" + urllib.request.quote(num))["orders"]))
    check("поиск по телефону вывода", any(u["id"] == me_s["id"] for u in admin.ok("GET", "/api/admin/search?q=9001234567")["users"]))
    admin.ok("POST", f"/api/admin/users/{me_b['id']}/message", {"text": "Проверка связи от администрации"})
    check("сообщение пришло в уведомления", any("Проверка связи" in n["text"] for n in buyer.ok("GET", "/api/notifications")["items"]))
    dd = admin.ok("GET", f"/api/admin/dispute/{oid2}")
    check("карточка спора: стороны, переписка, ход заказа", dd["buyer"] and dd["seller"] and dd["chat"] and dd["deadline"], dd.keys())
    ch = admin.ok("GET", "/api/admin/chats?flagged=1")
    check("чаты: сообщение со скрытым контактом найдено", ch["messages"] and ch["offenders"], ch)

    step("16. Премодерация и скрытие объявлений")
    admin.ok("POST", "/api/admin/settings", {"premod": True})
    g2 = twin.ok("POST", "/api/gigs", {"cat": cat, "sub": sub, "title": "Новое объявление на проверке", "base": price,
                                        "desc": "Описание объявления для проверки модератором", "tags": "a, b, c"})
    check("новое объявление ждёт проверки", g2.get("mod") == "pending", g2)
    check("в каталоге его нет", all(g["id"] != g2["id"] for g in buyer.ok("GET", "/api/gigs")))
    check("автор его видит", any(g["id"] == g2["id"] and g.get("mod") == "pending" for g in twin.ok("GET", "/api/gigs")))
    code, _ = buyer.call("POST", "/api/orders", {"items": [{"id": g2["id"], "tier": 0}], "method": "bal"})
    check("заказать объявление на проверке нельзя", code == 400, code)
    admin.ok("POST", f"/api/admin/gigs/{g2['id']}/approve")
    check("после одобрения — в каталоге", any(g["id"] == g2["id"] for g in buyer.ok("GET", "/api/gigs")))
    admin.ok("POST", f"/api/admin/gigs/{g2['id']}/hide", {"reason": "Нет примеров работ"})
    hid = next(g for g in twin.ok("GET", "/api/gigs") if g["id"] == g2["id"])
    check("скрытое: автор видит причину", hid.get("mod") == "hidden" and hid.get("mod_note") == "Нет примеров работ", hid)
    twin.ok("PUT", f"/api/gigs/{g2['id']}", {"cat": cat, "sub": sub, "title": "Исправленное объявление", "base": price,
                                              "desc": "Добавил примеры работ в портфолио", "tags": "a, b, c"})
    check("после исправления — снова на проверке",
          next(g for g in twin.ok("GET", "/api/gigs") if g["id"] == g2["id"]).get("mod") == "pending")
    admin.ok("POST", "/api/admin/settings", {"premod": False})

    step("17. Временная блокировка и роли")
    me_t = twin.ok("GET", "/api/state")["me"]
    admin.ok("POST", f"/api/admin/users/{me_t['id']}/block", {"reason": "Мультиаккаунт", "days": 1})
    code, err = Client(base, "10.0.0.7").call("POST", "/api/auth/login", {"email": f"t{sfx}@e2e.test", "password": pw})
    check("блокировка на сутки: вход закрыт с датой", code == 403 and " до " in str(err), (code, err))
    sql("UPDATE users SET blocked_until = ? WHERE id = ?", int(time.time() * 1000) - 1000, me_t["id"])
    unblocked = False
    for _ in range(15):
        if Client(base, "10.0.0.8").call("POST", "/api/auth/login", {"email": f"t{sfx}@e2e.test", "password": pw})[0] == 200:
            unblocked = True
            break
        time.sleep(1)
    check("по истечении срока разблокируется сам", unblocked)
    admin.ok("POST", f"/api/admin/users/{me_b['id']}/role", {"role": "moder"})
    check("модератор видит жалобы", buyer.call("GET", "/api/admin/reports")[0] == 200)
    check("модератор не видит деньги", buyer.call("GET", "/api/admin/ops")[0] == 403)
    check("модератор не может корректировать баланс",
          buyer.call("POST", f"/api/admin/users/{me_s['id']}/adjust", {"coins": 5, "reason": "попытка"})[0] == 403)
    check("модератор не может заблокировать владельца",
          buyer.call("POST", "/api/admin/users/u-admin/block", {"days": 0})[0] == 400)
    check("в /api/state у модератора staff=moder", buyer.ok("GET", "/api/state")["me"].get("staff") == "moder")
    check("обычный пользователь в админку не попадает", seller.call("GET", "/api/admin/attention")[0] == 403)


def run_mfa(base: str, db: Path, tmp: Path) -> None:
    """Тот же сервер, но с токеном бота: админ-панель требует код из Telegram (бот ненастоящий, код пишем в базу)."""
    import hashlib

    step("18. Второй фактор входа в админку")
    admin = Client(base, "10.0.0.3")
    admin.ok("POST", "/api/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    code, _ = admin.call("GET", "/api/admin/attention")
    check("без кода — 428", code == 428, code)
    st = admin.ok("GET", "/api/staff/mfa")
    check("Telegram не привязан — состояние сообщает об этом", st["on"] and not st["tg"], st)
    c = sqlite3.connect(db)
    c.execute("UPDATE users SET tg_chat = 1 WHERE email = ?", (ADMIN_EMAIL,))
    c.commit()
    admin.ok("POST", "/api/staff/mfa/send")
    c.execute("UPDATE sessions SET mfa_code = ? WHERE user_id = 'u-admin' AND mfa_sent IS NOT NULL",
              (hashlib.sha256(b"ruwork-mfa:123456").hexdigest(),))
    c.commit()
    code, _ = admin.call("POST", "/api/staff/mfa/verify", {"code": "000000"})
    check("неверный код отклонён", code == 400, code)
    admin.ok("POST", "/api/staff/mfa/verify", {"code": "123456"})
    check("с кодом — админка открыта", admin.call("GET", "/api/admin/attention")[0] == 200)
    check("Telegram сотрудника с сайта не отвязать", admin.call("DELETE", "/api/me/telegram")[0] == 403)
    check("и не перепривязать", admin.call("POST", "/api/me/telegram")[0] == 403)
    other = Client(base, "10.0.0.3")
    other.ok("POST", "/api/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    check("новая сессия с тем же паролем снова требует код", other.call("GET", "/api/admin/users")[0] == 428)
    c.execute("UPDATE users SET tg_chat = NULL WHERE email = ?", (ADMIN_EMAIL,))
    c.commit()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
