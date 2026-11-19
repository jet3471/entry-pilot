"""Prompt assembly for agent runs.

build(kind, item, ctx) = prompts/common.md + prompts/<kind>.md + prompts/regions/<region>.md + up to two
prompts/playbooks/<site_kind>.md + policy lines + item block + profile block + finishing commands.

Token thrift: playbook and region files may contain "## verify" / "## submit" / "## scout" sections; only the preamble
(text before the first "## " line) and the section for the current task are included.

ctx keys (all optional except agent):
  agent           claude | codex | gemini | grok                (finishing commands carry --agent <agent>)
  profile_lines   str, compact profile view the scheduler passes (this module never reads profile.json);
                  lines mentioning passwords are dropped
  last_failure    str, reason of the last failed attempt
  sns_logged_in   str or list, SNS accounts currently logged in in the entry browser
  out, known      scout output path and known-events path (defaults from ep.config)
  today           'YYYY-MM-DD' (default: today KST)

  python -m ep.prompts show <kind> <item.json> [--agent codex] [--profile-file F] [--last-failure T]
"""
import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from ep import policy as _policy

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(PROJECT_DIR, "prompts")

KINDS = ("scout", "verify", "submit")
# Kinds whose template is self-contained (no common.md, region, playbooks, profile or finishing commands). They take
# `item` = {"items": [rows]} (see backfill_build) and are built by their own function.
SELF_CONTAINED = ("backfill",)
REGIONS = ("KR", "JP", "US")
BUDGET = {"common": 2700, "kind": 2500, "region": 1200, "playbook": 2000, "item": 1500, "profile": 1000,
# common has room for the ep.py path, which is filled in per install
          "policy": 1500, "total": 10000}
TERMINAL = {
    "scout": (),
    "verify": ("verify", "exclude", "owner", "manual"),
    "submit": ("done", "fail", "manual", "owner", "exclude"),
}
PLAYBOOKS = ("google_form", "naver_form", "moaform", "nownsurvey", "site_form", "youtube", "naver_blog", "instagram",
             "x", "facebook", "threads", "kakao", "aggregator", "gleam", "nice_auth", "jp_campaign", "email_verify")

# Local copy of the host -> site_kind table (used only if ep.model cannot be imported).
_SITE_KIND_HOSTS = [
    ("docs.google.com/forms", "google_form"), ("forms.gle", "google_form"), ("form.naver.com", "naver_form"),
    ("naver.me", "naver_form"), ("moaform.com", "moaform"), ("nownsurvey.com", "nownsurvey"),
    ("youtube.com", "youtube"), ("youtu.be", "youtube"), ("blog.naver.com", "naver_blog"),
    ("instagram.com", "instagram"), ("x.com", "x"), ("twitter.com", "x"), ("facebook.com", "facebook"),
    ("threads.net", "threads"), ("threads.com", "threads"), ("pf.kakao.com", "kakao"), ("open.kakao.com", "kakao"),
    ("eventhouse.kr", "aggregator"), ("supertoday.co.kr", "aggregator"), ("suto.co.kr", "aggregator"),
    ("gleam.io", "gleam"), ("rafflecopter.com", "gleam"), ("kingsumo.com", "gleam"), ("woobox.com", "gleam"),
    ("viralsweep.com", "gleam"),
]
_FORM_TYPE_KIND = {"google": "google_form", "google_form": "google_form", "naver": "naver_form",
                   "naver_form": "naver_form", "moaform": "moaform", "nownsurvey": "nownsurvey", "x": "x",
                   "instagram": "instagram", "youtube": "youtube", "gleam": "gleam"}

# Secondary playbook triggers: (playbook, regex over title + conditions + notes). First match not already chosen wins.
_SECONDARY = [
    ("nice_auth", r"본인\s*인증|휴대폰\s*인증|identity verification|\bSMS\b|本人確認|NICE평가|PASS\s*인증"),
    ("jp_campaign", r"フォロー|リポスト|リツイート|友だち追加|LINE"),
    ("instagram", r"instagram|인스타|インスタ"),
    ("youtube", r"youtube|유튜브|ユーチューブ"),
    ("naver_blog", r"blog\.naver|네이버\s*블로그|블로그\s*이웃|서로이웃|공감"),
    ("kakao", r"kakao|카카오"),
    ("x", r"x\.com|twitter|트위터|repost|retweet"),
    ("facebook", r"facebook|페이스북|フェイスブック"),
    ("threads", r"threads|스레드"),
    ("gleam", r"gleam|rafflecopter|kingsumo|woobox|viralsweep"),
]

