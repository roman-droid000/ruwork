"""Картинки для телефона и превью ссылок.

/media/thumb/<папка>/<файл>?w=… — уменьшенная копия обложки или фото из отзыва: телефон не качает оригинал
1280×800 ради карточки шириной 180 px. Копии создаются при первом запросе и лежат в UPLOAD_DIR/thumbs.

/g/<id> и /s/<id> — ссылки «Поделиться». Telegram, WhatsApp и VK не видят адрес после «#», поэтому для превью
нужна отдельная страница с тегами og:*. Человека она сразу перенаправляет на /#/gig/<id> или /#/seller/<id>.
Картинка превью (/og/…) рисуется по образцу карточек бота поддержки: тёмный фон, название, цена, продавец.
"""

import hashlib
import html
import io
import json
import sqlite3
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ..db import UPLOAD_DIR, get_db
from ..logic import SOLD_SQL, gig_out, rub, seller_ratings
from .orders import NAME_RE

router = APIRouter()

FONT = Path(__file__).resolve().parent.parent / "fonts" / "Inter.ttf"
THUMB_DIRS = {"covers", "reviews"}
THUMB_WIDTHS = {24, 240, 480, 960}
OG_W, OG_H = 1200, 630
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}

BG = (11, 11, 18)
VI, CY = (166, 107, 255), (111, 227, 255)
TX, MU = (238, 234, 251), (163, 159, 191)


@router.get("/media/thumb/{folder}/{name}", include_in_schema=False)
def thumb(folder: str, name: str, w: int = 480):
    src = UPLOAD_DIR / folder / name
    if folder not in THUMB_DIRS or w not in THUMB_WIDTHS or not NAME_RE.match(name) or not src.is_file():
        raise HTTPException(404, "Не найдено")
    out = UPLOAD_DIR / "thumbs" / folder / f"{w}-{Path(name).stem}.jpg"
    if not out.is_file() or out.stat().st_mtime < src.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            if im.width > w:
                im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
            tmp = out.with_suffix(".tmp")
            im.save(tmp, "JPEG", quality=60 if w == 24 else 82, optimize=True, progressive=w > 240)
            tmp.replace(out)
    return FileResponse(out, media_type="image/jpeg", headers=IMMUTABLE)


# ---------- Превью ссылок ----------

def plain(text: str | None) -> str:
    """Тексты объявлений хранятся экранированными для innerHTML (logic.esc) — для картинки нужен обычный текст."""
    return html.unescape(text or "").strip()


def load_gig(conn: sqlite3.Connection, gig_id: str) -> dict:
    r = conn.execute(
        f"SELECT g.*, {SOLD_SQL} FROM gigs g LEFT JOIN users u ON u.id = g.owner_id"
        " WHERE g.id = ? AND u.blocked_at IS NULL", (gig_id,)
    ).fetchone()
    if not r:
        raise HTTPException(404, "Объявление не найдено")
    return gig_out(r)


def load_seller(conn: sqlite3.Connection, user_id: str) -> dict:
    u = conn.execute("SELECT id, name, avatar, blocked_at FROM users WHERE id = ?", (user_id,)).fetchone()
    if not u or u["blocked_at"] is not None:
        raise HTTPException(404, "Продавец не найден")
    done = conn.execute("SELECT count(*) FROM orders WHERE seller_id = ? AND status = 'Выполнен'", (user_id,)).fetchone()[0]
    gigs = conn.execute("SELECT count(*) FROM gigs WHERE owner_id = ?", (user_id,)).fetchone()[0]
    rating, rev = seller_ratings(conn).get(user_id, (0, 0))
    return {"id": u["id"], "name": u["name"], "avatar": u["avatar"], "done": done, "gigs": gigs, "rating": rating, "rev": rev}


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 10 <= n % 100 < 20 else many


def gig_lines(g: dict) -> tuple[str, str]:
    title = plain(g["title"])
    rating = f" · ★ {g['rating']} ({g['rev']})" if g["rev"] else ""
    return title, f"от {rub(g['base'])} · {plain(g['sub'])} · {plain(g['seller'])}{rating}"


