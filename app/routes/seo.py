"""Страницы для поисковиков: /uslugi, /uslugi/<категория>, /uslugi/<категория>/<подкатегория>, sitemap.xml, robots.txt.

Сайт — одностраничное приложение с адресами после «#», которые Яндекс и Google не индексируют как отдельные страницы.
Здесь сервер сам собирает обычный HTML: заголовок, описание, список объявлений со ссылками на сайт, ссылки на
соседние подкатегории и призыв разместить задание на бирже, если подходящей услуги нет.
"""

import html
import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from .. import tg
from ..db import get_db
from ..logic import CATS, SUBS, VISIBLE_SQL, rub

router = APIRouter()

SITE = tg.PUBLIC_URL
TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
              ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
               "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slug(text: str) -> str:
    out = "".join(TR.get(c, c) for c in text.lower())
    out = "".join(c if c.isascii() and c.isalnum() else "-" for c in out)
    return "-".join(p for p in out.split("-") if p)


SUB_BY_SLUG = {(cat, slug(sub)): sub for cat, subs in SUBS.items() for sub in subs}
e = lambda t: html.escape(str(t), quote=True)  # noqa: E731


def plain(t: str | None) -> str:
    return html.unescape(t or "").strip()


def gigs_of(conn: sqlite3.Connection, cat: str, sub: str | None) -> list[sqlite3.Row]:
    q = (f"SELECT g.* FROM gigs g LEFT JOIN users u ON u.id = g.owner_id WHERE {VISIBLE_SQL} AND g.cat = ?"
         + (" AND g.sub = ?" if sub else "") + " ORDER BY (g.promo_until > strftime('%s','now') * 1000) DESC,"
         " g.rev DESC, coalesce(g.bumped_at, g.created_at) DESC LIMIT 60")
    return conn.execute(q, (cat, sub) if sub else (cat,)).fetchall()


def counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    rows = conn.execute(f"SELECT g.cat, g.sub, count(*) FROM gigs g LEFT JOIN users u ON u.id = g.owner_id"
                        f" WHERE {VISIBLE_SQL} GROUP BY g.cat, g.sub").fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


CSS = """:root{--bg:#F7F5FD;--card:#fff;--ln:#E4DFF3;--tx:#1E1B2E;--mu:#6B6685;--vi:#7C4DEB;--pr:#0E7FA8;--chip:#EFE9FF}
@media(prefers-color-scheme:dark){:root{--bg:#0B0B12;--card:#15141F;--ln:#2A2740;--tx:#EEEAFB;--mu:#A39FBF;--vi:#A66BFF;--pr:#6FE3FF;--chip:#2A1D4D}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--vi)}main{max-width:1080px;margin:0 auto;padding:20px 16px 60px}header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.logo{font:800 24px/1 system-ui;color:var(--tx);text-decoration:none;letter-spacing:.5px}.crumbs{font-size:14px;color:var(--mu);margin:22px 0 6px}.crumbs a{color:var(--mu)}
h1{font-size:clamp(26px,5vw,38px);line-height:1.15;margin:0 0 10px}h2{font-size:22px;margin:34px 0 12px}.lead{color:var(--mu);max-width:760px;margin:0}
.btn{display:inline-block;padding:11px 18px;border-radius:10px;background:var(--vi);color:#fff;text-decoration:none;font-weight:600}.btn.o{background:var(--card);border:1px solid var(--ln);color:var(--tx)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}.card{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--ln);border-radius:14px;overflow:hidden;color:var(--tx);text-decoration:none}
.card img,.card .ph{width:100%;aspect-ratio:300/130;object-fit:cover;background:linear-gradient(120deg,#3A1F6E,#8B5CF6)}.card .b{padding:12px 14px;display:flex;flex-direction:column;gap:6px;flex:1}
.card b{font-weight:600}.card small{color:var(--mu)}.price{color:var(--pr);font-weight:700;margin-top:auto}.chips{display:flex;flex-wrap:wrap;gap:8px}.chips a{padding:7px 12px;border:1px solid var(--ln);background:var(--card);border-radius:99px;text-decoration:none;color:var(--tx);font-size:14px}
.chips a.on{border-color:var(--vi);background:var(--chip)}.box{color:#fff;background:linear-gradient(120deg,#1D2B6B,#4C3BCF 60%,#8B5CF6);border-radius:16px;padding:22px;margin-top:30px}.box p{margin:6px 0 14px;opacity:.92}
.cols{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}.cols ul{margin:6px 0 0;padding-left:18px}footer{margin-top:40px;color:var(--mu);font-size:14px}"""