# Item text that triggers the email_verify playbook for kind submit (this phrase list).
EMAIL_VERIFY_RX = (r"email verification|verification code|verify your email|confirmation email|인증 메일|이메일 인증|"
                   r"認証メール|メール認証")
EMAIL_VERIFY_FIELDS = ("verify_notes", "conditions", "legacy_notes", "notes")

ITEM_FIELDS = {
    "verify": ("id", "region", "title", "org", "url", "source_url", "alt_urls", "form_type", "site_kind", "deadline",
               "deadline_time", "prize", "conditions", "login_required", "eligibility", "verify_notes", "answers",
               "flags"),
    "submit": ("id", "region", "title", "org", "url", "source_url", "alt_urls", "form_type", "site_kind", "lane",
               "deadline", "deadline_time", "prize", "conditions", "login_required", "eligibility", "verify_notes",
               "answers", "flags"),
}
TEXT_LIMITS = {"title": 160, "org": 80, "prize": 160, "conditions": 450, "verify_notes": 600, "legacy_notes": 350,
               "last_failure": 250}
_PASSWORD_RE = re.compile(r"password|passwd|comment_pw|비밀번호|パスワード", re.I)

_cache = {}


def _read(rel: str) -> str:
    path = os.path.join(PROMPTS_DIR, rel)
    if path not in _cache:
        with open(path, encoding="utf-8") as f:
            _cache[path] = f.read().strip()
    return _cache[path]


def _section(text: str, kind: str) -> str:
    """Preamble plus the '## <kind>' section. Files without sections are returned whole."""
    want = kind
    parts = re.split(r"(?m)^## +(\w+)\s*$", text)
    out = parts[0].rstrip()
    for name, body in zip(parts[1::2], parts[2::2]):
        if name == want and body.strip():
            out += "\n" + body.strip()
    return out


# ---------- site kind and playbooks ----------
def _local_site_kind(url, form_type=None) -> str:
    u = str(url or "")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I):
        u = "https://" + u
    p = urllib.parse.urlsplit(u)
    host = p.netloc.lower().split("@")[-1].split(":")[0]
    for pat, kind in _SITE_KIND_HOSTS:
        h, _, pfx = pat.partition("/")
        if (host == h or host.endswith("." + h)) and (not pfx or p.path.lstrip("/").startswith(pfx)):
            return kind
    return _FORM_TYPE_KIND.get((form_type or "").lower(), "site_form")


def detect_site_kind(url, form_type=None) -> str:
    try:
        from ep import model
        sk = model.site_kind(url, form_type)
    except Exception:
        sk = _local_site_kind(url, form_type)
    if sk in ("site_form", "other"):  # form_type values from scouts (x, instagram, youtube, gleam)
        sk = _FORM_TYPE_KIND.get((form_type or "").lower(), sk)
    return sk


def _is_aggregator(url) -> bool:
    return _local_site_kind(url) == "aggregator"


