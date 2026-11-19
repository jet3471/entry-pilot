"""States, lanes, transitions, deadline/timezone parsing, site_kind detection, eligibility, dedupe helpers."""
import re
import urllib.parse
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ep import config

STATES = ("new", "verifying", "ready", "submitting", "submitted", "failed", "owner", "manual", "excluded")
TERMINAL = ("submitted", "excluded")
LANES = config.LANES
EXPIRABLE = ("new", "verifying", "ready", "failed", "owner", "manual")  # `submitting` is never auto-expired
PRIVILEGED = ("owner", "system")

# action -> (allowed source states, target state). Target None = decided by the command (fail -> failed|manual).
TRANSITIONS = {
    "next-batch": ({"new"}, "verifying"),
    "verify": ({"new", "verifying", "owner"}, "ready"),
    "claim": ({"ready"}, "submitting"),
    "done": ({"submitting"}, "submitted"),
    "fail": ({"submitting"}, None),
    "release": ({"submitting", "failed", "owner"}, "ready"),
    "requeue": ({"owner", "manual", "excluded", "failed", "verifying"}, "new"),
    "owner-done": (set(STATES) - {"submitted"}, "submitted"),
    "exclude": ({"new", "verifying", "ready", "failed", "submitting"}, "excluded"),
    "owner": ({"new", "verifying", "ready", "failed", "submitting"}, "owner"),
    "manual": ({"new", "verifying", "ready", "failed", "submitting"}, "manual"),
    "expire": (set(EXPIRABLE), "excluded"),
}
# the owner and the system may also move owner/manual items to excluded/owner/manual
# (needed by sync-xlsx "안함" on a manual row and by owner housekeeping).
PRIVILEGED_EXTRA = {"exclude": {"owner", "manual"}, "owner": {"manual"}, "manual": {"owner"}}
# Actions on a `submitting` item that only the claiming agent (or owner/system) may take.
CLAIMER_ONLY = {"done", "fail", "exclude", "owner", "manual"}


def check_transition(action: str, item: dict, agent: str) -> str | None:
    """Return None if `agent` may apply `action` to `item`, else an English error message."""
    if action not in TRANSITIONS:
        return f"unknown action {action}"
    allowed, _ = TRANSITIONS[action]
    st = item["state"]
    if agent in PRIVILEGED:
        allowed = allowed | PRIVILEGED_EXTRA.get(action, set())
    if st not in allowed:
        return f"{item['id']} is {st}; {action} needs one of {sorted(allowed)}"
    if st == "submitting" and action in CLAIMER_ONLY and agent not in PRIVILEGED and item.get("claimed_by") != agent:
        return f"{item['id']} is submitting, claimed by {item.get('claimed_by')}; only that agent may {action} it"
    return None


def can_claim(agent: str, lane: str) -> bool:
    return lane in config.CAPABILITIES.get(agent, set())


# ---------- timezones and deadlines ----------
_FIXED = {"Asia/Seoul": 9, "Asia/Tokyo": 9}