def page(title: str, desc: str, path: str, crumbs: list[tuple[str, str]], body: str) -> HTMLResponse:
    ld = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": SITE + u} for i, (n, u) in enumerate(crumbs)]}
    cr = " › ".join(f'<a href="{e(u)}">{e(n)}</a>' for n, u in crumbs[:-1]) + (f" › {e(crumbs[-1][0])}" if crumbs else "")
    out = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><meta name="description" content="{e(desc)}"><link rel="canonical" href="{e(SITE + path)}">
<meta property="og:type" content="website"><meta property="og:site_name" content="RUWORK"><meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}"><meta property="og:url" content="{e(SITE + path)}"><meta property="og:image" content="{e(SITE)}/og/site.jpg">
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><style>{CSS}</style>
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False).replace("<", "\\u003c")}</script></head><body><main>
<header><a class="logo" href="/">RUWORK</a><nav><a class="btn o" href="/#/catalog">Каталог услуг</a></nav></header>
<div class="crumbs">{cr}</div>{body}
<div class="box"><b style="font-size:20px">Не нашли подходящую услугу?</b><p>Опишите задачу на бирже заданий — продавцы сами предложат цену и срок. Оплата через безопасную сделку: деньги продавец получит только после вашего подтверждения.</p><a class="btn" style="background:#fff;color:#1E1B2E" href="/?req=1#/requests">Разместить задание</a></div>
<div class="box" style="background:linear-gradient(120deg,#123F2E,#1F7A5A 55%,#4CC38A)"><b style="font-size:20px">Вы фрилансер?</b><p>Разместите услугу на RUWORK за 5 минут. Первая неделя — без комиссии с продаж.</p><a class="btn" style="background:#fff;color:#1E1B2E" href="/#/dashboard/gigs">Стать продавцом</a></div>
<footer><a href="/uslugi">Все услуги</a> · <a href="/#/help">Как проходит сделка</a> · <a href="/#/support">Поддержка</a> · RUWORK — маркетплейс фриланс-услуг</footer>
</main></body></html>"""
    return HTMLResponse(out, headers={"Cache-Control": "public, max-age=600"})


def cards(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return '<p class="lead">Здесь пока нет готовых объявлений. Разместите задание — продавцы откликнутся сами.</p>'
    out = []
    for g in rows:
        img = (f'<img src="/media/thumb/covers/{e(g["cover"])}?w=480" alt="{e(plain(g["title"]))}" loading="lazy">'
               if g["cover"] else '<div class="ph"></div>')
        rate = f' · ★ {round(g["rating"], 1)} ({g["rev"]})' if g["rev"] else ""
        out.append(f'<a class="card" href="/#/gig/{e(g["id"])}">{img}<div class="b"><b>{e(plain(g["title"]))}</b>'
                   f'<small>{e(plain(g["seller"]))}{rate}</small><span class="price">от {e(rub(g["base"]))}</span></div></a>')
    return f'<div class="grid">{"".join(out)}</div>'


def chips(cat: str, cur: str | None, cnt: dict) -> str:
    return '<div class="chips">' + "".join(
        f'<a class="{"on" if s == cur else ""}" href="/uslugi/{cat}/{slug(s)}">{e(s)} · {cnt.get((cat, s), 0)}</a>'
        for s in SUBS.get(cat, [])) + "</div>"


@router.get("/uslugi", include_in_schema=False)
def all_services(conn: sqlite3.Connection = Depends(get_db)):
    cnt = counts(conn)
    cols = "".join(
        f'<div><a href="/uslugi/{cid}"><b>{e(c[1])}</b></a> <small style="color:var(--mu)">'
        f'{sum(cnt.get((cid, s), 0) for s in SUBS.get(cid, []))}</small><ul>'
        + "".join(f'<li><a href="/uslugi/{cid}/{slug(s)}">{e(s)}</a></li>' for s in SUBS.get(cid, [])) + "</ul></div>"
        for cid, c in CATS.items())
    body = (f'<h1>Фриланс-услуги на заказ</h1><p class="lead">Все категории RUWORK: дизайн, программирование, тексты, '
            f'маркетинг, видео и другое. Выберите услугу у фрилансера или разместите задание на бирже.</p>'
            f'<h2>Категории</h2><div class="cols">{cols}</div>')
    return page("Фриланс-услуги на заказ — все категории | RUWORK",
                "Каталог фриланс-услуг RUWORK: дизайн, разработка, тексты, маркетинг, видео, коучинг. Безопасная сделка.",
                "/uslugi", [("RUWORK", "/"), ("Услуги", "/uslugi")], body)


@router.get("/uslugi/{cat}", include_in_schema=False)
def category(cat: str, conn: sqlite3.Connection = Depends(get_db)):
    c = CATS.get(cat)
    if not c:
        raise HTTPException(404, "Категория не найдена")
    cnt, rows = counts(conn), gigs_of(conn, cat, None)
    low = min((g["base"] for g in rows), default=0)
    price = f" от {rub(low)}" if low else ""
    body = (f'<h1>{e(c[1])} — услуги фрилансеров</h1><p class="lead">Закажите {e(c[1].lower())} у фрилансеров на RUWORK'
            f'{e(price)}. Деньги продавец получает только после того, как вы подтвердите результат.</p>'
            f'<h2>Подкатегории</h2>{chips(cat, None, cnt)}<h2>Объявления</h2>{cards(rows)}')
    return page(f"{c[1]} — заказать у фрилансеров{price} | RUWORK",
                f"{c[1]}: {len(rows)} предложений от фрилансеров на RUWORK{price}. Безопасная сделка, отзывы покупателей.",
                f"/uslugi/{cat}", [("RUWORK", "/"), ("Услуги", "/uslugi"), (c[1], f"/uslugi/{cat}")], body)


@router.get("/uslugi/{cat}/{sub_slug}", include_in_schema=False)
def subcategory(cat: str, sub_slug: str, conn: sqlite3.Connection = Depends(get_db)):
    sub = SUB_BY_SLUG.get((cat, sub_slug))
    if not sub:
        raise HTTPException(404, "Подкатегория не найдена")
    c, cnt, rows = CATS[cat], counts(conn), gigs_of(conn, cat, sub)
    low = min((g["base"] for g in rows), default=0)
    price = f" от {rub(low)}" if low else ""
    body = (f'<h1>{e(sub)} на заказ</h1><p class="lead">{e(sub)} от фрилансеров RUWORK{e(price)}: выберите исполнителя '
            f'по отзывам, рейтингу и срокам. Оплата через безопасную сделку.</p>'
            f'<h2>Объявления</h2>{cards(rows)}<h2>Другое в категории «{e(c[1])}»</h2>{chips(cat, sub, cnt)}')
    return page(f"{sub} — заказать у фрилансеров{price} | RUWORK",
                f"{sub}: {len(rows)} предложений от фрилансеров на RUWORK{price}. Отзывы, рейтинг, безопасная сделка.",
                f"/uslugi/{cat}/{sub_slug}",
                [("RUWORK", "/"), ("Услуги", "/uslugi"), (c[1], f"/uslugi/{cat}"), (sub, f"/uslugi/{cat}/{sub_slug}")], body)


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    urls = ["/", "/uslugi"] + [f"/uslugi/{c}" for c in CATS] + [f"/uslugi/{c}/{s}" for c, s in SUB_BY_SLUG]
    body = "".join(f"<url><loc>{e(SITE + u)}</loc></url>" for u in urls)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"{body}</urlset>", media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse(f"User-agent: *\nDisallow: /api/\nDisallow: /media/\nAllow: /media/thumb/\n"
                             f"Sitemap: {SITE}/sitemap.xml\n")