def pick_playbooks(kind: str, item: dict) -> list:
    if kind == "scout":
        return []
    sk = item.get("site_kind") or detect_site_kind(item.get("url"), item.get("form_type"))
    if sk not in PLAYBOOKS:
        sk = "site_form"
    chosen = [sk]
    region = (item.get("region") or "KR").upper()
    text = " ".join(str(item.get(k) or "") for k in ("title", "conditions", "verify_notes", "legacy_notes"))
    if region == "JP" and sk in ("x", "site_form", "google_form") and sk != "jp_campaign" and (
            sk == "x" or re.search(_SECONDARY[1][1], text)):
        chosen.append("jp_campaign")
    elif sk != "aggregator" and _is_aggregator(item.get("url")) or (sk != "aggregator" and not item.get("url")
                                              and _is_aggregator(item.get("source_url"))):
        chosen.append("aggregator")
    elif kind == "submit" and re.search(EMAIL_VERIFY_RX, " ".join(str(item.get(k) or "") for k in EMAIL_VERIFY_FIELDS),
                                        re.I):
        # a needed email code outranks a merely mentioned SNS playbook (two-playbook cap).
        chosen.append("email_verify")
    else:
        for name, pat in _SECONDARY:
            if name == "jp_campaign" and region != "JP":
                continue
            if name not in chosen and re.search(pat, text, re.I):
                chosen.append(name)
                break
    return chosen[:2]


# ---------- blocks ----------
def _clip(v, n: int):
    s = str(v)
    return s if len(s) <= n else s[: max(0, n - 3)] + "..."


def item_block(kind: str, item: dict, ctx: dict, scale: float = 1.0) -> str:
    """Item JSON with only the fields the task needs; long text is clipped until the block fits its budget."""
    if kind == "scout":
        return ""
    text = _item_block_once(kind, item, ctx, scale)
    while len(text) > BUDGET["item"] and scale > 0.1:
        scale -= 0.1
        text = _item_block_once(kind, item, ctx, scale)
    return text


def _item_block_once(kind: str, item: dict, ctx: dict, scale: float) -> str:
    d = {}
    clipped = False
    for k in ITEM_FIELDS[kind]:
        v = item.get(k)
        if k == "site_kind" and not v:
            v = detect_site_kind(item.get("url"), item.get("form_type"))
        if v in (None, "", [], {}):
            continue
        if k in TEXT_LIMITS:
            c = _clip(v, int(TEXT_LIMITS[k] * scale))
            clipped = clipped or c != str(v)
            v = c
        d[k] = v
    if "answers" in d and len(json.dumps(d["answers"], ensure_ascii=False)) > int(700 * scale):
        d["answers"] = "too long for this prompt: read them with ep get " + str(item.get("id"))
    head = "# Item" + (f" (long text clipped; full record: ep get {item.get('id')})" if clipped else "")
    lines = [head, json.dumps(d, ensure_ascii=False)]
    legacy = str(item.get("legacy_notes") or "").strip()
    if legacy:
        lines.append("legacy notes (Korean): " + _clip(legacy, int(TEXT_LIMITS["legacy_notes"] * scale)))
    last = str(ctx.get("last_failure") or "").strip()
    if last:
        lines.append("last failure: " + _clip(last, int(TEXT_LIMITS["last_failure"] * scale)))
    sns = ctx.get("sns_logged_in")
    if sns:
        lines.append("Accounts logged in in the entry browser: " + (sns if isinstance(sns, str) else ", ".join(sns)))
    return "\n".join(lines)


def profile_block(kind: str, ctx: dict) -> str:
    if kind == "scout":
        return ""
    raw = str(ctx.get("profile_lines") or "").strip()
    keep = [ln for ln in raw.splitlines() if ln.strip() and not _PASSWORD_RE.search(ln)]
    body = "\n".join(keep) if keep else "(no profile lines given: any personal field a form requires → ep owner)"
    return "# Profile (only fields a form asks; never in public text)\n" + body


def finish_block(kind: str, item: dict, ctx: dict) -> str:
    a = ctx.get("agent") or "codex"
    i = item.get("id") or "ID"
    if kind == "scout":
        return ""
    rows = {
        "verify": f"ep verify {i} --agent {a} --lane form|browser --answers '<JSON>' --notes \"<T>\" "
                  "[--url U] [--eligibility open|residency_required|unknown] [--result-date T]",
        "exclude": f"ep exclude {i} --agent {a} --reason \"<T>\"",
        "owner": f"ep owner {i} --agent {a} --question \"<T>\"",
        "manual": f"ep manual {i} --agent {a} --need <need> --reason \"<T>\"",
        "done": f"ep done {i} --agent {a} --evidence \"<completion text seen>\"",
        "fail": f"ep fail {i} --agent {a} --reason \"<T>\" [--no-resubmit]",
    }
    lines = [f"# Finish (exactly one of these; you are {a})"] + [rows[v] for v in TERMINAL[kind]]
    return "\n".join(lines)


