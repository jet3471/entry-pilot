"""User-facing layer: Korean status page, full list, xlsx of what the user has to do.

  ep render [--no-xlsx] [--dry-run]

Writes, atomically (temp file in the same folder, then os.replace; nothing is ever deleted):
  config.owner_html_path()      현황판.html   compact, Korean, no JS, inline CSS only
  config.full_list_html_path()  전체목록.html  every non-excluded item + a collapsed excluded section (local only)
  config.owner_xlsx_path()      직접_참여_목록.xlsx  sheets "직접 참여" / "확인 필요" (+ "설명")

Privacy rule: the pages carry no personal data, no evidence text, no log paths, no secrets. Free text that is shown
(reasons, questions, error messages, win notes) goes through scrub() (emails, phone numbers, @handles, Windows paths,
long tokens and the values of the profile's identity fields are removed) before it is translated or printed. Evidence,
verify_notes, legacy_notes and notes are never rendered.

The footer time ("생성") is the time the page CONTENT last changed, not the time of the last render: when the content is
identical to the previous render the old time is reused, so the file hash stays stable.
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import date, timedelta

from ep import cli, config, model, store, translate

MAX_ROWS = 40                 # rows per section on the compact page
MAX_ERRORS = 20
MAX_TRANSLATE_CALLS = 3       # model calls per render; the rest is shown with " (EN)" until the next run
SOON_DAYS = 7
REDACTED = "[…]"  # no Hangul, so a scrubbed English text is still translated

NEED_KO = {"login": "로그인/가입 필요", "identity": "본인인증", "payment": "결제", "sns": "SNS 계정",
           "upload": "사진/파일 업로드", "app": "앱 필요", "kakao": "카카오톡", "residency": "현지 주소 필요",
           "local_phone": "현지 번호 필요", "other": "기타"}
STATE_KO = {"submitted": "제출 완료", "submitting": "응모 중", "ready": "응모 예정", "verifying": "확인 중",
            "new": "확인 중", "owner": "확인 필요", "manual": "직접 참여", "failed": "응모 실패", "excluded": "제외"}
PENDING_STATES = ("new", "verifying", "ready", "submitting", "failed", "owner", "manual")  # not entered, not excluded
IN_PROGRESS_STATES = ("new", "verifying", "ready", "submitting")  # what "진행 중" counts
# Counts are split by country, always in this order (한국, 미국, 일본), 0 shown too.
REGION_ORDER = ("KR", "US", "JP")
REGION_KO = {"KR": "한국", "US": "미국", "JP": "일본"}
TODAY_DEADLINE_STATES = ("new", "verifying", "ready", "submitting", "owner", "manual")  # "오늘 마감" states
FOOTER_GENERATED = "@@GENERATED@@"

# ---------- privacy scrub ----------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\d-])(?:\+?82[-. ]?)?0?1[016789][-. ]?\d{3,4}[-. ]?\d{4}(?![\d-])")
_HANDLE = re.compile(r"(?<![\w/.])@[A-Za-z0-9_.]{3,}")
_PATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+")
_TOKEN = re.compile(r"(?<![\w-])[A-Za-z0-9_\-]{32,}(?![\w-])")
_PROFILE_KEYS = ("name", "phone", "email", "address", "birth", "handle", "account", "channel", "postcode")
KNOWN_ACCOUNT_IDS = ("entry-pilot-placeholder-id",)  # Entry Pilot: the user's handles come from profile.json
_KNOWN_RX = re.compile("@?(?:" + "|".join(re.escape(x) for x in sorted(KNOWN_ACCOUNT_IDS, key=len, reverse=True)) + ")",
                       re.I)
_sensitive_cache: list | None = None


def _profile_values() -> list:
    """Identity strings of the profile (>= 4 chars) to remove from shown text. Never printed or stored."""
    global _sensitive_cache
    if _sensitive_cache is not None:
        return _sensitive_cache
    vals = []
    try:
        from ep import profile
        p = profile.load()

        def walk(prefix, d):
            for k, v in d.items():
                key = f"{prefix}.{k}".lower()
                if isinstance(v, dict):
                    walk(key, v)
                elif isinstance(v, (str, int)) and any(w in key for w in _PROFILE_KEYS):
                    s = str(v).strip()
                    if len(s) >= 4:
                        vals.append(s)
        walk("", p)
    except Exception:  # no profile file / unreadable: the regexes still apply (not an error for rendering)
        pass
    _sensitive_cache = sorted(set(vals), key=len, reverse=True)
    return _sensitive_cache


def scrub(text) -> str:
    s = "" if text is None else str(text)
    for v in _profile_values():
        if v in s:
            s = s.replace(v, REDACTED)
    s = _KNOWN_RX.sub(REDACTED, s)
    s = _EMAIL.sub(REDACTED, s)
    s = _PATH.sub(REDACTED, s)
    s = _PHONE.sub(REDACTED, s)
    s = _HANDLE.sub(REDACTED, s)
    s = _TOKEN.sub(REDACTED, s)
    return s


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def safe_url(u) -> str:
    """http(s) URL that survives scrub() unchanged, else ''."""
    u = str(u or "").strip()
    if not re.match(r"^https?://", u, re.I) or scrub(u) != u:
        return ""
    return u


def link(u, label="링크 ↗") -> str:
    u = safe_url(u)
    return f'<a href="{esc(u)}" target="_blank" rel="noopener noreferrer">{esc(label)}</a>' if u else ""


# ---------- data ----------
class RenderError(Exception):
    pass


def _need(res: dict, what: str) -> dict:
    if not res.get("ok"):
        raise RenderError(f"{what} failed: {res.get('error')}")
    return res


def gather() -> dict:
    """Read everything the pages need. status/wins/signups come from the board commands (cli.call)."""
    status = _need(cli.call(["status"]), "ep status")
    wins = _need(cli.call(["wins"]), "ep wins")["wins"]
    signups = _need(cli.call(["signups"]), "ep signups")
    db = store.connect()
    try:
        items = store.items(db)
        pool = store.pool_rows(db, "promoted_to IS NULL OR promoted_to=''")
        gen = store.kv_get(db, "render_gen") or {}
        xsig = store.kv_get(db, "xlsx_sig")
        pub_hash = store.kv_get(db, "render_hash")
        tried = {r[0] for r in db.execute("SELECT DISTINCT item_id FROM history WHERE action='backfill'")}
    finally:
        db.close()
    return {"status": status, "wins": wins, "signups": signups, "items": items, "pool": pool,
            "render_gen": gen if isinstance(gen, dict) else {}, "xlsx_sig": xsig, "render_hash": pub_hash,
            "backfill_tried": tried}


def need_text(it: dict, tr: dict) -> str:
    """What the user has to do for a manual/owner item, in Korean."""
    if it["state"] == "owner":
        q = scrub(it.get("question"))
        return "확인 필요: " + tr.get(q, q) if q else "확인 필요"
    label = NEED_KO.get(it.get("need") or "other", NEED_KO["other"])
    r = scrub(it.get("reason"))
    return label + (": " + tr.get(r, r) if r else "")


def owner_items(items: list) -> list:
    rows = [i for i in items if i["state"] in ("owner", "manual")]
    rows.sort(key=model.order_key)
    return rows


def free_texts(data: dict) -> list:
    """Scrubbed English-or-Korean free text the compact page and the xlsx show (cached_many drops the Korean ones)."""
    out = []
    for it in owner_items(data["items"]):
        out.append(scrub(it.get("question") if it["state"] == "owner" else it.get("reason")))
    out += [scrub(w.get("note")) for w in data["wins"]]
    out += [scrub(e.get("message")) for e in data["status"]["open_errors"][-MAX_ERRORS:]]
    out += [scrub(v) for v in data["status"]["blocked"].values()]
    return [s for s in out if s.strip()]


def full_texts(data: dict) -> list:
    out = []
    for it in data["items"]:
        for col in ("reason", "question"):
            if it.get(col):
                out.append(scrub(it[col]))
    return [s for s in out if s.strip()]


# ---------- formatting helpers ----------
def won(n) -> str:
    return f"{int(n):,}원"


def day_label(dl: str, today: date) -> str:
    dl = (dl or "")[:10]
    if len(dl) != 10:
        return "-"
    try:
        d = (date.fromisoformat(dl) - today).days
    except ValueError:
        return esc(dl)
    tag = "오늘" if d == 0 else f"D-{d}" if d > 0 else f"D+{-d}"
    return f"{dl[5:]} ({tag})"


def min_reward(it: dict) -> str:
    v = it.get("prize_min_value")
    if it.get("all_rewarded") == 1 and v is not None:
        return won(v)
    return "추첨" + (f" ({won(v)})" if v is not None else "")


def table(head: list, rows: list, cls: str = "") -> str:
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="w"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def more(total: int, shown: int, hint: str) -> str:
    return f'<p class="more">외 {total - shown}건 ({esc(hint)})</p>' if total > shown else ""


def title_cell(it: dict) -> str:
    t = esc(clip(scrub(it.get("title") or it.get("title_en") or "(제목 없음)"), 70))
    org = scrub(it.get("org"))
    return f'<span class="id">{esc(it["id"])}</span> {t}' + (f'<br><span class="mut">{esc(clip(org, 40))}</span>' if org else "")


CSS = """
:root{--bg:#f6f8fa;--s:#fff;--b:#e2e8f0;--t:#1e293b;--m:#64748b;--p:#2563eb;--g:#15803d;--r:#b91c1c;--a:#b45309;--pu:#6d28d9}
*{box-sizing:border-box}
body{font-family:'Pretendard','Malgun Gothic',-apple-system,sans-serif;margin:0;padding:12px 14px 40px;background:var(--bg);color:var(--t);font-size:13.5px;line-height:1.45}
a{color:var(--p);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:19px;margin:0}h2{font-size:15px;margin:0 0 6px}h2 small{font-weight:400;color:var(--m);font-size:12px}
.top{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:6px;margin-bottom:8px}
.sub{color:var(--m);font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px;margin-bottom:8px}
.card{background:var(--s);border:1px solid var(--b);border-radius:9px;padding:6px 9px}
.card .l{font-size:11.5px;color:var(--m)}.card .n{font-size:19px;font-weight:800;line-height:1.15}
.card .d{font-size:11px;color:var(--m)}
.card.wide{grid-column:1/-1}
.n.g{color:var(--g)}.n.r{color:var(--r)}.n.pu{color:var(--pu)}.n.a{color:var(--a)}
nav{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
nav a{background:var(--s);border:1px solid var(--b);border-radius:999px;padding:2px 9px;font-size:12px;color:var(--t)}
section{background:var(--s);border:1px solid var(--b);border-radius:11px;padding:9px 11px;margin-bottom:10px}
.w{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{background:#f8fafc;color:#475569;font-size:12px;text-align:left;padding:5px 6px;border-bottom:1.5px solid var(--b);white-space:nowrap}
td{border-bottom:1px solid #f1f5f9;padding:5px 6px;vertical-align:top;overflow-wrap:anywhere}
.id{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;background:#f1f5f9;border-radius:4px;padding:0 4px}
.mut{color:var(--m);font-size:12px}.more{color:var(--m);font-size:12px;margin:6px 0 0}
.tot{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:6px 9px;margin-bottom:6px;font-size:13px}
.days{display:grid;grid-template-columns:repeat(auto-fit,minmax(92px,1fr));gap:6px}
.day{border:1px solid var(--b);border-radius:8px;padding:5px 8px}.day.u{border-color:#ef4444;background:#fef2f2}
.day b{font-size:12px}.day div{font-size:12px;color:var(--m)}
.big{font-size:15px;padding:9px 12px}.big b{font-size:24px;color:var(--g)}
.btn{display:inline-block;background:var(--p);color:#fff;border-radius:7px;padding:4px 12px;font-weight:700;white-space:nowrap}
.btn:hover{text-decoration:none;opacity:.9}
.err{color:var(--r)}.ok{color:var(--g)}
details{margin-top:10px;background:var(--s);border:1px solid var(--b);border-radius:11px;padding:8px 11px}
summary{cursor:pointer;font-weight:700}
footer{color:var(--m);font-size:12px;margin-top:10px}
@media(max-width:600px){body{padding:8px 8px 30px;font-size:13px}.card .n{font-size:17px}}
"""


def page(title: str, body: str) -> str:
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow,noarchive">'
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{body}"
            f"<footer>생성 {FOOTER_GENERATED} · Entry Pilot</footer></body></html>\n")


# ---------- compact page ----------
def by_region(rows) -> dict:
    """{KR, US, JP: count} in display order; every region is present (0 when none). Unknown region counts as KR."""
    out = {r: 0 for r in REGION_ORDER}
    for i in rows:
        r = i.get("region") or "KR"
        out[r] = out.get(r, 0) + 1
    return out


def region_line(counts: dict) -> str:
    """'한국 N · 미국 N · 일본 N' (always all three, in this order)."""
    return " · ".join(f"{REGION_KO[r]} {counts.get(r, 0)}" for r in REGION_ORDER)


def due_today(items: list, today: date) -> list:
    """Open items (TODAY_DEADLINE_STATES) whose deadline date is today (KST date, compared as text like
    deadline_counts) and that have not passed their deadline time yet (an expired one is not 'due')."""
    t = today.isoformat()
    return [i for i in items if i["state"] in TODAY_DEADLINE_STATES and (i.get("deadline") or "")[:10] == t
            and not model.is_expired(i)]


def stats(data: dict, today: date) -> dict:
    items, st = data["items"], data["status"]
    today_s = today.isoformat()
    submitted = [i for i in items if i["state"] == "submitted"]
    today_sub = [i for i in submitted if (i.get("submitted_at") or "")[:10] == today_s]
    states = {s: 0 for s in model.STATES}
    for i in items:
        states[i["state"]] = states.get(i["state"], 0) + 1
    progress = [i for i in items if i["state"] in IN_PROGRESS_STATES]
    todo = owner_items(items)  # same list section_todo shows, so header count and list agree
    due = due_today(items, today)
    return {"submitted": len(submitted), "today": len(today_sub),
            "by_region": by_region(submitted), "today_by_region": by_region(today_sub), "states": states,
            "in_progress": len(progress), "in_progress_by_region": by_region(progress),
            "todo": len(todo), "todo_by_region": by_region(todo),
            "due_today": len(due), "due_today_by_region": by_region(due), "wins": len(data["wins"]),
            "blocked": sorted(st["blocked"]), "pool_open": len(data["pool"])}


def section_todo(data, today, tr) -> str:
    rows = owner_items(data["items"])
    shown = rows[:MAX_ROWS]
    trs = [[esc(day_label(it.get("deadline"), today)), title_cell(it), esc(clip(need_text(it, tr), 160)),
            link(it.get("url") or it.get("source_url"))] for it in shown]
    body = table(["마감", "이벤트", "해야 할 일", "링크"], trs) if trs else '<p class="mut">지금 내가 할 일이 없습니다.</p>'
    return (f'<section id="todo"><h2>1. 내가 할 일 <small>{len(rows)}건 · 마감 빠른 순</small></h2>{body}'
            f'{more(len(rows), len(shown), "직접_참여_목록.xlsx 에 전부 있습니다")}</section>')


def section_today(data, today) -> str:
    today_s = today.isoformat()
    rows = [i for i in data["items"] if i["state"] == "submitted" and (i.get("submitted_at") or "")[:10] == today_s]
    rows.sort(key=lambda i: (i.get("submitted_at") or "", i["id"]))
    shown = rows[-MAX_ROWS:]
    trs = [[esc((i.get("submitted_at") or "")[11:16]), title_cell(i), esc(REGION_KO.get(i.get("region") or "KR", i.get("region")))] for i in shown]
    body = table(["시각", "이벤트", "지역"], trs) if trs else '<p class="mut">오늘 제출한 응모가 아직 없습니다.</p>'
    return (f'<section id="today"><h2>2. 오늘 제출 <small>{today_s} · {len(rows)}건</small></h2>{body}'
            f'{more(len(rows), len(shown), "전체목록.html 참고")}</section>')


def reward_rows(items: list, today: date) -> list:
    """Entered items with a prize: upcoming announcements first (soonest), then no date, then past (latest first)."""
    t = today.isoformat()
    ent = [i for i in items if i["state"] == "submitted" and (i.get("prize") or i.get("prize_min_value") is not None)]
    keyed = [(model.parse_deadline(i.get("result_date"), i.get("region") or "KR", today)[0], i) for i in ent]
    up = sorted([k for k in keyed if k[0] and k[0] >= t], key=lambda k: (k[0], k[1]["id"]))
    nod = sorted([k for k in keyed if not k[0]], key=lambda k: k[1]["id"])
    past = sorted([k for k in keyed if k[0] and k[0] < t], key=lambda k: (k[0], k[1]["id"]), reverse=True)
    return [k[1] for k in up + nod + past]


def reward_totals(items: list, tried=()) -> dict:
    """missing_value = still waiting for the backfill task; unestimable = backfill ran and had no value."""
    ent = [i for i in items if i["state"] == "submitted"]
    with_val = [i for i in ent if i.get("prize_min_value") is not None]
    return {"entered": len(ent),
            "confirmed_min": sum(i["prize_min_value"] for i in with_val if i.get("all_rewarded") == 1),
            "at_stake_min": sum(i["prize_min_value"] for i in with_val),
            "missing_value": sum(1 for i in ent if i.get("prize_min_value") is None and (i.get("prize") or "").strip()
                                 and i["id"] not in tried),
            "unestimable": sum(1 for i in ent if i.get("prize_min_value") is None and (i.get("prize") or "").strip()
                               and i["id"] in tried),
            "no_prize": sum(1 for i in ent if i.get("prize_min_value") is None and not (i.get("prize") or "").strip())}


def section_rewards(data, today) -> str:
    rows = reward_rows(data["items"], today)
    tot = reward_totals(data["items"], data["backfill_tried"])
    shown = rows[:MAX_ROWS]
    trs = [[esc(clip(scrub(i.get("title") or ""), 50)), esc(clip(scrub(i.get("prize")), 70)), esc(min_reward(i)),
            esc(i["winners"]) if i.get("winners") is not None else "-", esc(clip(scrub(i.get("result_date")), 24)) or "-"]
           for i in shown]
    totline = (f'<div class="tot"><b>확정 최소 보상 합계</b> {won(tot["confirmed_min"])} (전원 지급 응모) · '
               f'<b>걸린 보상 합계</b> {won(tot["at_stake_min"])} (최소값 합) · '
               f'값 미정 {tot["missing_value"]}건 <span class="mut">(추정 작업 대기)</span>'
               + (f' · 추정 불가 {tot["unestimable"]}건' if tot["unestimable"] else "")
               + (f' · 경품 정보 없음 {tot["no_prize"]}건' if tot["no_prize"] else "") + "</div>")
    body = table(["이벤트", "기대 보상", "최소 보상", "당첨자 수", "발표"], trs) if trs else '<p class="mut">보상 정보가 있는 응모가 없습니다.</p>'
    return (f'<section id="rewards"><h2>3. 보상 <small>제출한 응모 {tot["entered"]}건 · 발표 임박 순</small></h2>{totline}{body}'
            f'{more(len(rows), len(shown), "전체목록.html 참고")}</section>')


def section_wins(data, tr) -> str:
    """Total first (prominent), then every win (no row cap: all of them are listed)."""
    rows = data["wins"]
    trs = []
    for w in sorted(rows, key=lambda w: (w.get("won_at") or "", w["id"]), reverse=True):
        who = " / ".join(x for x in (scrub(w.get("title")), scrub(w.get("org"))) if x) or "-"
        note = scrub(w.get("note"))
        v = w.get("value_krw")
        val = f'{v:,}원' if isinstance(v, int) else "-"
        trs.append([esc(w.get("won_at") or ""), esc(clip(who, 60)), esc(clip(scrub(w.get("prize")), 60)),
                    esc(val), esc(clip(scrub(w.get("value_basis")), 80)), esc(clip(tr.get(note, note), 120))])
    body = (table(["날짜", "제목/기관", "상품", "가치(추정)", "가치 근거", "메모"], trs) if trs
            else '<p class="mut">기록된 당첨이 아직 없습니다.</p>')
    priced = [w["value_krw"] for w in rows if isinstance(w.get("value_krw"), int)]
    total = (f' · 총 가치 <b>{sum(priced):,}</b>원 <small>(시장가 추정, {len(priced)}/{len(rows)}건 반영)</small>'
             if priced else "")
    return (f'<section id="wins"><h2>4. 당첨 <small>{len(rows)}건</small></h2>'
            f'<div class="tot big">총 당첨 <b>{len(rows)}</b>건{total}</div>{body}</section>')


def section_signups(data) -> str:
    g = data["signups"]["groups"]
    shown = g[:MAX_ROWS]
    trs = []
    for x in shown:
        ex = " · ".join(v for v in (clip(scrub(x.get("example_title")), 40), clip(scrub(x.get("example_prize")), 40)) if v)
        needs = "/".join(NEED_KO.get(n, n) for n in x.get("needs") or [])
        trs.append([f'{esc(x["host"])}<br><span class="mut">{esc(needs)}</span>', esc(x["count"]),
                    esc(x.get("earliest_deadline") or "-"), esc(ex)])
    body = table(["사이트", "건수", "가장 빠른 마감", "예시 (제목 · 경품)"], trs) if trs else '<p class="mut">해당 사이트가 없습니다.</p>'
    return (f'<section id="signups"><h2>5. 가입하면 참여 가능한 사이트 <small>{data["signups"]["sites"]}곳 · '
            f'{data["signups"]["items"]}건</small></h2>{body}{more(len(g), len(shown), "건수 많은 순")}</section>')


def deadline_counts(data, today) -> list:
    out = []
    for n in range(0, SOON_DAYS + 1):
        d = (today + timedelta(days=n)).isoformat()
        sub = sum(1 for i in data["items"] if i["state"] == "submitted" and (i.get("deadline") or "")[:10] == d)
        left = sum(1 for i in data["items"] if i["state"] in PENDING_STATES and (i.get("deadline") or "")[:10] == d)
        cand = sum(1 for p in data["pool"] if (p.get("deadline") or "")[:10] == d)
        out.append({"date": d, "n": n, "submitted": sub, "left": left, "pool": cand})
    return out


def section_deadlines(data, today) -> str:
    cells = []
    for c in deadline_counts(data, today):
        tag = "오늘" if c["n"] == 0 else f"D-{c['n']}"
        cls = "day u" if c["n"] <= 1 and c["left"] else "day"
        cells.append(f'<div class="{cls}"><b>{esc(c["date"][5:])} ({tag})</b><div>제출 {c["submitted"]}</div>'
                     f'<div>남음 {c["left"]}</div><div>후보 {c["pool"]}</div></div>')
    return (f'<section id="soon"><h2>6. 마감 {SOON_DAYS}일 이내 <small>날짜별 제출 / 남음 / 후보(보관 목록)</small></h2>'
            f'<div class="days">{"".join(cells)}</div></section>')


def section_errors(data, tr) -> str:
    errs = data["status"]["open_errors"]
    parts = []
    for e in errs[-MAX_ERRORS:][::-1]:
        m = scrub(e.get("message"))
        parts.append(f'<tr><td>{esc((e.get("at") or "")[5:16].replace("T", " "))}</td><td>{esc(e.get("agent"))}</td>'
                     f'<td>{esc(clip(tr.get(m, m), 200))}</td></tr>')
    body = (f'<div class="w"><table><thead><tr><th>시각</th><th>에이전트</th><th>내용</th></tr></thead>'
            f'<tbody>{"".join(parts)}</tbody></table></div>') if parts else '<p class="ok">열린 오류가 없습니다.</p>'
    return (f'<section id="errors"><h2>7. 오류 <small>열린 오류 {len(errs)}건</small></h2>'
            f'{body}{more(len(errs), min(len(errs), MAX_ERRORS), "오래된 것")}</section>')


def section_check(data, tr) -> str:
    """Last section: every blocked agent with the reason (the user restarts it in the app)."""
    rows = [(a, v) for a, v in sorted(data["status"]["blocked"].items())]
    trs = []
    for agent, why in rows:
        w = scrub(why)
        trs.append([esc(agent), esc(clip(tr.get(w, w), 200))])
    body = table(["에이전트", "이유"], trs) if trs else '<p class="ok">확인할 에이전트가 없습니다.</p>'
    return f'<section id="check"><h2>8. 확인 필요 <small>{len(rows)}건</small></h2>{body}</section>'


def render_compact(data: dict, today: date, tr: dict) -> str:
    s = stats(data, today)
    st = s["states"]
    # (label, number, color, detail lines); the per-country line is "한국 N · 미국 N · 일본 N"
    cards = [
        ("총 제출", s["submitted"], "g", [region_line(s["by_region"])]),
        ("오늘 제출", s["today"], "g", [region_line(s["today_by_region"])]),
        ("진행 중", s["in_progress"], "a",
         [region_line(s["in_progress_by_region"]),
          f'확인 {st["new"] + st["verifying"]} · 예정 {st["ready"]} · 응모 중 {st["submitting"]}']),
        ("오늘 마감", s["due_today"], "r" if s["due_today"] else "", [region_line(s["due_today_by_region"])]),
        ("내가 할 일", s["todo"], "pu" if s["todo"] else "",
         [region_line(s["todo_by_region"]), f'직접 {st["manual"]} · 확인 {st["owner"]}']),
        ("당첨", s["wins"], "g", [f'실패 {st["failed"]} · 제외 {st["excluded"]}']),
    ]
    ch = "".join(f'<div class="card"><div class="l">{esc(l)}</div><div class="n {c}">{esc(n)}</div>'
                 + "".join(f'<div class="d">{esc(x)}</div>' for x in d) + "</div>" for l, n, c, d in cards)
    priced = [w["value_krw"] for w in data["wins"] if isinstance(w.get("value_krw"), int)]
    if priced:  # full-width card under the grid: total market value of the wins
        ch += (f'<div class="card wide"><div class="l">당첨 가치 (시장가 추정)</div>'
               f'<div class="n g">{sum(priced):,}원</div></div>')
    nav = "".join(f'<a href="#{a}">{esc(t)}</a>' for a, t in (
        ("todo", "내가 할 일"), ("today", "오늘 제출"), ("rewards", "보상"), ("wins", "당첨"), ("signups", "가입 사이트"),
        ("soon", "마감 임박"), ("errors", "오류"), ("check", "확인 필요")))
    body = (f'<div class="top"><h1>응모 현황판</h1><div class="sub">기준일 {today.isoformat()} (KST)</div></div>'
            f'<div class="cards">{ch}</div><nav>{nav}</nav>'
            + section_todo(data, today, tr) + section_today(data, today) + section_rewards(data, today)
            + section_wins(data, tr) + section_signups(data) + section_deadlines(data, today) + section_errors(data, tr)
            + section_check(data, tr))
    return page("응모 현황판", body)


# ---------- full list ----------
def render_full(data: dict, today: date, tr: dict) -> str:
    items = sorted(data["items"], key=model.order_key)
    live = [i for i in items if i["state"] != "excluded"]
    excluded = [i for i in items if i["state"] == "excluded"]
    s = stats(data, today)

    def row(i, why_col):
        why = scrub(i.get(why_col))
        why = tr.get(why, why)
        if i["state"] == "owner":
            q = scrub(i.get("question"))
            why = tr.get(q, q)
        return [esc(i["id"]), esc(STATE_KO.get(i["state"], i["state"])), esc(i.get("region") or "KR"),
                esc(day_label(i.get("deadline"), today)), esc(clip(scrub(i.get("org")), 30)),
                esc(clip(scrub(i.get("title")), 70)), link(i.get("url") or i.get("source_url"), "응모 ↗"),
                esc(clip(scrub(i.get("prize")), 50)), esc(min_reward(i)) if i.get("prize_min_value") is not None else "-",
                esc(clip(scrub(i.get("result_date")), 20)) or "-", esc(clip(why, 120))]

    head = ["ID", "상태", "지역", "마감", "기관", "이벤트", "링크", "경품", "최소 보상", "발표", "메모"]
    counts = " · ".join(f"{STATE_KO[k]} {v}" for k, v in s["states"].items() if v and k != "new") or ""
    body = (f'<div class="top"><h1>전체 목록</h1><div class="sub">기준일 {today.isoformat()} (KST) · 이 파일은 이 PC에서만 봅니다</div></div>'
            f'<p class="sub">{esc(counts)}</p>'
            f'<section><h2>제외 아님 <small>{len(live)}건</small></h2>'
            + table(head, [row(i, "reason") for i in live]) + "</section>"
            + f'<details><summary>제외 {len(excluded)}건</summary>'
            + table(head, [row(i, "reason") for i in excluded]) + "</details>")
    return page("응모 전체 목록", body)


# ---------- xlsx ----------
MANUAL_HEAD = ["ID", "마감", "사이트", "제목", "해야 할 일", "링크", config.XLSX_COL_DONE, config.XLSX_COL_ANSWER]
OWNER_HEAD = ["ID", "마감", "사이트", "제목", "질문", "링크", config.XLSX_COL_ANSWER]
XLSX_NOTES = [
    "노란 칸만 적습니다. 나머지는 현황판이 다시 씁니다(항목이 바뀔 때).",
    "직접 참여 시트: 로그인, 본인 인증, 결제처럼 내가 직접 해야 하는 응모입니다. 하고 나면 '내 처리'에 했음, 안 하면 안함 이라고 적습니다.",
    "직접 참여 시트의 '내 답'은 메모 칸입니다(읽어서 처리하지 않습니다).",
    "확인 필요 시트: 세션이 묻는 것입니다. '내 답'에 답을 적으면 그 항목은 다시 검증 대기로 돌아가 답을 써서 진행합니다.",
    "적은 뒤 저장하고 닫으면 실행기가 60초 안에 읽습니다. 파일이 열려 있는 동안은 현황판이 엑셀을 고치지 못합니다.",
    "마감이 없는 직접 참여 항목은 목록 맨 아래에 있습니다.",
    "반영되지 않은 내 입력이 있는 항목은 상태가 바뀌어도 목록 맨 아래에 남깁니다.",
    "글꼴은 한글 표시를 위해 맑은 고딕을 썼습니다(가정).",
]


def read_owner_cells(path: str) -> dict:
    """{sheet: {item id: {column: text}}} of the owner columns of an existing xlsx (all non-empty cells)."""
    import openpyxl
    out = {config.XLSX_SHEET_MANUAL: {}, config.XLSX_SHEET_OWNER: {}}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in out:
            if sheet not in wb.sheetnames:
                continue
            rows = wb[sheet].iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in (next(rows, None) or ())]
            if config.XLSX_COL_ID not in header:
                continue
            ii = header.index(config.XLSX_COL_ID)
            cols = [(c, header.index(c)) for c in (config.XLSX_COL_DONE, config.XLSX_COL_ANSWER) if c in header]
            for row in rows:
                if not row or ii >= len(row) or not row[ii]:
                    continue
                cells = {c: str(row[j]).strip() for c, j in cols if j < len(row) and row[j] not in (None, "")}
                if cells:
                    out[sheet][str(row[ii]).strip()] = cells
    finally:
        wb.close()
    return out


def _applied(db, iid: str, text: str) -> bool:
    return db.execute("SELECT 1 FROM owner_answers WHERE item_id=? AND text=? AND applied=1", (iid, text)).fetchone() is not None


FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def safe_xlsx_text(v):
    """Neutralize spreadsheet formula prefixes (= + - @ tab CR) on outside text."""
    if v is None:
        return None
    s = str(v)
    if s.startswith(FORMULA_LEAD) or s.lstrip().startswith("="):
        return "'" + s
    return s


def xlsx_rows(data: dict, today: date, tr: dict, old: dict) -> tuple:
    """(manual rows, owner rows, kept) with the owner cells carried over by ID. Owner input that was not applied yet is
    never dropped: when its item no longer belongs to the sheet the row stays at the bottom (kept counts them)."""
    today_s = today.isoformat()
    byid = {i["id"]: i for i in data["items"]}
    manual = sorted([i for i in data["items"] if i["state"] == "manual"
                     and (not i.get("deadline") or i["deadline"] >= today_s)], key=model.order_key)
    owner = sorted([i for i in data["items"] if i["state"] == "owner"], key=model.order_key)

    def common(i):
        return [safe_xlsx_text(i["id"]),
                (i.get("deadline") or "")[:10] + ((" " + i["deadline_time"]) if i.get("deadline") and i.get("deadline_time") else ""),
                safe_xlsx_text(model.signup_host(i.get("url")) or scrub(i.get("org"))),
                safe_xlsx_text(scrub(i.get("title") or i.get("title_en"))),
                None, safe_url(i.get("url") or i.get("source_url"))]

    mrows, orows, kept = [], [], 0
    om, oo = old.get(config.XLSX_SHEET_MANUAL, {}), old.get(config.XLSX_SHEET_OWNER, {})
    for i in manual:
        r = common(i)
        r[4] = safe_xlsx_text(need_text(i, tr))
        c = om.get(i["id"], {})
        mrows.append(r + [safe_xlsx_text(c.get(config.XLSX_COL_DONE)), safe_xlsx_text(c.get(config.XLSX_COL_ANSWER))])
    for i in owner:
        r = common(i)
        r[4] = safe_xlsx_text(need_text(i, tr).removeprefix("확인 필요: "))
        c = oo.get(i["id"], {})
        orows.append(r + [safe_xlsx_text(c.get(config.XLSX_COL_ANSWER))])
    db = store.connect()
    try:
        have_m, have_o = {i["id"] for i in manual}, {i["id"] for i in owner}
        recognized = set(config.XLSX_DONE_VALUES) | set(config.XLSX_SKIP_VALUES)
        for iid, c in om.items():
            if iid in have_m:
                continue
            v = c.get(config.XLSX_COL_DONE)
            if v in recognized and not _applied(db, iid, v):
                it = byid.get(iid) or {"id": iid}
                r = common(it)
                r[4] = "(반영되지 않은 내 입력 보존)"
                mrows.append(r + [safe_xlsx_text(v), safe_xlsx_text(c.get(config.XLSX_COL_ANSWER))])
                kept += 1
        for iid, c in oo.items():
            v = c.get(config.XLSX_COL_ANSWER)
            if iid in have_o or not v or _applied(db, iid, v):
                continue
            it = byid.get(iid) or {"id": iid}
            r = common(it)
            r[4] = "(반영되지 않은 내 입력 보존)"
            orows.append(r + [safe_xlsx_text(v)])
            kept += 1
    finally:
        db.close()
    return mrows, orows, kept


def write_xlsx(path: str, mrows: list, orows: list) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    font, bold = Font(name="Malgun Gothic", size=10), Font(name="Malgun Gothic", size=10, bold=True)
    fill = PatternFill("solid", fgColor="FFF8E1")

    def sheet(title, head, rows, edit_cols, widths):
        ws = wb.create_sheet(title)
        ws.append(head)
        for c in ws[1]:
            c.data_type = "s"
            c.font = bold
        for r in rows:
            ws.append([safe_xlsx_text(v) for v in r])
        for row in ws.iter_rows(min_row=2):
            for c in row:
                if c.value is not None:
                    c.data_type = "s"
                c.font, c.alignment = font, Alignment(wrap_text=True, vertical="top")
        for col in edit_cols:
            j = head.index(col) + 1
            for r in range(2, ws.max_row + 1):
                ws.cell(r, j).fill = fill
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    sheet(config.XLSX_SHEET_MANUAL, MANUAL_HEAD, mrows, (config.XLSX_COL_DONE, config.XLSX_COL_ANSWER),
          [8, 16, 20, 36, 50, 40, 11, 24])
    sheet(config.XLSX_SHEET_OWNER, OWNER_HEAD, orows, (config.XLSX_COL_ANSWER,), [8, 16, 20, 36, 60, 40, 30])
    ws = wb.create_sheet("설명")
    for line in XLSX_NOTES:
        ws.append([safe_xlsx_text(line)])
    for row in ws.iter_rows(min_row=1):
        for c in row:
            if c.value is not None:
                c.data_type = "s"
    ws.column_dimensions["A"].width = 110
    d, name = os.path.split(path)
    tmp = os.path.join(d, ".tmp-" + name)
    wb.save(tmp)
    try:
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)  # our own temp file
        raise


def do_xlsx(data, today, tr) -> dict:
    """Returns {"xlsx": path|None, "xlsx_skipped": reason|None, "xlsx_changed": bool, "xlsx_kept": n}."""
    path = config.owner_xlsx_path()
    old = {}
    if os.path.exists(path):
        if cli._xlsx_locked(path):
            return {"xlsx": None, "xlsx_skipped": "locked", "xlsx_changed": False, "xlsx_kept": 0}
        try:
            old = read_owner_cells(path)
        except PermissionError:
            return {"xlsx": None, "xlsx_skipped": "locked", "xlsx_changed": False, "xlsx_kept": 0}
        except Exception as e:  # never overwrite a file we could not read (it may hold owner answers)
            msg = f"owner xlsx not readable, not rewritten: {type(e).__name__}: {e}"
            _error(msg)
            return {"xlsx": None, "xlsx_skipped": msg, "xlsx_changed": False, "xlsx_kept": 0}
    mrows, orows, kept = xlsx_rows(data, today, tr, old)
    sig = hashlib.sha256(json.dumps([mrows, orows], ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    if sig == data["xlsx_sig"] and os.path.exists(path):
        return {"xlsx": path, "xlsx_skipped": None, "xlsx_changed": False, "xlsx_kept": kept}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        write_xlsx(path, mrows, orows)
    except PermissionError:
        return {"xlsx": None, "xlsx_skipped": "locked", "xlsx_changed": False, "xlsx_kept": kept}
    with store.writer() as db:
        store.kv_set(db, "xlsx_sig", sig)
    return {"xlsx": path, "xlsx_skipped": None, "xlsx_changed": True, "xlsx_kept": kept,
            "xlsx_rows": {"manual": len(mrows), "owner": len(orows)}}


# ---------- files ----------
def _error(msg: str):
    try:
        with store.writer() as db:
            store.add_error(db, "system", msg)
    except Exception as e:
        sys.stderr.write(f"could not record error row ({type(e).__name__}: {e}): {msg}\n")


def write_atomic(path: str, text: str) -> bool:
    """Write text (UTF-8) through a temp file in the same folder. Returns False when the file already had that content."""
    data = text.encode("utf-8")
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    d, name = os.path.split(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".tmp-" + name)
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.replace(tmp, path)
    except OSError:
        os.remove(tmp)  # our own temp file
        raise
    return True


def run(no_xlsx: bool = False, dry_run: bool = False) -> dict:
    today = model.today_kst()
    out = {"ok": True}
    sync = None
    xlsx_skipped = None
    if dry_run:
        xlsx_skipped = "dry-run"
    elif no_xlsx:
        xlsx_skipped = "disabled (--no-xlsx)"
    else:
        sync = cli.call(["sync-xlsx"])
        if not sync.get("ok"):
            xlsx_skipped = f"sync-xlsx failed: {sync.get('error')}"
            _error(f"render: {xlsx_skipped}")
        elif sync.get("locked"):
            xlsx_skipped = "locked"  # Excel has the file open: do not rewrite it, edits are queued next time
    data = gather()
    tr = translate.cached_many(free_texts(data), allow_call=not dry_run, max_calls=MAX_TRANSLATE_CALLS)
    tl = dict(translate.LAST)
    tr_full = translate.cached_many(full_texts(data), allow_call=False)
    tr = {**tr_full, **tr}
    compact = render_compact(data, today, tr)
    full = render_full(data, today, tr)

    content_hash = hashlib.sha256(compact.encode("utf-8")).hexdigest()
    gen = data["render_gen"]
    stamp = gen.get("generated") if gen.get("content_hash") == content_hash and gen.get("generated") else \
        model.now_kst().strftime("%Y-%m-%d %H:%M") + " KST"
    compact, full = compact.replace(FOOTER_GENERATED, stamp), full.replace(FOOTER_GENERATED, stamp)
    digest = hashlib.sha256(compact.encode("utf-8")).hexdigest()
    html_path, full_path = config.owner_html_path(), config.full_list_html_path()
    out.update({"html": html_path, "full": full_path, "hash": digest,
                "changed": digest != data["render_hash"], "bytes": len(compact.encode("utf-8")),
                "translated": tl["translated"], "translate_errors": tl["errors"],
                "translate_deferred": translate.LAST["deferred"], "translate_env": tl["env"],
                "stats": stats(data, today)})
    if tl.get("error"):
        out["translate_error"] = tl["error"]
    if dry_run:
        out.update({"xlsx": None, "xlsx_skipped": xlsx_skipped, "dry_run": True})
        return out
    out["html_written"] = write_atomic(html_path, compact)
    out["full_written"] = write_atomic(full_path, full)
    with store.writer() as db:
        if gen.get("content_hash") != content_hash or gen.get("generated") != stamp:
            store.kv_set(db, "render_gen", {"content_hash": content_hash, "generated": stamp})
        store.kv_set(db, "render_hash", digest)
    if xlsx_skipped is None:
        out.update(do_xlsx(data, today, tr))
    else:
        out.update({"xlsx": None, "xlsx_skipped": xlsx_skipped})
    if sync and sync.get("queued"):
        # sync-xlsx only queues owner edits; they are applied after confirmation in the app (confirm_xlsx)
        out["owner_answers_queued"] = sync["queued"]
    return out


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="ep render")
    ap.add_argument("cmd", nargs="?", default="render")
    ap.add_argument("--no-xlsx", dest="no_xlsx", action="store_true")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = run(no_xlsx=a.no_xlsx, dry_run=a.dry_run)
    except Exception as e:  # never swallow: JSON result + errors row
        msg = f"render failed: {type(e).__name__}: {e}"
        _error(msg)
        res = {"ok": False, "error": msg}
    sys.stdout.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
    return 0 if res.get("ok") else 1