def tzinfo(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # without tzdata fall back to fixed offsets; US Eastern uses standard time (-5).
        from datetime import timezone
        return timezone(timedelta(hours=_FIXED.get(name, -5)))


def region_tz(region: str) -> str:
    return config.REGIONS.get(region or config.DEFAULT_REGION, config.REGIONS["KR"])["tz"]


def item_tz(item: dict) -> str:
    return item.get("tz") or region_tz(item.get("region"))


def today_kst() -> date:
    return datetime.now(tzinfo(config.KST_TZ)).date()


def now_kst() -> datetime:
    return datetime.now(tzinfo(config.KST_TZ))


_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-4])\s*:\s*([0-5]\d)(?!\d)")
_PATTERNS = [
    # 2026-11-05, 2026.11.05, 2026/11/05, 2026. 11. 5., 2026년 11월 5일, 2026年11月5日, 2026-11-05T23:59
    ("ymd", re.compile(r"(?<!\d)(\d{4})\s*(?:[-./]|년|年)\s*(\d{1,2})\s*(?:[-./]|월|月)\s*(\d{1,2})(?!\d)")),
    # 11/05/2026 (US month/day/year)
    ("mdy", re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")),
    # Nov 5, 2026 / November 5 / Nov. 5th
    ("mon_d", re.compile(_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?(?!\d)", re.I)),
    # 5 November 2026
    ("d_mon", re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"(?:,?\s*(\d{4}))?", re.I)),
    # 11월 5일, 11月5日
    ("md_cjk", re.compile(r"(?<!\d)(\d{1,2})\s*[월月]\s*(\d{1,2})\s*[일日]?")),
    # 11-05, 11/05, 11.05, 11.5(수)
    ("md", re.compile(r"(?<!\d)(\d{1,2})\s*[-./]\s*(\d{1,2})(?![\d:])")),
]


def _infer_year(m: int, d: int, today: date) -> int | None:
    """Missing year -> the current KST year unless that date is already past by more than 300 days."""
    try:
        cand = date(today.year, m, d)
    except ValueError:
        try:
            date(today.year + 1, m, d)
            return today.year + 1
        except ValueError:
            return None
    return today.year + 1 if (today - cand).days > 300 else today.year


def parse_deadline(text, region: str = "KR", today: date | None = None) -> tuple[str, str]:
    """Return (YYYY-MM-DD or '', HH:MM or ''). Unparseable text -> ('', '') (callers keep the raw text).
    `region` is accepted for future region-specific formats; month-first ordering is used everywhere."""
    if text is None:
        return "", ""
    s = str(text).strip()
    if not s:
        return "", ""
    today = today or today_kst()
    for kind, rx in _PATTERNS:
        for mt in rx.finditer(s):
            g = mt.groups()
            try:
                if kind == "ymd":
                    y, m, d = int(g[0]), int(g[1]), int(g[2])
                elif kind == "mdy":
                    m, d, y = int(g[0]), int(g[1]), int(g[2])
                elif kind == "mon_d":
                    m, d, y = _MONTHS[g[0][:3].lower()], int(g[1]), int(g[2]) if g[2] else None
                elif kind == "d_mon":
                    d, m, y = int(g[0]), _MONTHS[g[1][:3].lower()], int(g[2]) if g[2] else None
                else:
                    m, d, y = int(g[0]), int(g[1]), None
                if not (1 <= m <= 12 and 1 <= d <= 31):
                    continue
                if y is None:
                    y = _infer_year(m, d, today)
                    if y is None:
                        continue
                dd = date(y, m, d)
            except (ValueError, KeyError):
                continue
            tm = _TIME.search(s, mt.end()) or _TIME.search(s)
            t = ""
            if tm:
                hh, mm = int(tm.group(1)), int(tm.group(2))
                t = "23:59" if hh == 24 else f"{hh:02d}:{mm:02d}"
            return dd.isoformat(), t
    return "", ""


def deadline_end(item: dict) -> datetime | None:
    """Aware datetime at which the item's deadline passes (deadline_time, else end of day) in the item's tz."""
    dl = (item.get("deadline") or "")[:10]
    if len(dl) != 10:
        return None
    try:
        d = date.fromisoformat(dl)
    except ValueError:
        return None
    t = time(23, 59, 59)
    dt_s = item.get("deadline_time") or ""
    if re.fullmatch(r"\d{2}:\d{2}", dt_s):
        t = time(int(dt_s[:2]) % 24, int(dt_s[3:]), 59 if dt_s[3:] == "59" else 0)
    return datetime.combine(d, t, tzinfo(item_tz(item)))


def is_expired(item: dict, now: datetime | None = None) -> bool:
    end = deadline_end(item)
    if end is None:
        return False
    return (now or now_kst()) > end


def order_key(item: dict):
    """Soonest deadline first; region rank KR<JP<US as tie-break; items without deadline last."""
    dl = (item.get("deadline") or "")[:10]
    rank = config.REGIONS.get(item.get("region") or "KR", {"rank": 9})["rank"]
    num = int(item["id"][1:]) if str(item.get("id", ""))[1:].isdigit() else 0
    return (0 if dl else 1, dl or "9999-99-99", rank, item.get("deadline_time") or "23:59", num)


# ---------- URLs, site kind, eligibility ----------
DROP_QS = re.compile(r"^(utm_|fbclid|gclid|ref|from|usp|fbzx|_ga|igsh|si$)", re.I)


def norm_url(u) -> str:
    """Normalize for duplicate detection: scheme/www/m. dropped, host lower-cased, Google Form endpoints unified,
    trailing slash and tracking query keys removed, remaining query sorted."""
    if not u:
        return ""
    u = str(u).strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I):
        u = "https://" + u
    p = urllib.parse.urlsplit(u)
    host = p.netloc.lower().split("@")[-1]
    host = re.sub(r":(80|443)$", "", host).removeprefix("www.").removeprefix("m.")
    path = re.sub(r"/(viewform|formResponse|edit)$", "", p.path).rstrip("/")
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not DROP_QS.match(k)]
    q = urllib.parse.urlencode(sorted(qs))
    return f"{host}{path}" + (f"?{q}" if q else "")