def _placeholders(kind: str, item: dict, ctx: dict) -> dict:
    region = (item.get("region") or ctx.get("region") or "KR").upper()
    today = ctx.get("today") or datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    until = (date.fromisoformat(today) + timedelta(days=45)).isoformat()
    agent = ctx.get("agent") or "codex"
    out, known = ctx.get("out"), ctx.get("known")
    if kind == "scout" and (not out or not known):
        try:
            from ep import config
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out = out or os.path.join(config.scout_inbox_dir(), f"{stamp}-{agent}-{region}.jsonl")
            known = known or config.known_events_path()
        except Exception:
            out = out or "(scout output path missing)"
            known = known or "(known-events list missing)"
    try:
        from ep import config as _cfg
        data_dir = _cfg.data_dir()
    except Exception:
        data_dir = ""
    return {"EP_PY": os.path.join(PROJECT_DIR, "ep.py"), "DATA_DIR": data_dir,
            "ID": str(item.get("id") or ""), "AGENT": agent, "REGION": region, "LANE": str(item.get("lane") or ""),
            "OUT": str(out or ""), "KNOWN": str(known or ""), "TODAY": today, "UNTIL": until}


def _fill(text: str, values: dict) -> str:
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), text)


def _parts(kind: str, item: dict, ctx: dict, max_playbooks: int = 2, scale: float = 1.0) -> list:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    region = (item.get("region") or ctx.get("region") or "KR").upper()
    if region not in REGIONS:
        raise ValueError(f"unknown region {region}")
    vals = _placeholders(kind, item, ctx)
    parts = [("common", _read("common.md")), ("kind", _read(f"{kind}.md")),
             ("region", _section(_read(f"regions/{region.lower()}.md"), kind))]
    chosen = pick_playbooks(kind, item)
    for pb in chosen[:max_playbooks]:
        parts.append((f"playbook:{pb}", _section(_read(f"playbooks/{pb}.md"), kind)))
    for pb in chosen[max_playbooks:]:  # dropped for size: point to the file instead of losing it silently
        parts.append((f"pointer:{pb}", f"# Also needed: read {os.path.join(PROMPTS_DIR, 'playbooks', pb + '.md')} "
                                       "before that step."))
    pol = _policy.policy_lines(region, kind)
    if pol:
        parts.append(("policy", "# Policy\n" + pol))
    for name, fn in (("item", item_block), ("profile", profile_block)):
        txt = fn(kind, item, ctx, scale) if name == "item" else fn(kind, ctx)
        if txt:
            parts.append((name, txt))
    fin = finish_block(kind, item, ctx)
    if fin:
        parts.append(("finish", fin))
    return [(n, _fill(t, vals)) for n, t in parts]


def _join(parts: list) -> str:
    return "\n\n".join(t for _, t in parts) + "\n"


def _assemble(kind: str, item: dict, ctx: dict) -> list:
    """Drop the second playbook, then shrink item text, until the total fits the budget."""
    for max_pb, scales in ((2, (1.0, 0.85, 0.7, 0.55)), (1, (1.0, 0.85, 0.7, 0.55, 0.4, 0.25))):
        for scale in scales:
            parts = _parts(kind, item, ctx, max_playbooks=max_pb, scale=scale)
            if len(_join(parts)) <= BUDGET["total"]:
                return parts
    return parts


def _check(parts: list) -> None:
    for name, text in parts:
        key = "playbook" if name.startswith("playbook:") else name
        if key in BUDGET:
            assert len(text) <= BUDGET[key], f"{name} is {len(text)} chars (budget {BUDGET[key]})"
        assert "{{" not in text, f"unfilled placeholder in {name}"
    total = len(_join(parts))
    assert total <= BUDGET["total"], f"prompt is {total} chars (budget {BUDGET['total']})"