def seller_lines(s: dict) -> tuple[str, str]:
    bits = [f"★ {s['rating']} · {s['rev']} {plural(s['rev'], 'отзыв', 'отзыва', 'отзывов')}"] if s["rev"] else []
    bits.append(f"{s['done']} {plural(s['done'], 'заказ выполнен', 'заказа выполнено', 'заказов выполнено')}")
    bits.append(f"{s['gigs']} {plural(s['gigs'], 'услуга', 'услуги', 'услуг')}")
    return f"{plain(s['name'])} — продавец на RUWORK", " · ".join(bits)


def version(*parts) -> str:
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:10]


def share_page(request: Request, title: str, desc: str, image: str, target: str) -> HTMLResponse:
    base = str(request.base_url).rstrip("/")
    e = lambda t: html.escape(t, quote=True)  # noqa: E731
    js_target = json.dumps(target).replace("<", "\\u003c")
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} — RUWORK</title>
<meta name="description" content="{e(desc)}">
<meta property="og:type" content="website"><meta property="og:site_name" content="RUWORK">
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(base + request.url.path)}">
<meta property="og:image" content="{e(base + image)}"><meta property="og:image:width" content="{OG_W}"><meta property="og:image:height" content="{OG_H}">
<meta name="twitter:card" content="summary_large_image">
<meta http-equiv="refresh" content="0;url={e(target)}">
<script>location.replace({js_target})</script>
</head><body style="background:#0B0B12;color:#EEEAFB;font-family:sans-serif"><p><a href="{e(target)}" style="color:#B482FF">Открыть на RUWORK</a></p></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=300"})


@router.get("/g/{gig_id}", include_in_schema=False)
def share_gig(gig_id: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    g = load_gig(conn, gig_id)
    title, desc = gig_lines(g)
    v = version(title, desc, g["cover"])
    return share_page(request, title, desc, f"/og/gig/{g['id']}.jpg?v={v}", f"/#/gig/{g['id']}")


@router.get("/s/{user_id}", include_in_schema=False)
def share_seller(user_id: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    s = load_seller(conn, user_id)
    title, desc = seller_lines(s)
    v = version(title, desc, s["avatar"])
    return share_page(request, title, desc, f"/og/seller/{s['id']}.jpg?v={v}", f"/#/seller/{s['id']}")


@lru_cache(maxsize=32)
def font(size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), size)
    f.set_variation_by_axes([max(14, min(32, size)), weight])  # оптический размер, насыщенность
    return f


def backdrop() -> Image.Image:
    """Фон в цветах сайта: почти чёрный с фиолетовым и голубым свечением."""
    k = 10
    small = Image.new("RGB", (OG_W // k, OG_H // k), BG)
    d = ImageDraw.Draw(small)
    for x, y, r, c in ((0.15, 0.05, 0.38, VI), (0.95, 0.9, 0.32, CY), (0.6, 0.0, 0.22, (120, 70, 230))):
        cx, cy, rr = x * OG_W / k, y * OG_H / k, r * OG_W / k
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=tuple(int(BG[i] + (c[i] - BG[i]) * 0.42) for i in range(3)))
    return small.filter(ImageFilter.GaussianBlur(OG_W / k / 9)).resize((OG_W, OG_H), Image.BICUBIC)


def wrap(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, width: int, lines: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textlength(test, font=f) <= width:
            cur = test
            continue
        if cur:
            out.append(cur)
        cur = w
        if len(out) == lines:
            break
    if cur and len(out) < lines:
        out.append(cur)
    if len(out) == lines and " ".join(out) != " ".join(words):
        last = out[-1]
        while last and d.textlength(last + "…", font=f) > width:
            last = last[:-1]
        out[-1] = last.rstrip() + "…"
    return out


def paste_round(img: Image.Image, src: Path, box: tuple[int, int, int, int], radius: int) -> None:
    x0, y0, x1, y1 = box
    with Image.open(src) as im:
        im = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (x1 - x0, y1 - y0), Image.LANCZOS)
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *im.size), radius, fill=255)
    img.paste(im, (x0, y0), mask)


