"""Plain-Python scouting crawler for online events (giveaways, surveys, quizzes).

Replaces step-by-step LLM browsing: it lists events from a few public sources,
fetches each candidate's detail page, and writes a JSON list for an LLM judge.
GET only. Never submits anything. Never opens supertoday.co.kr / suto.co.kr.

    python -X utf8 -m ep.crawler --known known-events.txt --out cand.json [--max 40] [--today YYYY-MM-DD]

Each source is a small function ``src_<name>(fetch) -> list[stub]``. ``fetch(url)``
returns the page text and raises on failure. A stub is a dict with at least
source, source_url, title; optional org, deadline_hint, entry_url_guess, text,
posted (ISO date, used for newest-first sorting), links, and "_detail"
(callable(BeautifulSoup) -> dict of field updates, run on the fetched detail page).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from bs4 import BeautifulSoup

from ep.crawler_chrome import src_instagram, src_navercafe, src_naverblog
from ep.urlpolicy import build_pinned_opener, is_public_http_url, resolve_public_http_url

UA = "Mozilla/5.0"
TIMEOUT = 20
HOST_DELAY = 1.0
MAX_REDIRECTS = 6
TEXT_MAX = 1800
MAX_LINKS = 8
BLOCKED_HOSTS = ("supertoday.co.kr", "suto.co.kr")
# Hosts whose pages are useless to fetch as plain HTML (login walls / JS apps).
NO_DETAIL_HOSTS = ("blog.naver.com", "cafe.naver.com", "instagram.com", "youtube.com", "youtu.be", "facebook.com", "twitter.com", "x.com")

LINK_PATTERNS = (
    "forms.gle", "docs.google.com/forms", "form.naver.com", "naver.me", "moaform",
    "nownsurvey", "instagram.com", "youtube.com", "youtu.be", "blog.naver.com",
)
# Preference order when guessing the entry URL from found links.
ENTRY_PREFERENCE = (
    "forms.gle", "docs.google.com/forms", "form.naver.com", "moaform", "nownsurvey",
    "naver.me", "blog.naver.com", "instagram.com", "youtube.com", "youtu.be",
)
# Accounts of the aggregator sites themselves: not entry links.
SELF_LINKS = ("instagram.com/eventhouse", "instagram.com/events.kr", "instagram.com/quasarzone")


# ---------------------------------------------------------------- fetching

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401
        return None


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _is_blocked(url: str) -> bool:
    h = _host(url)
    return any(h == b or h.endswith("." + b) for b in BLOCKED_HOSTS)


def _decode(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
        enc = m.group(1).decode("ascii", "ignore") if m else ""
    else:
        enc = m.group(1)
    for e in (enc, "utf-8", "cp949"):
        if not e:
            continue
        try:
            return raw.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


class Fetcher:
    """GET-only fetcher with per-host delay. Callable: fetch(url) -> text."""

    def __init__(self, delay: float = HOST_DELAY, timeout: int = TIMEOUT):
        self.delay = delay
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._noredir = build_pinned_opener(_NoRedirect)

    def _wait(self, url: str) -> None:
        # Reserve the next free slot for this host under the lock and sleep outside it,
        # so threads on different hosts never wait for each other (1 s per host stays).
        h = _host(url)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._last.get(h, -1e9) + self.delay)
            self._last[h] = slot
        if slot > now:
            time.sleep(slot - now)

    def _req(self, url: str) -> urllib.request.Request:
        if _is_blocked(url):
            raise RuntimeError("blocked host: " + _host(url))
        ok, reason, pinned_ips = resolve_public_http_url(url)
        if not ok:
            raise RuntimeError(f"blocked url ({reason}): {url}")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"}, method="GET")
        req._pinned_ips = tuple(pinned_ips)
        return req

    def __call__(self, url: str) -> str:
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            req = self._req(cur)
            self._wait(cur)
            try:
                with self._noredir.open(req, timeout=self.timeout) as r:
                    final_url = r.geturl() if hasattr(r, "geturl") else cur
                    if final_url and final_url != cur:
                        self._req(final_url)
                    raw = r.read(3_000_000)
                    return _decode(raw, r.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as e:
                try:
                    loc = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
                finally:
                    e.close()
                if loc is None:
                    raise
                if not loc.strip():
                    raise RuntimeError("empty redirect location: " + cur)
                cur = urllib.parse.urljoin(cur, loc.strip())
        raise RuntimeError("too many redirects: " + url)

    def resolve(self, url: str, hops: int = 3) -> str:
        """Follow redirects manually (Location headers only) and return the last URL.

        Stops as soon as the target host differs from the starting host: the
        external page is never requested. Returns "" on failure.
        """
        start_host = _host(url)
        cur = url
        for _ in range(hops):
            req = self._req(cur)
            self._wait(cur)
            try:
                with self._noredir.open(req, timeout=self.timeout) as r:
                    final_url = r.geturl() if hasattr(r, "geturl") else cur
                    if final_url and final_url != cur:
                        self._req(final_url)
                    return cur  # 200: this is the page itself
            except urllib.error.HTTPError as e:
                try:
                    loc = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
                finally:
                    e.close()
                if not loc or not loc.strip():
                    return ""
                cur = urllib.parse.urljoin(cur, loc.strip())
                if _host(cur) != start_host:
                    self._req(cur)
                    return cur
        return ""


# ---------------------------------------------------------------- helpers

def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def visible_text(root) -> str:
    root = BeautifulSoup(str(root), "html.parser")
    for x in root(["script", "style", "noscript", "nav", "header", "footer", "form", "iframe"]):
        x.decompose()
    return clean(root.get_text(" ", strip=True))


def find_links(root, base: str = "") -> list[str]:
    seen: list[str] = []
    cands = [a.get("href", "") for a in root.find_all("a", href=True)]
    cands += re.findall(r"https?://[^\s\"'<>)\]]+", root.get_text(" "))
    for h in cands:
        h = h.strip().rstrip(".,;")
        if not h or h.startswith(("#", "javascript:", "mailto:")):
            continue
        u = urllib.parse.urljoin(base, h) if base else h
        low = u.lower()
        if not any(p in low for p in LINK_PATTERNS) or any(s in low for s in SELF_LINKS):
            continue
        if u not in seen:
            seen.append(u)
    return seen[:MAX_LINKS]


def guess_entry(links: list[str]) -> str:
    for pref in ENTRY_PREFERENCE:
        for u in links:
            if pref in u.lower():
                return u
    return ""


def norm_title(t: str) -> str:
    t = re.sub(r"^(\s*\[[^\]]*\])+", "", t or "")
    return re.sub(r"[\W_]+", "", t.lower())


def _iso(y: int, m: int, d: int) -> str:
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return ""


def parse_md(s: str, today: dt.date) -> str:
    """'11/20' -> ISO date, choosing the year that puts it within ~6 months of today."""
    m = re.search(r"(\d{1,2})\s*[/.]\s*(\d{1,2})", s or "")
    if not m:
        return ""
    mo, d = int(m.group(1)), int(m.group(2))
    best = ""
    for y in (today.year - 1, today.year, today.year + 1):
        iso = _iso(y, mo, d)
        if iso and abs((dt.date.fromisoformat(iso) - today).days) <= 183:
            best = iso
    return best


def last_date(s: str, today: dt.date) -> str:
    """Last full date found in a string like '2026-11-13 ~ 2026-11-20' or '26.11.13 ~26.11.19'."""
    found = ""
    for m in re.finditer(r"(?<!\d)(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})", s or ""):
        found = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))) or found
    if not found:
        for m in re.finditer(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{2})(?!\d)", s or ""):
            found = _iso(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))) or found
    return found


def first_date(s: str, today: dt.date) -> str:
    for m in re.finditer(r"(?<!\d)(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})", s or ""):
        iso = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            return iso
    for m in re.finditer(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{2})(?!\d)", s or ""):
        iso = _iso(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            return iso
    return ""


def today_default() -> dt.date:
    return dt.date.today()


# ---------------------------------------------------------------- sources
# Every src_* takes fetch(url)->str and returns stubs, newest first.

EH_BASE = "https://www.eventhouse.kr"


def eventhouse_parse_list(html: str, today: dt.date) -> list[dict]:
    s = soup_of(html)
    out = []
    for row in s.select(".list_row"):
        a = row.select_one(".list_no a[href]")
        subj = row.select_one(".list_subject")
        if not a or not subj:
            continue
        view = urllib.parse.urljoin(EH_BASE + "/site/", a["href"])
        org_el = subj.find("strong")
        org = clean(org_el.get_text()) if org_el else ""
        sub = BeautifulSoup(str(subj), "html.parser").select_one(".list_subject")
        for x in sub.find_all(["sup", "img"]):
            x.decompose()
        title = clean(sub.get_text(" "))
        if org:
            title = clean(re.sub(r"^\[\s*" + re.escape(org) + r"\s*\]\s*", "", title))
        cate = clean((row.select_one(".list_cates") or row).get_text()) if row.select_one(".list_cates") else ""
        det = row.find_next_sibling("div", class_="list_detail")
        dtext = clean(det.get_text(" ", strip=True)) if det else ""
        fnum = re.search(r"fnum=(\d+)", str(det or row))
        no = re.search(r"no=(\d+)", view)
        period = re.search(r"응모기간\s*([^ㆍ]*?)\s*(?:ㆍ|$)", dtext)
        deadline = ""
        if period:
            deadline = clean(period.group(1))
        if not deadline:
            deadline = clean((row.select_one(".list_day") or row).get_text()) if row.select_one(".list_day") else ""
        dl_iso = last_date(deadline, today) or parse_md(deadline, today)
        text = clean(f"{cate} [{org}] {title}. {dtext}")
        out.append({
            "source": "eventhouse", "source_url": view, "title": title, "org": org,
            "deadline_hint": deadline, "entry_url_guess": "", "text": text,
            "posted": first_date(deadline, today), "_deadline_iso": dl_iso,
            "_fnum": fnum.group(1) if fnum else (no.group(1) if no else ""),
            "_order": int(no.group(1)) if no else 0, "_has_detail": True,
        })
    return out


def src_eventhouse(fetch, today: dt.date | None = None, pages: int = 2, flogin: bool = False) -> list[dict]:
    """eventhouse.kr list (https://www.eventhouse.kr/site/?page=N, '/' redirects to /site).

    The site's own fgo.php entry redirect demands a login unless the link carries
    flogin_no=1 (the site puts that only on ad tiles). By default we respect the
    login gate and leave entry_url_guess empty; flogin=True resolves it anyway.
    """
    today = today or today_default()
    out: list[dict] = []
    for p in range(1, pages + 1):
        out += eventhouse_parse_list(fetch(f"{EH_BASE}/site/?page={p}"), today)
    out.sort(key=lambda x: -x["_order"])
    if flogin and hasattr(fetch, "resolve"):
        for it in out:
            it["_resolve"] = f"{EH_BASE}/site/fgo.php?fnum={it['_fnum']}&go=2&flogin_no=1"
    return out


QZ_BASE = "https://quasarzone.com"


def quasarzone_parse_list(html: str, today: dt.date) -> list[dict]:
    s = soup_of(html)
    out = []
    for c in s.select("a.v2-card[href*='qe_event/views/']"):
        url = urllib.parse.urljoin(QZ_BASE, c["href"])
        title = clean((c.select_one(".v2-card__title") or c).get_text(" "))
        spans = [clean(x.get_text(" ")) for x in c.select(".v2-card__row span")]
        cate = spans[0] if spans else ""
        rng = next((x for x in spans if re.search(r"\d{2}\.\d{2}\.\d{2}", x)), "")
        dl = last_date(rng, today)
        posted = first_date(rng, today)
        m = re.search(r"/views/(\d+)", url)
        out.append({
            "source": "quasarzone", "source_url": url, "title": title, "org": "",
            "deadline_hint": clean(rng) + (f" ({cate})" if cate else ""),
            "entry_url_guess": "", "posted": posted, "_deadline_iso": dl,
            "_order": int(m.group(1)) if m else 0,
            "_detail": _qz_detail,
        })
    return out


def _qz_detail(s: BeautifulSoup, url: str) -> dict:
    body = s.select_one(".view-content") or s
    links = find_links(body, url)
    return {"text": visible_text(body), "links": links}


def src_quasarzone(fetch, today: dt.date | None = None, pages: int = 1) -> list[dict]:
    """quasarzone.com/bbs/qe_event: list cards are server-rendered (a.v2-card)."""
    today = today or today_default()
    out: list[dict] = []
    for p in range(1, pages + 1):
        out += quasarzone_parse_list(fetch(f"{QZ_BASE}/bbs/qe_event?page={p}"), today)
    out.sort(key=lambda x: -x["_order"])
    return out


EK_BASE = "https://events.kr"


def eventskr_parse_list(html: str) -> list[dict]:
    s = soup_of(html)
    best: dict[int, str] = {}
    for a in s.find_all("a", href=re.compile(r"^event\.php\?id=\d+")):
        i = int(re.search(r"id=(\d+)", a["href"]).group(1))
        t = clean(a.get_text(" "))
        if len(t) > len(best.get(i, "")) and t not in ("응모", "응모하러 가기"):
            best[i] = t
        best.setdefault(i, "")
    out = []
    for i in sorted(best, reverse=True):
        out.append({
            "source": "eventskr", "source_url": f"{EK_BASE}/event.php?id={i}", "title": best[i],
            "org": "", "deadline_hint": "", "entry_url_guess": "", "_order": i,
            "_detail": _ek_detail,
        })
    return out


def _ek_detail(s: BeautifulSoup, url: str) -> dict:
    upd: dict = {}
    if s.title and s.title.string:
        upd["title"] = clean(re.sub(r"\s*-\s*이벤트 코리아\s*$", "", s.title.string))
    body = s
    for x in s(["script", "style"]):
        x.decompose()
    text = visible_text(body)
    # drop comment form / footer chatter
    text = re.split(r"광고 댓글 \(", text)[0]
    upd["text"] = text
    m = re.search(r"주최\s+(.+?)\s+당첨자", text)
    if m:
        upd["org"] = clean(m.group(1))[:60]
    m = re.search(r"(\d{4}년 \d{1,2}월 \d{1,2}일)\s*~\s*(\d{1,2}월 \d{1,2}일)", text)
    if m:
        upd["deadline_hint"] = clean(m.group(0))
    r = re.search(r"등록일\s+(\d{4})\.(\d{2})\.(\d{2})", text)
    if r:
        upd["posted"] = f"{r.group(1)}-{r.group(2)}-{r.group(3)}"
    upd["links"] = find_links(body, url)
    return upd


def src_eventskr(fetch, today: dt.date | None = None, pages: int = 2) -> list[dict]:
    """events.kr user/agency registered events (event.php?id=N, list at /?page=N)."""
    out: list[dict] = []
    seen = set()
    for p in range(1, pages + 1):
        for it in eventskr_parse_list(fetch(f"{EK_BASE}/?page={p}")):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    out.sort(key=lambda x: -x["_order"])
    return out


KK_URL = "https://www.korea.kr/etc/byulbyulList.do"


def koreakr_parse_list(html: str, today: dt.date) -> list[dict]:
    s = soup_of(html)
    out = []
    for i, a in enumerate(s.select(".list_type.event li a[href]")):
        url = urllib.parse.urljoin(KK_URL, a["href"])
        strong = a.find("strong")
        full = clean(strong.get_text(" ")) if strong else ""
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", full)
        org, title = (m.group(1), m.group(2)) if m else ("", full)
        period = clean((a.select_one(".source") or a).get_text(" ")).replace("기간 :", "").strip()
        out.append({
            "source": "koreakr", "source_url": url, "title": title or full, "org": org,
            "deadline_hint": period, "entry_url_guess": url,
            "posted": first_date(period, today), "_deadline_iso": last_date(period, today),
            "_order": -i, "text": clean(f"[{org}] {title} {period}"), "_detail": _generic_detail,
        })
    return out


def _generic_detail(s: BeautifulSoup, url: str) -> dict:
    body = s.body or s
    return {"text": visible_text(body), "links": find_links(body, url)}


def src_koreakr(fetch, today: dt.date | None = None) -> list[dict]:
    """korea.kr (정책브리핑) 이벤트: government agency contests/quizzes/surveys.

    The listed link is the agency's own page, so it is also the entry URL guess.
    """
    today = today or today_default()
    return koreakr_parse_list(fetch(KK_URL), today)


DN_BASE = "https://event.danawa.com"
DN_SKIP_TYPES = ("체험단", "기자단")  # reviewer recruiting, not a giveaway entry


def danawa_parse_list(html: str, today: dt.date) -> list[dict]:
    """event.danawa.com/event/lists: one table row per running event (type, company, period)."""
    s = soup_of(html)
    out = []
    for i, tr in enumerate(s.select("tr")):
        a = tr.select_one("td.title a.link_tit[href]")
        if not a or not tr.select_one(".ico_state.t_ing"):
            continue
        typ = clean(tr.select_one("td.type").get_text(" ")) if tr.select_one("td.type") else ""
        if typ in DN_SKIP_TYPES:
            continue
        url = urllib.parse.urljoin(DN_BASE + "/", a["href"])
        title = clean(a.get_text(" "))
        org = clean(tr.select_one("td.company").get_text(" ")) if tr.select_one("td.company") else ""
        due = clean(tr.select_one("td.due").get_text(" ")) if tr.select_one("td.due") else ""
        ann = clean(tr.select_one("td.date").get_text(" ")) if tr.select_one("td.date") else ""
        m = re.search(r"_(\d{6})$", a["href"])
        order = int(m.group(1)) if m else 0
        out.append({
            "source": "danawa", "source_url": url, "title": title, "org": org,
            "deadline_hint": due, "entry_url_guess": url, "posted": first_date(due, today),
            "_deadline_iso": last_date(due, today), "_order": order - i / 1000, "_has_detail": True,
            "text": clean(f"다나와 이벤트 [{typ}] [{org}] {title}. 응모기간 {due}. 당첨자 발표 {ann}. "
                          "참여는 이벤트 페이지(다나와 로그인)에서."),
        })
    return out


def src_danawa(fetch, today: dt.date | None = None, pages: int = 2) -> list[dict]:
    """event.danawa.com: brand quiz / comment / roulette / survey events (server-rendered table)."""
    today = today or today_default()
    out: list[dict] = []
    seen = set()
    for p in range(1, pages + 1):
        for it in danawa_parse_list(fetch(f"{DN_BASE}/event/lists?page={p}"), today):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    out.sort(key=lambda x: -x["_order"])
    return out


WV_BASE = "https://www.wevity.com/"
WV_LIST = WV_BASE + "?c=find&s=1&gub=1&cidx=&sp=&sw=&gbn=list&mode=ing"


def wevity_parse_list(html: str, today: dt.date) -> list[dict]:
    """wevity.com contests currently accepting entries (mode=ing); cards carry D-day only."""
    s = soup_of(html)
    out = []
    for li in s.select("ul.list li"):
        a = li.select_one("div.tit a[href*='gbn=view']")
        if not a:
            continue
        ix = re.search(r"ix=(\d+)", a["href"])
        if not ix:
            continue
        for x in a.select("span"):
            x.decompose()
        title = clean(a.get_text(" "))
        url = f"{WV_BASE}?c=find&s=1&gbn=view&gp=1&ix={ix.group(1)}"
        org = clean(li.select_one("div.organ").get_text(" ")) if li.select_one("div.organ") else ""
        field = clean(li.select_one("div.sub-tit").get_text(" ")) if li.select_one("div.sub-tit") else ""
        day = clean(li.select_one("div.day").get_text(" ")) if li.select_one("div.day") else ""
        dm = re.search(r"D-(\d+)", day)
        dl = (today + dt.timedelta(days=int(dm.group(1)))).isoformat() if dm else ""
        out.append({
            "source": "wevity", "source_url": url, "title": title, "org": org,
            "deadline_hint": day, "entry_url_guess": "", "_deadline_iso": dl,
            "_order": int(ix.group(1)), "text": clean(f"[공모전] [{org}] {title}. {field}"),
            "_detail": _wv_detail,
        })
    return out


def _wv_detail(s: BeautifulSoup, url: str) -> dict:
    upd: dict = {}
    info = {}
    for li in s.select("ul.cd-info-list li"):
        k, _, v = clean(li.get_text(" ")).partition(" ")
        info[k] = v
    body = s.select_one("div.cd-area") or s.body or s
    text = visible_text(body)
    upd["text"] = re.split(r"본 정보의 사실 여부를 보증하지", text)[0]
    period = info.get("접수기간", "")
    if period:
        upd["deadline_hint"] = period
        today = today_default()
        upd["_deadline_iso"] = last_date(period, today)
        upd["posted"] = first_date(period, today)
    if info.get("주최/주관"):
        upd["org"] = info["주최/주관"][:60]
    links = [u for u in find_links(body, url) if _host(u) != _host(url)]
    home = ""
    for ha in body.select("ul.cd-info-list a[href^='http']"):  # href keeps '&' intact, text does not
        if _host(ha["href"]) != _host(url):
            home = ha["href"]
            break
    if home:
        upd["entry_url_guess"] = home
        if home not in links:
            links.insert(0, home)
    upd["links"] = links[:MAX_LINKS]
    return upd


def src_wevity(fetch, today: dt.date | None = None) -> list[dict]:
    """wevity.com 공모전 (robots allows all): naming / idea / survey-like contests open now."""
    today = today or today_default()
    out = wevity_parse_list(fetch(WV_LIST), today)
    out.sort(key=lambda x: -x["_order"])
    return out


SEOUL_FEEDS = ("gov", "economy", "citybuild")
SEOUL_KEEP = re.compile(r"설문|투표|이벤트|퀴즈|응모|경품|공모|참여")
SEOUL_DROP = re.compile(r"결과|발표|수상작|당첨|선정|종료|마감")


def seoul_parse_feed(xml: str, today: dt.date) -> list[dict]:
    """news.seoul.go.kr/<cat>/feed (WordPress RSS, robots allow). Title-keyword filtered."""
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(n):
            m = re.search(rf"<{n}>(.*?)</{n}>", it, re.S)
            return re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip()) if m else ""
        title = clean(htmllib.unescape(tag("title")))
        link = clean(tag("link"))
        if not title or not link or not SEOUL_KEEP.search(title) or SEOUL_DROP.search(title):
            continue
        desc = tag("content:encoded") or tag("description")
        body = soup_of(htmllib.unescape(desc))
        text = clean(title + ". " + visible_text(body))
        rng = re.search(r"~\s*(\d{1,2})\s*[./월]\s*(\d{1,2})", text)
        dl = parse_md(f"{rng.group(1)}/{rng.group(2)}", today) if rng else ""
        m = re.search(r"archives/(\d+)", link)
        out.append({
            "source": "seoulnews", "source_url": link, "title": title, "org": clean(tag("dc:creator"))[:60],
            "deadline_hint": "", "entry_url_guess": link, "posted": clean(tag("pubDate"))[:10],
            "_deadline_iso": dl, "_order": int(m.group(1)) if m else 0, "text": text[:TEXT_MAX],
            "links": find_links(body, link), "_has_detail": True,
        })
    return out


def src_seoulnews(fetch, today: dt.date | None = None) -> list[dict]:
    """Seoul city news feeds: surveys, public votes and contests announced by the city."""
    today = today or today_default()
    out: list[dict] = []
    for cat in SEOUL_FEEDS:
        try:
            out += seoul_parse_feed(fetch(f"https://news.seoul.go.kr/{cat}/feed"), today)
        except Exception:
            if cat == SEOUL_FEEDS[0]:
                raise
    out.sort(key=lambda x: -x["_order"])
    return out


SOURCES: list[tuple[str, Callable]] = [
    ("eventhouse", src_eventhouse),
    ("quasarzone", src_quasarzone),
    ("eventskr", src_eventskr),
    ("koreakr", src_koreakr),
    ("danawa", src_danawa),
    ("wevity", src_wevity),
    ("seoulnews", src_seoulnews),
    ("naverblog", src_naverblog),
    ("navercafe", src_navercafe),
    ("instagram", src_instagram),
]

# Per-source cap on items taken in one run (default: CAP_FRACTION of --max) so one big
# source cannot fill the whole list. Noisy / low-yield sources get a smaller fixed cap.
CAP_FRACTION = 0.4
SOURCE_CAPS = {"wevity": 8, "seoulnews": 6, "naverblog": 12, "navercafe": 8, "instagram": 8}
LIST_WORKERS = 8


# ---------------------------------------------------------------- pipeline

def load_known(path: str) -> tuple[set[str], set[str]]:
    urls: set[str] = set()
    titles: set[str] = set()
    if not path:
        return urls, titles
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            url, _, title = line.partition("\t")
            if url.strip():
                urls.add(url.strip())
            if title.strip():
                titles.add(title.strip())
    return urls, titles


def _url_key(u: str) -> str:
    return u.strip().rstrip("/").lower()


def enrich(fetch, stub: dict, errors: list[str]) -> None:
    """Fetch the detail page (when the stub does not carry its text) and fill fields."""
    url = stub["source_url"]
    if stub.get("_has_detail"):
        pass
    elif any(h in _host(url) for h in NO_DETAIL_HOSTS):
        pass
    else:
        try:
            s = soup_of(fetch(url))
            det = stub.get("_detail") or _generic_detail
            upd = det(s, url)
            for k, v in upd.items():
                if v:
                    stub[k] = v
        except Exception as e:  # never crash on one page
            errors.append(f"{stub['source']}: detail {url}: {type(e).__name__}: {e}")
    text = stub.get("text") or stub.get("title", "")
    links = stub.get("links") or find_links(soup_of(f"<p>{text}</p>"))
    stub["links"] = links[:MAX_LINKS]
    if stub.get("_resolve") and hasattr(fetch, "resolve"):
        try:
            real = fetch.resolve(stub["_resolve"])
            if real and "login" not in real:
                stub["entry_url_guess"] = real
                if real not in stub["links"] and len(stub["links"]) < MAX_LINKS:
                    stub["links"].append(real)
        except Exception as e:
            errors.append(f"{stub['source']}: resolve {stub['_resolve']}: {type(e).__name__}: {e}")
    if not stub.get("entry_url_guess"):
        stub["entry_url_guess"] = guess_entry(stub["links"])
    stub["text"] = text[:TEXT_MAX]


def _contest_field_ok(text: str) -> bool:
    from ep import policy
    return policy.contest_field_ok(text)


def collect(fetch, known_urls: set[str], known_titles: set[str], max_items: int, today: dt.date,
            sources=None, errors: list[str] | None = None) -> tuple[list[dict], dict[str, int], list[str], int]:
    """Returns (items, per_source_counts, errors, sources_worked)."""
    errors = errors if errors is not None else []
    sources = sources if sources is not None else SOURCES
    kurls = {_url_key(u) for u in known_urls}
    queues: dict[str, list[dict]] = {}
    worked = 0

    def run_list(nf):
        try:
            return nf[0], nf[1](fetch, today=today), None
        except Exception as e:
            return nf[0], None, e

    with ThreadPoolExecutor(max_workers=max(1, min(LIST_WORKERS, len(sources)))) as ex:
        listed = list(ex.map(run_list, sources))  # keeps source order; hosts are independent
    for name, stubs, err in listed:
        if err is not None:
            errors.append(f"{name}: list failed: {type(err).__name__}: {err}")
            continue
        if not stubs:
            errors.append(f"{name}: list returned 0 items")
            continue
        worked += 1
        q = []
        for st in stubs:
            if _is_blocked(st["source_url"]):
                continue
            if _url_key(st["source_url"]) in kurls or (st.get("title") and st["title"] in known_titles):
                continue
            dl = st.get("_deadline_iso")
            if dl and dl < today.isoformat():
                continue
            if st.get("text", "").startswith("[공모전]") and not _contest_field_ok(st["text"]):
                continue  # a contest outside the fields the user chose (policy contest_fields)
            q.append(st)
        queues[name] = q

    items: list[dict] = []
    counts: dict[str, int] = {}
    seen_titles: set[str] = set()
    seen_entry: set[str] = set()
    attempts = 0
    cap_default = max(3, int(max_items * CAP_FRACTION + 0.5))
    names = [n for n, _ in sources if n in queues]
    while len(items) < max_items and any(queues[n] for n in names) and attempts < max_items * 5:
        for n in names:
            if len(items) >= max_items:
                break
            if counts.get(n, 0) >= SOURCE_CAPS.get(n, cap_default):
                queues[n] = []
            if not queues[n]:
                continue
            st = queues[n].pop(0)
            attempts += 1
            nt = norm_title(st.get("title", ""))
            if nt and nt in seen_titles:
                continue
            enrich(fetch, st, errors)
            nt = norm_title(st.get("title", ""))
            if st.get("title") in known_titles or (nt and nt in seen_titles):
                continue
            cand_urls = [st["entry_url_guess"]] + st["links"]
            if any(u and _url_key(u) in kurls for u in cand_urls):
                continue
            if any(u and _url_key(u) in seen_entry and "instagram" not in u and "youtube" not in u
                   for u in cand_urls):
                continue
            if nt:
                seen_titles.add(nt)
            for u in cand_urls:
                if u:
                    seen_entry.add(_url_key(u))
            items.append(st)
            counts[n] = counts.get(n, 0) + 1
    items.sort(key=lambda x: x.get("posted") or "", reverse=True)
    return items, counts, errors, worked


FIELDS = ("source", "source_url", "title", "org", "deadline_hint", "entry_url_guess", "text", "links")


def to_output(item: dict) -> dict:
    return {k: item.get(k, [] if k == "links" else "") for k in FIELDS}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scout online events without an LLM.")
    ap.add_argument("--known", default="", help="known-events.txt (lines URL<TAB>title)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--today", default="")
    ap.add_argument("--eventhouse-flogin", action="store_true",
                    help="resolve eventhouse entry links with the site's flogin_no=1 parameter "
                         "(skips its login gate; off by default)")
    a = ap.parse_args(argv)
    today = dt.date.fromisoformat(a.today) if a.today else today_default()
    known_urls, known_titles = load_known(a.known)
    fetch = Fetcher()
    sources = list(SOURCES)
    if a.eventhouse_flogin:
        sources[0] = ("eventhouse", lambda fetch, today=None: src_eventhouse(fetch, today, flogin=True))
    t0 = time.time()
    items, counts, errors, worked = collect(fetch, known_urls, known_titles, a.max, today, sources)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump([to_output(i) for i in items], f, ensure_ascii=False, indent=2)
    for e in errors:
        print("ERROR " + e, file=sys.stderr)
    print(f"{len(items)} candidates {counts} in {time.time() - t0:.0f}s -> {a.out}", file=sys.stderr)
    return 0 if worked else 2


if __name__ == "__main__":
    sys.exit(main())