# ---------- backfill (self-contained kind) ----------
BACKFILL_FIELDS = ("id", "region", "title", "org", "prize", "conditions", "winners", "all_rewarded")
BACKFILL_LIMITS = {"title": 100, "org": 50, "prize": 220, "conditions": 200}


def _backfill_row(row: dict, scale: float) -> str:
    d = {}
    for k in BACKFILL_FIELDS:
        v = row.get(k)
        if v in (None, "", [], {}):
            continue
        d[k] = _clip(v, int(BACKFILL_LIMITS[k] * scale)) if k in BACKFILL_LIMITS else v
    return json.dumps(d, ensure_ascii=False)


def backfill_build(rows: list, ctx: dict | None = None) -> tuple:
    """(prompt text, ids included). Item text is clipped, then trailing items are dropped, until the prompt fits
    BUDGET["total"] (config.PROMPT_MAX_CHARS). The template is prompts/backfill.md (no common.md)."""
    head = _read("backfill.md")
    assert len(head) <= BUDGET["kind"], f"backfill.md is {len(head)} chars (budget {BUDGET['kind']})"
    rows = list(rows)
    nl = chr(10)
    for scale in (1.0, 0.7, 0.5, 0.35):
        n = len(rows)
        while n > 0:
            text = head + nl + nl + "# Items" + nl + "[" + ("," + nl).join(_backfill_row(r, scale) for r in rows[:n]) + "]" + nl
            if len(text) <= BUDGET["total"]:
                return text, [r["id"] for r in rows[:n]]
            if scale > 0.35:
                break  # shrink the text first, drop items only at the smallest scale
            n -= 1
    return head + nl + nl + "# Items" + nl + "[]" + nl, []


def build(kind: str, item: dict, ctx: dict) -> str:
    if kind in SELF_CONTAINED:
        return backfill_build((item or {}).get("items") or [], ctx)[0]
    parts = _assemble(kind, item, ctx)
    _check(parts)
    return _join(parts)


def budget_report(kind: str, item: dict, ctx: dict) -> dict:
    parts = _assemble(kind, item, ctx)
    rep = {name: len(text) for name, text in parts}
    total = len(_join(parts))
    over = [n for n, t in parts if len(t) > BUDGET.get("playbook" if n.startswith("playbook:") else n, 10 ** 9)]
    return {"kind": kind, "parts": rep, "total": total, "budget": BUDGET["total"],
            "ok": total <= BUDGET["total"] and not over, "over": over,
            "playbooks": [n.split(":", 1)[1] for n, _ in parts if n.startswith("playbook:")],
            "pointers": [n.split(":", 1)[1] for n, _ in parts if n.startswith("pointer:")]}


def file_report() -> dict:
    """Length of every prompt file and its budget (for tests and docs)."""
    rep = {}
    for root, _, files in os.walk(PROMPTS_DIR):
        for f in sorted(files):
            if f.endswith(".md"):
                rel = os.path.relpath(os.path.join(root, f), PROMPTS_DIR).replace("\\", "/")
                key = ("region" if rel.startswith("regions/") else "playbook" if rel.startswith("playbooks/")
                       else "common" if rel == "common.md" else "kind")
                rep[rel] = (len(_read(rel)), BUDGET[key])
    return rep


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ep.prompts")
    sp = ap.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("show")
    x.add_argument("kind", choices=KINDS)
    x.add_argument("item_json", help="file with one item as JSON (for scout: {\"region\": \"JP\"})")
    x.add_argument("--agent", default="codex")
    x.add_argument("--profile-file", help="text file with profile lines (test data; never profile.json)")
    x.add_argument("--last-failure")
    sp.add_parser("files")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "files":
        for rel, (n, b) in file_report().items():
            print(f"{n:5d} / {b}  {rel}")
        return 0
    with open(a.item_json, encoding="utf-8") as f:
        item = json.load(f)
    ctx = {"agent": a.agent, "last_failure": a.last_failure}
    if a.profile_file:
        with open(a.profile_file, encoding="utf-8") as f:
            ctx["profile_lines"] = f.read()
    print(build(a.kind, item, ctx))
    print(json.dumps(budget_report(a.kind, item, ctx), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