def url_host(u) -> str:
    if not u:
        return ""
    u = str(u).strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I):
        u = "https://" + u
    return urllib.parse.urlsplit(u).netloc.lower().split("@")[-1].split(":")[0]


def site_kind(url, form_type=None) -> str:
    host = url_host(url)
    path = urllib.parse.urlsplit(str(url) if "://" in str(url or "") else "https://" + str(url or "")).path
    for pat, kind in config.SITE_KIND_HOSTS:
        h, _, pfx = pat.partition("/")
        if host == h or host.endswith("." + h):
            if not pfx or path.lstrip("/").startswith(pfx):
                return kind
    ft = (form_type or "").lower()
    if ft in ("google", "google_form"):
        return "google_form"
    if ft in ("naver", "naver_form"):
        return "naver_form"
    if ft == "moaform":
        return "moaform"
    return "site_form"


def eligibility_from_text(text, region: str = "KR") -> str:
    s = str(text or "").lower()
    if any(p.lower() in s for p in config.ELIGIBILITY_RESIDENCY_PHRASES):
        return "residency_required"
    if any(p.lower() in s for p in config.ELIGIBILITY_OPEN_PHRASES):
        return "open"
    return "unknown"


def norm_title(org, title) -> str:
    return re.sub(r"[\W_]+", "", f"{org or ''}{title or ''}").lower()


def _grams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similar(org_a, title_a, org_b, title_b) -> bool:
    """Same organization and a similar title: one contains the other, or bigram Jaccard >= 0.4 (old rule)."""
    o, t = norm_title(org_a, ""), norm_title("", title_a)
    if len(o) < 2 or len(t) < 4 or norm_title(org_b, "") != o:
        return False
    t2 = norm_title("", title_b)
    if len(t2) < 4:
        return False
    g, g2 = _grams(t), _grams(t2)
    return t in t2 or t2 in t or len(g & g2) / max(1, len(g | g2)) >= 0.4


# ---------- value coercion (legacy and agent input) ----------
YES = {"1", "true", "yes", "y", "o", "예", "네", "있음", "전원"}
NO = {"0", "false", "no", "n", "x", "아니오", "아니요", "없음"}


def to_bool(v):
    """1/0/None from bools, numbers and yes/no words (English/Korean, e.g. '예'/'아니오'/'true')."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return 1 if v else 0
    s = str(v).strip().lower()
    if s in YES:
        return 1
    if s in NO:
        return 0
    return None


def to_int(v):
    """First integer in the value ('5,000원' -> 5000); None when there is none."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d[\d,]*", str(v))
    return int(m.group(0).replace(",", "")) if m else None


def signup_host(url) -> str:
    """Host for grouping sign-up sites: www./m./event./apps. prefixes stripped."""
    h = url_host(url)
    changed = True
    while changed:
        changed = False
        for pfx in ("www.", "m.", "event.", "apps."):
            if h.startswith(pfx) and h.count(".") > 1:
                h, changed = h[len(pfx):], True
    return h
