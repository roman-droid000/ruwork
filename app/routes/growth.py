"""Привлечение продавцов и покупателей.

- Опыт на других площадках: продавец присылает ссылку на свой профиль (Kwork, FL.ru, Профи.ру…), администратор
  сверяет её во вкладке «Площадки» и одобряет — продавец получает значок «Проверенный продавец», а в публичном
  профиле появляется блок «Опыт на других площадках» с рейтингом и числом отзывов.
- «Поделиться» с бонусом: ссылка на своё объявление несёт ?ref=<id продавца>. Когда по ней придут SHARE_VISITS
  разных посетителей, объявление бесплатно продвигается сутки (не чаще раза в SHARE_COOLDOWN_MS).
Сниженная комиссия в первые 3 недели, реферальная доля и приветственный бонус — в app/logic.py и app/routes/account.py.
"""

import hashlib
import re
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, guard
from ..db import get_db, now_ms, tx
from ..logic import SHARE_PROMO_MS, SHARE_COOLDOWN_MS, SHARE_VISITS, WELCOME_BONUS, audit, esc, notify
from .share import plain, share_page

router = APIRouter(prefix="/api")
pub = APIRouter()  # страницы вне /api: ссылка-приглашение /r/<код>

REF_COOKIE = "ruwork_ref"
REF_COOKIE_DAYS = 30


