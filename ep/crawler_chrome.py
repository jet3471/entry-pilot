"""Chrome-based collector for sites that block plain HTTP (Naver blog search). Read only: it opens a new tab in the
entry Chrome (ep.chrome, hidden desktop, the user's logged-in profile), reads the
search result list, and closes the tab. It never clicks, posts or submits anything."""
from __future__ import annotations

import contextlib
import datetime as dt
import re
import urllib.parse

from ep import chrome
from ep.urlpolicy import ALLOWED_BROWSER_METHODS, PolicyBlockedError, fetch_pinned, is_public_http_url

NAVER_QUERIES = ("이벤트 응모 경품 댓글", "댓글 이벤트 추첨 기프티콘", "이벤트 참여 방법 당첨자 발표", "응모 이벤트 상품 증정")
NAVER_URL = "https://search.naver.com/search.naver?ssc=tab.blog.all&sm=tab_opt&nso=so%3Add%2Cp%3A1w&query="
POST_RE = re.compile(r"^https://blog\.naver\.com/[^/?#]+/\d{6,}")
JS = """els => els.map(e => [e.innerText.trim(), e.href])"""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("새 창 열림", "")).strip()


def parse_links(links: list[list[str]], today: dt.date) -> list[dict]:
    """links: [[text, href], ...] in page order. The first long text of a post is its title, the next is a snippet."""
    posts: dict[str, dict] = {}
    for text, href in links:
        m = POST_RE.match(href)
        if not m:
            continue
        url = m.group(0)
        text = _clean(text)
        if not text:
            continue
        p = posts.setdefault(url, {"title": "", "snippet": ""})
        if not p["title"]:
            p["title"] = text
        elif len(text) > len(p["snippet"]):
            p["snippet"] = text
    out = []
    for i, (url, p) in enumerate(posts.items()):
        out.append({
            "source": "naverblog", "source_url": url, "title": p["title"][:120], "org": "",
            "deadline_hint": "", "entry_url_guess": url, "posted": today.isoformat(),
            "_deadline_iso": "", "_order": -i, "_has_detail": True,
            "text": _clean(f"네이버 블로그 글 {p['title']}. {p['snippet']} 응모 링크는 글 본문에서 확인."),
        })
    return out


def _close_popup(popup) -> None:
    with contextlib.suppress(Exception):
        popup.route("**/*", lambda r: r.abort())
    with contextlib.suppress(Exception):
        popup.close()


def _cookie_header_for(ctx, url: str) -> str:
    if ctx is None or not hasattr(ctx, "cookies"):
        return ""
    try:
        ck = ctx.cookies([url])
    except Exception:
        return ""
    if not isinstance(ck, list):
        return ""
    pairs = [f"{c['name']}={c.get('value', '')}" for c in ck if isinstance(c, dict) and c.get("name")]
    return "; ".join(pairs)


def _guard_page(pg) -> None:
    if hasattr(pg, "on"):
        with contextlib.suppress(Exception):
            pg.on("popup", _close_popup)

    def _handle(route):
        req = route.request
        raw_m = getattr(req, "method", None)
        method = raw_m.upper() if isinstance(raw_m, str) and raw_m else "GET"
        if method not in ALLOWED_BROWSER_METHODS:
            route.abort()
            return
        raw_hdrs = getattr(req, "headers", None)
        hdrs = dict(raw_hdrs) if isinstance(raw_hdrs, dict) else {}
        ctx = getattr(pg, "context", None)
        try:
            status, resp_hdrs, body, _final_url = fetch_pinned(
                req.url,
                method=method,
                headers=hdrs,
                cookie_provider=(lambda u: _cookie_header_for(ctx, u)) if ctx is not None else None,
            )
        except (PolicyBlockedError, OSError, Exception):
            route.abort()
            return
        route.fulfill(status=status, headers=resp_hdrs, body=body)

    pg.route("**/*", _handle)


@contextlib.contextmanager
def _open_guarded_page(b):
    own_ctx = False
    try:
        ctx = b.new_context(service_workers="block")
        own_ctx = True
        if getattr(b, "contexts", None):
            with contextlib.suppress(Exception):
                cookies = b.contexts[0].cookies()
                if isinstance(cookies, list) and cookies:
                    ctx.add_cookies(cookies)
    except Exception:
        ctx = b.contexts[0]
    pg = ctx.new_page()

    def _on_new_page(new_pg):
        if new_pg is not pg:
            _close_popup(new_pg)

    if own_ctx and hasattr(ctx, "on"):
        with contextlib.suppress(Exception):
            ctx.on("page", _on_new_page)
    try:
        _guard_page(pg)
        yield pg
    finally:
        with contextlib.suppress(Exception):
            pg.close()
        if own_ctx:
            with contextlib.suppress(Exception):
                ctx.close()