def pill(d: ImageDraw.ImageDraw, x: int, y: int, text: str, color: tuple) -> None:
    f = font(26, 600)
    w = int(d.textlength(text, font=f))
    d.rounded_rectangle((x, y, x + w + 36, y + 46), 23, fill=color + (46,), outline=color + (150,), width=2)
    d.text((x + 18, y + 23), text, font=f, fill=TX, anchor="lm")


def og_card(title: str, kicker: str, big: str, small: str, photo: Path | None, round_photo: bool) -> bytes:
    img = backdrop()
    d = ImageDraw.Draw(img, "RGBA")
    left, right = 72, OG_W - 72
    if photo:
        if round_photo:
            s = 300
            paste_round(img, photo, (right - s, 165, right, 165 + s), s // 2)
        else:
            paste_round(img, photo, (right - 430, 150, right, 150 + 330), 28)
        right -= 470
    d.text((left, 70), "RUWORK", font=font(34, 800), fill=TX, anchor="lm")
    d.text((left + d.textlength("RUWORK", font=font(34, 800)) + 18, 72), "маркетплейс фриланс-услуг", font=font(24, 500),
           fill=MU, anchor="lm")
    pill(d, left, 140, kicker, VI)
    f = font(62, 700)
    y = 225
    for line in wrap(d, title, f, right - left, 3):
        d.text((left, y), line, font=f, fill=TX)
        y += 76
    d.text((left, OG_H - 150), big, font=font(54, 800), fill=(214, 190, 255))
    d.text((left, OG_H - 78), small, font=font(28, 500), fill=MU)
    d.rectangle((0, OG_H - 8, OG_W, OG_H), fill=VI + (255,))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=86, optimize=True)
    return buf.getvalue()


def cached_og(key: str, v: str, render) -> Response:
    out = UPLOAD_DIR / "og" / f"{key}-{v}.jpg"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        for old in out.parent.glob(f"{key}-*.jpg"):
            old.unlink(missing_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(render())
        tmp.replace(out)
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


def media(folder: str, name: str | None) -> Path | None:
    p = UPLOAD_DIR / folder / name if name and NAME_RE.match(name) else None
    return p if p and p.is_file() else None


@router.get("/og/gig/{gig_id}.jpg", include_in_schema=False)
def og_gig(gig_id: str, conn: sqlite3.Connection = Depends(get_db)):
    g = load_gig(conn, gig_id)
    title, desc = gig_lines(g)
    rating = f"★ {g['rating']} · {g['rev']} {plural(g['rev'], 'отзыв', 'отзыва', 'отзывов')} · " if g["rev"] else ""
    small = f"{rating}продавец {plain(g['seller'])}"
    return cached_og(f"gig-{g['id']}", version(title, desc, g["cover"]), lambda: og_card(
        title, plain(g["sub"]), f"от {rub(g['base'])}", small, media("covers", g["cover"]), False))


@router.get("/og/seller/{user_id}.jpg", include_in_schema=False)
def og_seller(user_id: str, conn: sqlite3.Connection = Depends(get_db)):
    s = load_seller(conn, user_id)
    title, desc = seller_lines(s)
    big = f"★ {s['rating']}" if s["rev"] else f"{s['gigs']} {plural(s['gigs'], 'услуга', 'услуги', 'услуг')}"
    return cached_og(f"seller-{s['id']}", version(title, desc, s["avatar"]), lambda: og_card(
        plain(s["name"]), "Продавец", big, desc, media("avatars", s["avatar"]), True))


@router.get("/og/site.jpg", include_in_schema=False)
def og_site():
    return cached_og("site", "1", lambda: og_card(
        "Фриланс-услуги нового поколения", "Маркетплейс", "Безопасная сделка", "Дизайн · IT · тексты · видео · ruwork.shop", None, False))
