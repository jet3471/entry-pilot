"""Plain-Python scouting crawler for Korean contests (공모전).

Lists open contests from a few public sources, fetches each candidate's detail page
and writes a JSON list for an LLM judge. GET only. Never submits anything.
Every host's robots.txt is read first; a disallowed path is skipped, a 403 is not bypassed.

    python -X utf8 -m cp.crawler --known known-contests.txt --out cand.json [--max 60] [--today YYYY-MM-DD]

Each source is a small function ``src_<name>(fetch, today=...) -> list[stub]``. ``fetch(url)``
returns the page text and raises on failure. A stub is a dict with at least
source, source_url, title; optional org, deadline_hint, entry_url_guess, text,
posted (ISO date, used for newest-first sorting), links, "_deadline_iso" and "_detail"
(callable(BeautifulSoup, url) -> dict of field updates, run on the fetched detail page).
Contests whose deadline (from the list or from the detail page) is before today are dropped.
No eligibility filtering: region / age / school limits are written into text, not guessed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from bs4 import BeautifulSoup

from cp.crawler_chrome import src_instagram, src_navercafe, src_naverblog
from cp.urlpolicy import build_pinned_opener, is_public_http_url, resolve_public_http_url

UA = "Mozilla/5.0"
TIMEOUT = 20
HOST_DELAY = 1.0
MAX_REDIRECTS = 6
TEXT_MAX = 1800
MAX_LINKS = 8
BLOCKED_HOSTS: tuple = ()
# Hosts whose pages are useless to fetch as plain HTML (login walls / JS apps).
NO_DETAIL_HOSTS = ("blog.naver.com", "cafe.naver.com", "instagram.com", "youtube.com", "youtu.be", "facebook.com", "twitter.com", "x.com")
KST = dt.timezone(dt.timedelta(hours=9))

LINK_PATTERNS = (
    "forms.gle", "docs.google.com/forms", "form.naver.com", "naver.me", "moaform",
    "instagram.com", "youtube.com", "youtu.be", "blog.naver.com",
)
# Preference order when guessing the entry URL from found links.
ENTRY_PREFERENCE = (
    "forms.gle", "docs.google.com/forms", "form.naver.com", "moaform",
    "naver.me", "blog.naver.com", "instagram.com", "youtube.com", "youtu.be",
)
SELF_LINKS = ("instagram.com/contestkorea", "instagram.com/wevity", "instagram.com/linkareer")


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


def robots_allows(robots_txt: str, url: str, agent: str = "*") -> bool:
    """True when robots.txt text allows ``url`` for ``agent`` (stdlib parser; empty text allows all)."""
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots_txt.splitlines())
    return rp.can_fetch(agent, url)


class _PolicyBlockedError(RuntimeError):
    pass


class Fetcher:
    """GET-only fetcher with per-host delay and robots.txt check. Callable: fetch(url) -> text."""

    def __init__(self, delay: float = HOST_DELAY, timeout: int = TIMEOUT, check_robots: bool = True):
        self.delay = delay
        self.timeout = timeout
        self.check_robots = check_robots
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._robots: dict[str, str] = {}
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
            raise _PolicyBlockedError("blocked host: " + _host(url))
        ok, reason, pinned_ips = resolve_public_http_url(url)
        if not ok:
            raise _PolicyBlockedError(f"blocked url ({reason}): {url}")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"}, method="GET")
        req._pinned_ips = tuple(pinned_ips)
        return req

    def _open_checked(self, url: str, limit: int, check_robots: bool) -> str:
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            req = self._req(cur)
            if check_robots and not self._robots_ok(cur):
                raise RuntimeError("robots.txt disallows: " + cur)
            self._wait(cur)
            try:
                with self._noredir.open(req, timeout=self.timeout) as r:
                    final_url = r.geturl() if hasattr(r, "geturl") else cur
                    if final_url and final_url != cur:
                        self._req(final_url)
                    raw = r.read(limit)
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

    def _robots_ok(self, url: str) -> bool:
        if not self.check_robots:
            return True
        sp = urllib.parse.urlsplit(url)
        key = f"{sp.scheme}://{sp.netloc}"
        with self._lock:
            txt = self._robots.get(key)
        if txt is None:
            try:
                txt = self._open_checked(key + "/robots.txt", 300_000, check_robots=False)
            except _PolicyBlockedError:
                raise
            except Exception:
                txt = ""  # no readable robots.txt: nothing is disallowed
            with self._lock:
                self._robots[key] = txt
        return robots_allows(txt, url)

    def __call__(self, url: str) -> str:
        return self._open_checked(url, 3_000_000, check_robots=True)


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
    """'11/20' or '11.20' -> ISO date, choosing the year that puts it within ~6 months of today."""
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


def ms_to_iso(ms) -> str:
    """Epoch milliseconds -> ISO date in KST ('' when missing)."""
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, KST).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _generic_detail(s: BeautifulSoup, url: str) -> dict:
    body = s.body or s
    return {"text": visible_text(body), "links": find_links(body, url)}


def _json_script(s: BeautifulSoup, script_id: str) -> dict:
    tag = s.find("script", id=script_id)
    if not tag:
        return {}
    try:
        return json.loads(tag.string or tag.get_text() or "{}")
    except ValueError:
        return {}


# ---------------------------------------------------------------- sources
# Every src_* takes fetch(url)->str and returns stubs, newest first.

WV_BASE = "https://www.wevity.com/"
WV_LIST = WV_BASE + "?c=find&s=1&mode=ing&gub=1&gp={p}"


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
    text = re.split(r"본 정보의 사실 여부를 보증하지", text)[0]
    facts = []
    for k in ("응모대상", "총", "1등"):  # 응모대상, 총 상금, 1등 상금
        for kk, v in info.items():
            if kk == k and v:
                facts.append(f"{kk} {v}")
    upd["text"] = clean(" | ".join(facts) + " | " + text) if facts else text
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


def src_wevity(fetch, today: dt.date | None = None, pages: int = 3) -> list[dict]:
    """wevity.com 공모전 (robots allows all), 접수중 list pages."""
    today = today or today_default()
    out: list[dict] = []
    seen = set()
    for p in range(1, pages + 1):
        for it in wevity_parse_list(fetch(WV_LIST.format(p=p)), today):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    out.sort(key=lambda x: -x["_order"])
    return out


LK_BASE = "https://linkareer.com"
LK_LIST = LK_BASE + "/list/contest?filterType=CATEGORY&orderBy_direction=DESC&orderBy_field=CREATED_AT&page={p}"


def _apollo(s: BeautifulSoup) -> dict:
    return (_json_script(s, "__NEXT_DATA__").get("props", {}).get("pageProps", {}) or {}).get("__APOLLO_STATE__", {}) or {}


def linkareer_parse_list(html: str, today: dt.date) -> list[dict]:
    """linkareer.com/list/contest: the page is a Next.js app; the list lives in __NEXT_DATA__ (Apollo cache)."""
    ap = _apollo(soup_of(html))
    out = []
    for key, a in ap.items():
        if not key.startswith("Activity:") or not a.get("title"):
            continue
        aid = int(a.get("id") or key.split(":")[1])
        dl = ms_to_iso(a.get("recruitCloseAt"))
        org = clean(a.get("organizationName") or "")
        out.append({
            "source": "linkareer", "source_url": f"{LK_BASE}/activity/{aid}", "title": clean(a["title"]), "org": org,
            "deadline_hint": dl, "entry_url_guess": "", "_deadline_iso": dl, "_order": aid,
            "text": clean(f"[공모전] [{org}] {a['title']}. 마감 {dl}"), "_detail": _lk_detail,
        })
    return out


def _lk_detail(s: BeautifulSoup, url: str) -> dict:
    ap = _apollo(s)
    m = re.search(r"/activity/(\d+)", url)
    a = ap.get(f"Activity:{m.group(1)}") if m else None
    if not a:
        return {}

    def names(field):
        res = []
        for r in a.get(field) or []:
            ref = (r or {}).get("__ref")
            n = (ap.get(ref) or {}).get("name") if ref else ""
            if n:
                res.append(n)
        return res

    dt_ref = (a.get("detailText") or {}).get("__ref")
    body_html = (ap.get(dt_ref) or {}).get("text", "") if dt_ref else ""
    body = soup_of(body_html)
    bodytxt = visible_text(body)
    facts = []
    if names("targets"):
        facts.append("참가대상 " + ", ".join(names("targets")))
    if names("regions"):
        facts.append("지역 " + ", ".join(names("regions")))
    if a.get("organizationType"):
        facts.append("기관유형 " + str(a["organizationType"]))
    if names("benefits"):
        facts.append("혜택 " + ", ".join(names("benefits")))
    if a.get("tenThousandUnitOfReward"):
        facts.append(f"상금/보상 {a['tenThousandUnitOfReward']}만원")
    if a.get("additionalBenefit"):
        facts.append("추가혜택 " + clean(str(a["additionalBenefit"])))
    upd: dict = {"text": clean(" | ".join(facts) + " | " + bodytxt) if facts else bodytxt}
    start, end = ms_to_iso(a.get("recruitStartAt")), ms_to_iso(a.get("recruitCloseAt"))
    if end:
        upd["_deadline_iso"] = end
        upd["deadline_hint"] = f"{start} ~ {end}" if start else end
    if start:
        upd["posted"] = start
    links = find_links(body, url)
    home = a.get("homepageURL") or ""
    if home.startswith("http"):
        upd["entry_url_guess"] = home
        links.insert(0, home)
    upd["links"] = links[:MAX_LINKS]
    return upd


def src_linkareer(fetch, today: dt.date | None = None, pages: int = 2) -> list[dict]:
    """linkareer.com 공모전 list (robots allow /list/contest and /activity/)."""
    today = today or today_default()
    out: list[dict] = []
    seen = set()
    for p in range(1, pages + 1):
        for it in linkareer_parse_list(fetch(LK_LIST.format(p=p)), today):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    out.sort(key=lambda x: -x["_order"])
    return out


CP_BASE = "https://www.campuspick.com"


def campuspick_parse_list(html: str, today: dt.date) -> list[dict]:
    """campuspick.com/contest: the visible app is JS, the crawlable list is the hidden #seo-fallback block.
    No dates on the list; the detail page (window state JSON) carries start/end date and prize."""
    s = soup_of(html)
    out = []
    for a in s.select("#seo-fallback li a[href*='/contest/view']"):
        m = re.search(r"id=(\d+)", a["href"])
        if not m:
            continue
        h3 = a.find("h3")
        title = clean((h3 or a).get_text(" "))
        p = a.find("p")
        org = clean(p.get_text(" ")) if p else ""
        out.append({
            "source": "campuspick", "source_url": f"{CP_BASE}/contest/view?id={m.group(1)}", "title": title, "org": org,
            "deadline_hint": "", "entry_url_guess": "", "_deadline_iso": "", "_order": int(m.group(1)),
            "text": clean(f"[공모전] [{org}] {title}"), "_detail": _cp_detail,
        })
    return out


def _cp_detail(s: BeautifulSoup, url: str) -> dict:
    st = _json_script(s, "__INITIAL_STATE__")
    a = st.get("activity") or {}
    if not a:
        return {}
    upd: dict = {}
    facts = []
    if a.get("prize_top"):
        facts.append(f"1등 상금 {int(a['prize_top']):,}원")
    if a.get("region") and a["region"] != "null":
        facts.append(f"지역 {a['region']}")
    desc = str(a.get("description") or "")
    upd["text"] = clean(" | ".join(facts) + " | " + desc) if facts else clean(desc)
    sd, ed = a.get("start_date") or "", a.get("end_date") or ""
    if re.match(r"\d{4}-\d{2}-\d{2}$", ed):
        upd["_deadline_iso"] = ed
        upd["deadline_hint"] = f"{sd} ~ {ed}"
    if re.match(r"\d{4}-\d{2}-\d{2}$", sd):
        upd["posted"] = sd
    if a.get("company"):
        upd["org"] = clean(a["company"])[:60]
    links = find_links(soup_of(f"<p>{desc}</p>"), url)
    site = a.get("website") or ""
    if site.startswith("http"):
        upd["entry_url_guess"] = site
        links.insert(0, site)
    upd["links"] = links[:MAX_LINKS]
    return upd


def src_campuspick(fetch, today: dt.date | None = None) -> list[dict]:
    """campuspick.com (에브리커리어) 공모전; robots disallow only login/download/linkclick paths."""
    today = today or today_default()
    out = campuspick_parse_list(fetch(CP_BASE + "/contest"), today)
    out.sort(key=lambda x: -x["_order"])
    return out


CK_BASE = "https://www.contestkorea.com"
CK_LIST = CK_BASE + "/sub/list.php?int_gbn=1&Txt_sortkey=a.int_sort&Txt_sortword=desc&page={p}"


def contestkorea_parse_list(html: str, today: dt.date) -> list[dict]:
    """contestkorea.com 대회·공모전 전체 현황 (server-rendered cards with 접수 period and D-day)."""
    s = soup_of(html)
    out = []
    for li in s.select("div.list_style_2 ul > li"):
        a = li.select_one("div.title a[href*='str_no=']")
        if not a:
            continue
        no = re.search(r"str_no=(\d+)", a["href"])
        bcode = re.search(r"Txt_bcode=(\d+)", a["href"])
        if not no:
            continue
        cat = clean(a.select_one(".category").get_text(" ")) if a.select_one(".category") else ""
        title = clean(a.select_one(".txt").get_text(" ")) if a.select_one(".txt") else clean(a.get_text(" "))
        host = li.select_one("ul.host li.icon_1")
        target = li.select_one("ul.host li.icon_2")
        org = clean(re.sub(r"^주최\s*\.?\s*", "", host.get_text(" "))) if host else ""
        tg = clean(re.sub(r"^대상\s*\.?\s*", "", target.get_text(" "))) if target else ""
        tg = clean(re.sub(r"\s*,\s*", ", ", tg))
        step = li.select_one(".step-1")
        period = clean(re.sub(r"^접수\s*", "", step.get_text(" "))) if step else ""
        dday = clean(li.select_one(".d-day .day").get_text(" ")) if li.select_one(".d-day .day") else ""
        cond = clean(li.select_one(".d-day .condition").get_text(" ")) if li.select_one(".d-day .condition") else ""
        dl = ""
        rng = re.search(r"~\s*(\d{1,2}\.\d{1,2})", period)
        if rng:
            dl = parse_md(rng.group(1), today)
        dm = re.search(r"D-(\d+)", dday)
        if dm:  # D-day is the more reliable value when present
            dl = (today + dt.timedelta(days=int(dm.group(1)))).isoformat()
        elif "D+" in dday or cond in ("마감", "접수마감"):
            dl = (today - dt.timedelta(days=1)).isoformat()
        url = f"{CK_BASE}/sub/view.php?int_gbn=1&Txt_bcode={bcode.group(1) if bcode else ''}&str_no={no.group(1)}"
        out.append({
            "source": "contestkorea", "source_url": url, "title": title, "org": org,
            "deadline_hint": clean(f"접수 {period} {dday}"), "entry_url_guess": "", "_deadline_iso": dl,
            "_order": int(no.group(1)),
            "text": clean(f"[공모전] [{cat}] [{org}] {title}. 참가대상 {tg}. 접수 {period} {dday} {cond}"),
            "_detail": _ck_detail,
        })
    return out


def _ck_detail(s: BeautifulSoup, url: str) -> dict:
    top = s.select_one(".view_top_area") or s
    info = {}
    for tr in top.select("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            info[clean(th.get_text())] = clean(td.get_text(" "))
    body = s.select_one(".view_detail_area") or s.select_one(".view_cont_area") or s.body or s
    bodytxt = visible_text(body)
    facts = [f"{k} {info[k]}" for k in ("참가대상", "대회지역", "시상내역", "참가비용") if info.get(k)]
    upd: dict = {"text": clean(" | ".join(facts) + " | " + bodytxt) if facts else bodytxt}
    period = info.get("접수기간", "")
    if period:
        today = today_default()
        upd["deadline_hint"] = period
        upd["_deadline_iso"] = last_date(period, today)
        upd["posted"] = first_date(period, today)
    if info.get("주최.주관") or info.get("주최 . 주관"):
        upd["org"] = (info.get("주최.주관") or info.get("주최 . 주관"))[:60]
    links = find_links(body, url)
    for a in top.select("a[href^='http']"):
        if _host(a["href"]) not in ("www.contestkorea.com", "contestkorea.com") and "javascript" not in a["href"]:
            upd["entry_url_guess"] = a["href"]
            links.insert(0, a["href"])
            break
    upd["links"] = links[:MAX_LINKS]
    return upd


def src_contestkorea(fetch, today: dt.date | None = None, pages: int = 2) -> list[dict]:
    """contestkorea.com 전체 현황 (robots allow all)."""
    today = today or today_default()
    out: list[dict] = []
    seen = set()
    for p in range(1, pages + 1):
        for it in contestkorea_parse_list(fetch(CK_LIST.format(p=p)), today):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    out.sort(key=lambda x: -x["_order"])
    return out


TC_BASE = "https://www.thinkcontest.com"
TC_LIST = TC_BASE + "/thinkgood/user/contest/index.do"


def thinkcontest_parse_list(html: str, today: dt.date) -> list[dict]:
    """thinkcontest.com 공모전 index: the crawlable list is the schema.org ItemList carousel (title + contest_pk)."""
    out, seen = [], set()
    for m in re.finditer(r'"name":\s*"([^"]+)",\s*"image":\s*"[^"]*",\s*"url":\s*"[^"]*contest_pk=(\d+)"', html):
        pk = int(m.group(2))
        if pk in seen:
            continue
        seen.add(pk)
        title = clean(m.group(1))
        out.append({
            "source": "thinkcontest", "source_url": f"{TC_BASE}/thinkgood/user/contest/view.do?contest_pk={pk}",
            "title": title, "org": "", "deadline_hint": "", "entry_url_guess": "", "_deadline_iso": "",
            "_order": pk, "text": f"[공모전] {title}", "_detail": _tc_detail,
        })
    return out


def _tc_detail(s: BeautifulSoup, url: str) -> dict:
    root = s.select_one("div.content-detail") or s.body or s
    info = {}
    for row in root.select("div.info > div"):
        k, v = row.select_one(".tit"), row.select_one(".txt")
        if k and v:
            info[clean(k.get_text())] = clean(v.get_text(" "))
    body = root
    bodytxt = visible_text(body)
    facts = [f"{k} {info[k]}" for k in ("참가자격", "시상종류") if info.get(k)]
    upd: dict = {"text": clean(" | ".join(facts) + " | " + bodytxt) if facts else bodytxt}
    period = info.get("접수기간", "")
    if period:
        today = today_default()
        upd["deadline_hint"] = period
        upd["_deadline_iso"] = last_date(period, today)
        upd["posted"] = first_date(period, today)
    if info.get("주최"):
        upd["org"] = info["주최"][:60]
    links = find_links(body, url)
    for a in root.select("div.info a[href^='http']"):
        upd["entry_url_guess"] = a["href"]
        links.insert(0, a["href"])
        break
    upd["links"] = links[:MAX_LINKS]
    return upd


def src_thinkcontest(fetch, today: dt.date | None = None) -> list[dict]:
    """thinkcontest.com (씽굿) 공모전; deadlines come from the detail pages."""
    today = today or today_default()
    out = thinkcontest_parse_list(fetch(TC_LIST), today)
    out.sort(key=lambda x: -x["_order"])
    return out


SOURCES: list[tuple[str, Callable]] = [
    ("wevity", src_wevity),
    ("linkareer", src_linkareer),
    ("campuspick", src_campuspick),
    ("contestkorea", src_contestkorea),
    ("thinkcontest", src_thinkcontest),
    ("naverblog", src_naverblog),
    ("navercafe", src_navercafe),
    ("instagram", src_instagram),
]

# Per-source cap on items taken in one run (default: CAP_FRACTION of --max) so one big
# source cannot fill the whole list. Noisy / low-yield sources get a smaller fixed cap.
CAP_FRACTION = 0.4
SOURCE_CAPS = {"wevity": 14, "linkareer": 12, "campuspick": 10, "contestkorea": 14, "thinkcontest": 6,
               "naverblog": 10, "navercafe": 6, "instagram": 6}
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
    if not stub.get("entry_url_guess"):
        stub["entry_url_guess"] = guess_entry(stub["links"])
    stub["text"] = text[:TEXT_MAX]


def _contest_field_ok(text: str) -> bool:
    from cp import policy
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
            if not _contest_field_ok(st.get("text") or st.get("title") or ""):
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
            dl = st.get("_deadline_iso")
            if dl and dl < today.isoformat():  # deadline only known from the detail page
                continue
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
    ap = argparse.ArgumentParser(description="Scout open Korean contests (공모전) without an LLM.")
    ap.add_argument("--known", default="", help="known-contests.txt (lines URL<TAB>title)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--today", default="")
    a = ap.parse_args(argv)
    today = dt.date.fromisoformat(a.today) if a.today else today_default()
    known_urls, known_titles = load_known(a.known)
    fetch = Fetcher()
    sources = list(SOURCES)
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