def _goto_public(pg, url: str, timeout: int = 30000) -> None:
    ok, reason = is_public_http_url(url)
    if not ok:
        raise RuntimeError(f"blocked url ({reason}): {url}")
    pg.goto(url, timeout=timeout)
    if pg.url and pg.url != "about:blank":
        ok2, reason2 = is_public_http_url(pg.url)
        if not ok2:
            raise RuntimeError(f"blocked redirect url ({reason2}): {pg.url}")


def src_naverblog(fetch=None, today: dt.date | None = None) -> list[dict]:
    if not chrome.alive():
        raise RuntimeError("entry Chrome (9222) is not running")
    today = today or dt.date.today()
    from playwright.sync_api import sync_playwright
    out, seen = [], set()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(chrome.CDP)
        with _open_guarded_page(b) as pg:
            for q in NAVER_QUERIES:
                _goto_public(pg, NAVER_URL + urllib.parse.quote(q), timeout=30000)
                pg.wait_for_timeout(2000)
                links = pg.eval_on_selector_all("a[href*='blog.naver.com']", JS)
                for it in parse_links(links, today):
                    if it["source_url"] not in seen:
                        seen.add(it["source_url"])
                        out.append(it)
    return out


CAFE_QUERIES = ("댓글 이벤트 추첨", "이벤트 응모 경품 기프티콘")
CAFE_URL = "https://search.naver.com/search.naver?ssc=tab.cafe.all&sm=tab_opt&nso=so%3Add%2Cp%3A1w&query="
CAFE_RE = re.compile(r"^https://cafe\.naver\.com/[^/?#]+/\d+")
INSTA_TAGS = ("댓글이벤트", "경품이벤트", "기프티콘이벤트", "추첨이벤트")
INSTA_URL = "https://www.instagram.com/explore/search/keyword/?q=%23"
INSTA_RE = re.compile(r"^https://www\.instagram\.com/p/[\w-]+")
INSTA_MAX_OPEN = 14
EVENT_WORDS = ("이벤트", "응모", "추첨", "경품", "증정", "giveaway")


def _item(source, url, title, text, today, order):
    return {"source": source, "source_url": url, "title": title[:120], "org": "", "deadline_hint": "",
            "entry_url_guess": url, "posted": today.isoformat(), "_deadline_iso": "", "_order": order,
            "_has_detail": True, "text": _clean(text)}


def parse_cafe_links(links: list[list[str]], today: dt.date) -> list[dict]:
    posts: dict[str, str] = {}
    for text, href in links:
        m = CAFE_RE.match(href)
        text = _clean(text)
        if m and text and m.group(0) not in posts:
            posts[m.group(0)] = text
    return [_item("navercafe", u, t, f"네이버 카페 글 {t}. 응모 링크는 글 본문에서 확인.", today, -i)
            for i, (u, t) in enumerate(posts.items())]


def src_navercafe(fetch=None, today: dt.date | None = None) -> list[dict]:
    if not chrome.alive():
        raise RuntimeError("entry Chrome (9222) is not running")
    today = today or dt.date.today()
    from playwright.sync_api import sync_playwright
    out, seen = [], set()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(chrome.CDP)
        with _open_guarded_page(b) as pg:
            for q in CAFE_QUERIES:
                _goto_public(pg, CAFE_URL + urllib.parse.quote(q), timeout=30000)
                pg.wait_for_timeout(2000)
                links = pg.eval_on_selector_all("a[href*='cafe.naver.com']", JS)
                for it in parse_cafe_links(links, today):
                    if it["source_url"] not in seen:
                        seen.add(it["source_url"])
                        out.append(it)
    return out


def caption_ok(caption: str) -> bool:
    return any(w in caption.lower() for w in EVENT_WORDS)


def src_instagram(fetch=None, today: dt.date | None = None) -> list[dict]:
    """Hashtag search, then each post's og:description (caption). Posts without event words are dropped."""
    if not chrome.alive():
        raise RuntimeError("entry Chrome (9222) is not running")
    today = today or dt.date.today()
    from playwright.sync_api import sync_playwright
    urls: list[str] = []
    out = []
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(chrome.CDP)
        with _open_guarded_page(b) as pg:
            for tag in INSTA_TAGS:
                _goto_public(pg, INSTA_URL + urllib.parse.quote(tag), timeout=30000)
                pg.wait_for_timeout(3000)
                for _, href in pg.eval_on_selector_all("a[href*='/p/']", JS):
                    m = INSTA_RE.match(href)
                    if m and m.group(0) + "/" not in urls and m.group(0) not in urls:
                        urls.append(m.group(0))
            for i, u in enumerate(urls[:INSTA_MAX_OPEN]):
                _goto_public(pg, u + "/", timeout=30000)
                pg.wait_for_timeout(1200)
                cap = pg.evaluate("() => (document.querySelector('meta[property=\"og:description\"]')||{}).content || ''")
                if not caption_ok(cap):
                    continue
                title = _clean(cap.split(":", 1)[-1])[:60] or "인스타그램 이벤트"
                out.append(_item("instagram", u + "/", title, "인스타그램 게시글 설명: " + cap[:900], today, -i))
    return out