@pub.get("/r/{code}", include_in_schema=False)
def invite(code: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Ссылка-приглашение. Запоминает пригласившего в cookie на 30 дней: кто зарегистрируется с этого браузера,
    закрепляется за автором ссылки. Мессенджеры видят превью «<имя> приглашает вас на RUWORK»."""
    u = conn.execute("SELECT id, name FROM users WHERE ref_code = ? AND blocked_at IS NULL AND deleted_at IS NULL",
                     (code.upper()[:12],)).fetchone()
    if not u:
        return share_page(request, "Маркетплейс фриланс-услуг", "Безопасная сделка: продавец получает оплату"
                          " только после вашего подтверждения.", "/og/site.jpg", "/")
    resp = share_page(request, f"{plain(u['name'])} приглашает вас на RUWORK",
                      f"Зарегистрируйтесь по приглашению и получите {WELCOME_BONUS} ₽ на первый заказ. "
                      "Продавцам — неделя без комиссии.", "/og/site.jpg", f"/?inv={code.upper()}#/dashboard")
    resp.headers["Cache-Control"] = "no-store"
    resp.set_cookie(REF_COOKIE, u["id"], max_age=REF_COOKIE_DAYS * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")
    return resp

EXT_SITES = {
    "kwork.ru": "Kwork", "fl.ru": "FL.ru", "profi.ru": "Профи.ру", "youdo.com": "YouDo",
    "freelance.habr.com": "Хабр Фриланс", "freelance.ru": "Freelance.ru", "weblancer.net": "Weblancer",
    "workzilla.com": "Workzilla", "avito.ru": "Авито Услуги", "behance.net": "Behance", "dribbble.com": "Dribbble",
}
EXT_RATE = (5, 3600)
VISIT_RATE = (30, 3600)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def ext_site(url: str) -> str | None:
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not p.path.strip("/"):
        return None
    return next((name for dom, name in EXT_SITES.items() if host == dom or host.endswith("." + dom)), None)


class ExtIn(BaseModel):
    url: str
    rating: float | None = None
    reviews: int | None = None


@router.post("/me/ext")
def ext_submit(body: ExtIn, user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    url = body.url.strip()[:300]
    site = ext_site(url)
    if not site:
        raise HTTPException(400, "Вставьте ссылку на ваш профиль на одной из площадок: " + ", ".join(EXT_SITES.values()))
    if body.rating is not None and not 0 <= body.rating <= 5:
        raise HTTPException(400, "Рейтинг — число от 0 до 5")
    if body.reviews is not None and not 0 <= body.reviews <= 100_000:
        raise HTTPException(400, "Проверьте число отзывов")
    if user["ext_status"] == "ok" and user["ext_url"] == url:
        raise HTTPException(400, "Этот профиль уже подтверждён")
    guard.throttle("ext:" + user["id"], EXT_RATE, "Слишком много заявок — попробуйте позже")
    conn.execute("UPDATE users SET ext_url = ?, ext_site = ?, ext_rating = ?, ext_reviews = ?, ext_status = 'pending',"
                 " ext_note = '', ext_at = ? WHERE id = ?",
                 (url, site, round(body.rating, 2) if body.rating is not None else None, body.reviews, now_ms(), user["id"]))
    guard.alert(f"Проверка опыта: {user['name']} — {site} {url}", "#/admin/ext")
    return {"ok": True}


@router.delete("/me/ext")
def ext_remove(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    conn.execute("UPDATE users SET ext_url = '', ext_site = '', ext_rating = NULL, ext_reviews = NULL, ext_status = '',"
                 " ext_note = '', ext_at = NULL WHERE id = ?", (user["id"],))
    return {"ok": True}


EXT_SQL = ("SELECT id AS user_id, name, verified, ext_url, ext_site, ext_rating, ext_reviews, ext_status, ext_note, ext_at"
           " FROM users WHERE ext_status != '' AND deleted_at IS NULL")


@router.get("/admin/ext", dependencies=[Depends(auth.require_staff)])
def admin_ext(conn: sqlite3.Connection = Depends(get_db)):
    """Заявки на проверку (сначала старые), затем 100 последних решённых."""
    pend = conn.execute(EXT_SQL + " AND ext_status = 'pending' ORDER BY ext_at").fetchall()
    rest = conn.execute(EXT_SQL + " AND ext_status != 'pending' ORDER BY ext_at DESC LIMIT 100").fetchall()
    return [dict(r) for r in pend + rest]


class ExtDecision(BaseModel):
    reason: str = ""


@router.post("/admin/ext/{user_id}/{act}")
def admin_ext_decide(user_id: str, act: str, body: ExtDecision, staff=Depends(auth.require_staff),
                     conn: sqlite3.Connection = Depends(get_db)):
    if act not in ("approve", "reject"):
        raise HTTPException(400, "Неизвестное действие")
    u = conn.execute("SELECT ext_site, ext_status FROM users WHERE id = ?", (user_id,)).fetchone()
    if not u or not u["ext_status"]:
        raise HTTPException(404, "Заявка не найдена")
    reason = esc(body.reason.strip())[:300]
    if act == "reject" and len(reason) < 5:
        raise HTTPException(400, "Напишите причину — её увидит продавец")
    with tx(conn):
        if act == "approve":
            conn.execute("UPDATE users SET ext_status = 'ok', ext_note = '', ext_at = ?, verified = 1 WHERE id = ?",
                         (now_ms(), user_id))
            notify(conn, user_id, "ext", f"Ваш опыт на {u['ext_site']} подтверждён: в профиле появились отзывы с площадки"
                   " и значок «Проверенный продавец».", f"#/seller/{user_id}")
            audit(conn, staff, "ext_ok", f"Подтверждён опыт на {u['ext_site']}", user_id)
        else:
            conn.execute("UPDATE users SET ext_status = 'rejected', ext_note = ?, ext_at = ? WHERE id = ?",
                         (reason, now_ms(), user_id))
            notify(conn, user_id, "ext", f"Опыт на {u['ext_site']} не подтверждён: {reason}", "#/dashboard/profile")
            audit(conn, staff, "ext_no", f"Отклонён опыт на {u['ext_site']}: {reason}", user_id)
    return admin_ext(conn)


# ---------- «Поделиться» с бонусом ----------

class VisitIn(BaseModel):
    gig: str
    ref: str


@router.post("/share/visit")
def share_visit(body: VisitIn, request: Request, user=Depends(auth.optional_user),
                conn: sqlite3.Connection = Depends(get_db)):
    """Переход по ссылке продавца на его объявление. Свои заходы (тот же аккаунт или адрес, с которого входил
    продавец) не считаются; каждый адрес — один раз на объявление."""
    if not ID_RE.match(body.gig) or not ID_RE.match(body.ref) or (user and user["id"] == body.ref):
        return {"ok": True}
    ip = guard.client_ip(request)
    if guard.limited("visit:" + ip, VISIT_RATE):
        return {"ok": True}
    guard.hit("visit:" + ip)
    with tx(conn):
        g = conn.execute("SELECT id, title, owner_id, promo_until, share_reward_at FROM gigs WHERE id = ? AND owner_id = ?"
                         " AND mod = 'ok'", (body.gig, body.ref)).fetchone()
        if not g or conn.execute("SELECT 1 FROM user_ips WHERE user_id = ? AND ip = ?", (g["owner_id"], ip)).fetchone():
            return {"ok": True}
        n = now_ms()
        conn.execute("INSERT OR IGNORE INTO share_visits(gig_id, ip, ts) VALUES (?, ?, ?)",
                     (g["id"], hashlib.sha256(ip.encode()).hexdigest()[:20], n))
        since = g["share_reward_at"] or 0
        if n - since < SHARE_COOLDOWN_MS:
            return {"ok": True}
        cnt = conn.execute("SELECT count(*) FROM share_visits WHERE gig_id = ? AND ts > ?", (g["id"], since)).fetchone()[0]
        if cnt >= SHARE_VISITS:
            conn.execute("UPDATE gigs SET promo_until = ?, bumped_at = ?, share_reward_at = ? WHERE id = ?",
                         (max(n, g["promo_until"] or 0) + SHARE_PROMO_MS, n, n, g["id"]))
            notify(conn, g["owner_id"], "promo", f"По вашей ссылке пришли {cnt} человек — объявление «{g['title']}» бесплатно"
                   " продвигается сутки.", f"#/gig/{g['id']}")
    return {"ok": True}


@router.get("/me/share")
def share_progress(user=Depends(auth.require_user), conn: sqlite3.Connection = Depends(get_db)):
    """По каждому своему объявлению: сколько переходов уже набрано и когда снова можно получить продвижение."""
    n, out = now_ms(), {}
    for g in conn.execute("SELECT id, share_reward_at FROM gigs WHERE owner_id = ?", (user["id"],)):
        since = g["share_reward_at"] or 0
        wait = since + SHARE_COOLDOWN_MS if since and n - since < SHARE_COOLDOWN_MS else None
        cnt = 0 if wait else conn.execute("SELECT count(*) FROM share_visits WHERE gig_id = ? AND ts > ?",
                                          (g["id"], since)).fetchone()[0]
        out[g["id"]] = {"n": cnt, "need": SHARE_VISITS, "next": wait}
    return out
