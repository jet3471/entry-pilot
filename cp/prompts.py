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
  out, known      scout output path and known-events path (defaults from cp.config)
  today           'YYYY-MM-DD' (default: today KST)

  python -m cp.prompts show <kind> <item.json> [--agent codex] [--profile-file F] [--last-failure T]
"""
import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from cp import policy as _policy

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(PROJECT_DIR, "prompts_contest")

KINDS = ("scout", "verify", "audit", "draft", "submit")
# Kinds whose template is self-contained (no common.md, region, playbooks, profile or finishing commands). They take
# `item` = {"items": [rows]} (see backfill_build) and are built by their own function.
SELF_CONTAINED = ("backfill",)
REGIONS = ("KR", "JP", "US")
TERMINAL = {
    "scout": (),
    "verify": ("verify", "exclude", "owner", "manual"),
    "audit": ("audit_done",),
    "draft": ("draft_done", "owner", "exclude", "manual"),
    "submit": ("done", "fail", "manual", "owner", "exclude"),
}
PLAYBOOKS = ("google_form", "naver_form", "moaform", "nownsurvey", "site_form", "youtube", "naver_blog", "instagram",
             "x", "facebook", "threads", "kakao", "aggregator", "gleam", "jp_campaign", "email_verify",
             "email_submit")

# Local copy of the design's host -> site_kind table (used only if cp.model cannot be imported).
_SITE_KIND_HOSTS = [
    ("docs.google.com/forms", "google_form"), ("forms.gle", "google_form"), ("goo.gl/forms", "google_form"),
    ("form.naver.com", "naver_form"),
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

# Item text that triggers the email_verify playbook for kind submit (assumption: this phrase list).
EMAIL_VERIFY_RX = (r"email verification|verification code|verify your email|confirmation email|인증 메일|이메일 인증|"
                   r"認証メール|メール認証")
EMAIL_VERIFY_FIELDS = ("verify_notes", "conditions", "legacy_notes", "notes")

ITEM_FIELDS = {
    "verify": ("id", "region", "title", "org", "url", "source_url", "alt_urls", "form_type", "site_kind", "deadline",
               "deadline_time", "prize", "conditions", "login_required", "eligibility", "verify_notes", "answers",
               "flags", "work"),
    "audit": ("id", "region", "title", "org", "url", "source_url", "deadline", "deadline_time", "prize", "conditions",
              "eligibility", "verify_notes", "work"),
    "draft": ("id", "region", "title", "org", "url", "source_url", "deadline", "deadline_time", "prize", "conditions",
              "eligibility", "verify_notes", "answers", "flags", "work"),
    "submit": ("id", "region", "title", "org", "url", "source_url", "alt_urls", "form_type", "site_kind", "lane",
               "deadline", "deadline_time", "prize", "conditions", "login_required", "eligibility", "verify_notes",
               "answers", "flags", "work"),
}
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
        from cp import model
        sk = model.site_kind(url, form_type)
    except Exception:
        sk = _local_site_kind(url, form_type)
    if sk in ("site_form", "other"):  # form_type values from scouts (x, instagram, youtube, gleam)
        sk = _FORM_TYPE_KIND.get((form_type or "").lower(), sk)
    return sk


def _is_aggregator(url) -> bool:
    return _local_site_kind(url) == "aggregator"


def pick_playbooks(kind: str, item: dict) -> list:
    if kind in ("scout", "draft", "audit"):
        return []
    sk = item.get("site_kind") or detect_site_kind(item.get("url"), item.get("form_type"))
    if sk not in PLAYBOOKS:
        sk = "site_form"
    if kind == "submit" and (item.get("work") or {}).get("submit_method") == "email":
        return ["email_submit"]  # the whole flow is Gmail; the entry page needs no site playbook
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
        # (assumption) a needed email code outranks a merely mentioned SNS playbook; lane-driven picks above keep
        # their slot because of the two-playbook cap.
        chosen.append("email_verify")
    else:
        for name, pat in _SECONDARY:
            if name == "jp_campaign" and region != "JP":
                continue
            if name not in PLAYBOOKS:
                # nice_auth.md went away with the phone stack but its trigger stayed; an item
                # whose notes said 본인인증 could not be launched at all and blocked the verify queue for 80 minutes.
                continue
            if name not in chosen and re.search(pat, text, re.I):
                chosen.append(name)
                break
    return chosen[:2]


def _sections(text: str, wanted: tuple[str, ...]) -> str:
    """Preamble plus every '## <name>' section in `wanted`, in file order."""
    parts = re.split(r"(?m)^## +(\w+)\s*$", text)
    out = parts[0].rstrip()
    for name, body in zip(parts[1::2], parts[2::2]):
        if name in wanted and body.strip():
            out += "\n\n" + body.strip()
    return out


def _is_no_work_submit(item: dict | None) -> bool:
    return bool(((item or {}).get("work") or {}).get("no_work"))


# ---------- blocks ----------
def item_block(kind: str, item: dict, ctx: dict) -> str:
    """Item JSON with only the fields the task needs (nothing is clipped)."""
    if kind == "scout":
        return ""
    d = {}
    for k in ITEM_FIELDS[kind]:
        v = item.get(k)
        if k == "site_kind" and not v:
            v = detect_site_kind(item.get("url"), item.get("form_type"))
        if k == "work" and kind == "submit" and isinstance(v, dict) and v.get("snapshot_dir"):
            v = {wk: wv for wk, wv in v.items() if wk not in ("draft_dir", "draft_files")}
        if v in (None, "", [], {}):
            continue
        d[k] = v
    lines = ["# Item", json.dumps(d, ensure_ascii=False)]
    legacy = str(item.get("legacy_notes") or "").strip()
    if legacy:
        lines.append("legacy notes (Korean): " + legacy)
    last = str(ctx.get("last_failure") or "").strip()
    if last:
        lines.append("last failure: " + last)
    return "\n".join(lines)


def profile_block(kind: str, ctx: dict) -> str:
    if kind == "scout":
        return ""
    raw = str(ctx.get("profile_lines") or "").strip()
    keep = [ln for ln in raw.splitlines() if ln.strip() and not _PASSWORD_RE.search(ln)]
    body = "\n".join(keep) if keep else "(no profile lines given: any personal field a form requires → cp owner)"
    return "# Profile (only fields a form asks; never in public text)\n" + body


def finish_block(kind: str, item: dict, ctx: dict) -> str:
    a = ctx.get("agent") or "codex"
    i = item.get("id") or "ID"
    if kind == "scout":
        return ""
    need_sent = kind == "submit" and not _is_no_work_submit(item)
    rows = {
        "verify": f"cp verify {i} --agent {a} --lane form|browser --work-type <type> | --no-work "
                  "--theme \"<T>\" --format \"<T>\" --submit-method form|email|upload|post|sns|other --top-prize \"<T>\" "
                  "--top-prize-krw <n> --analysis-file {{ANALYSISFILE}} --notes \"<T>\" [--answers '<JSON>'] [--url U] "
                  "[--eligibility open|residency_required|unknown] [--result-date T]",
        "audit_done": f"cp audit-done {i} --agent {a} --verdict pass|fail --findings \"<English: what is wrong, where in the "
                      "dossier, the correct fact with its source; for pass: minor remarks or empty>\"",
        "draft_done": f"cp draft-done {i} --agent {a} --summary \"<what the work is, 1-2 sentences>\" "
                      "--facts \"<each user fact used, separated by |; empty if none>\"",
        "exclude": f"cp exclude {i} --agent {a} --reason \"<T>\"",
        "owner": f"cp owner {i} --agent {a} --question \"<T>\"",
        "manual": f"cp manual {i} --agent {a} --need <need> --reason \"<T>\"",
        "done": f"cp done {i} --agent {a} --evidence \"<completion text seen>\"" + (
            " --sent-json '<JSON of what you sent>'" if need_sent else ""),
        "fail": f"cp fail {i} --agent {a} --reason \"<T>\" [--no-resubmit]",
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
            from cp import config
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out = out or os.path.join(config.scout_inbox_dir(), f"{stamp}-{agent}-{region}.jsonl")
            known = known or config.known_events_path()
        except Exception:
            out = out or "(scout output path missing)"
            known = known or "(known-events list missing)"
    from cp import config
    iid = str(item.get("id") or "ID")
    facts, drafts = config.owner_facts_path(), os.path.join(config.drafts_dir(), iid)
    snap_dir = str(((item or {}).get("work") or {}).get("snapshot_dir") or "")
    analysis_file = os.path.join(config.analysis_dir(), iid + ".md")
    ep_cmd = "python -X utf8 " + os.path.join(config.PROJECT_DIR, "cp.py")
    return {"ID": str(item.get("id") or ""), "AGENT": agent, "REGION": region, "LANE": str(item.get("lane") or ""),
            "OUT": str(out or ""), "KNOWN": str(known or ""), "TODAY": today, "UNTIL": until,
            "FACTS": facts, "CAPTURES": os.path.join(config.data_dir(), "captures"), "DRAFTDIR": drafts,
            "SNAPDIR": snap_dir, "ANALYSISFILE": analysis_file, "EP": ep_cmd}


def _fill(text: str, values: dict) -> str:
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), text)


def _parts(kind: str, item: dict, ctx: dict) -> list:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    region = (item.get("region") or ctx.get("region") or "KR").upper()
    if region not in REGIONS:
        raise ValueError(f"unknown region {region}")
    vals = _placeholders(kind, item, ctx)
    raw_kind = _read(f"{kind}.md")
    if kind == "submit":
        mode_sec = "no_work" if _is_no_work_submit(item) else "drafted"
        kind_txt = _sections(raw_kind, (mode_sec, "shared_outcomes"))
    else:
        kind_txt = raw_kind
    parts = [("common", _read("common.md")), ("kind", kind_txt),
             ("region", _section(_read(f"regions/{region.lower()}.md"), kind))]
    chosen = pick_playbooks(kind, item)
    pb_sec = "submit_no_work" if (kind == "submit" and _is_no_work_submit(item)) else kind
    for pb in chosen:
        pb_raw = _read(f"playbooks/{pb}.md")
        sec_key = pb_sec if f"\n## {pb_sec}\n" in ("\n" + pb_raw) else kind
        parts.append((f"playbook:{pb}", _section(pb_raw, sec_key)))
    pol = _policy.policy_lines(region, kind)
    if pol:
        parts.append(("policy", "# Policy\n" + pol))
    for name, fn in (("item", item_block), ("profile", profile_block)):
        txt = fn(kind, item, ctx) if name == "item" else fn(kind, ctx)
        if txt:
            parts.append((name, txt))
    fin = finish_block(kind, item, ctx)
    if fin:
        parts.append(("finish", fin))
    return [(n, _fill(t, vals)) for n, t in parts]


def _join(parts: list) -> str:
    return "\n\n".join(t for _, t in parts) + "\n"


def _assemble(kind: str, item: dict, ctx: dict) -> list:
    return _parts(kind, item, ctx)


def _check(parts: list) -> None:
    for name, text in parts:
        assert "{{" not in text, f"unfilled placeholder in {name}"


# ---------- backfill (self-contained kind) ----------
BACKFILL_FIELDS = ("id", "region", "title", "org", "prize", "conditions", "winners", "all_rewarded")
def _backfill_row(row: dict) -> str:
    d = {}
    for k in BACKFILL_FIELDS:
        v = row.get(k)
        if v in (None, "", [], {}):
            continue
        d[k] = v
    return json.dumps(d, ensure_ascii=False)


def backfill_build(rows: list, ctx: dict | None = None) -> tuple:
    """(prompt text, ids included): every row is included. The template is prompts/backfill.md (no common.md)."""
    head = _read("backfill.md")
    rows = list(rows)
    nl = chr(10)
    text = head + nl + nl + "# Items" + nl + "[" + ("," + nl).join(_backfill_row(r) for r in rows) + "]" + nl
    return text, [r["id"] for r in rows]


def build(kind: str, item: dict, ctx: dict) -> str:
    if kind in SELF_CONTAINED:
        return backfill_build((item or {}).get("items") or [], ctx)[0]
    parts = _assemble(kind, item, ctx)
    _check(parts)
    return _join(parts)


def size_report(kind: str, item: dict, ctx: dict) -> dict:
    """Information only: the characters of each part and of the whole prompt (there is no limit)."""
    parts = _assemble(kind, item, ctx)
    return {"kind": kind, "parts": {name: len(text) for name, text in parts}, "total": len(_join(parts)),
            "playbooks": [n.split(":", 1)[1] for n, _ in parts if n.startswith("playbook:")]}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m cp.prompts")
    sp = ap.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("show")
    x.add_argument("kind", choices=KINDS)
    x.add_argument("item_json", help="file with one item as JSON (for scout: {\"region\": \"JP\"})")
    x.add_argument("--agent", default="codex")
    x.add_argument("--profile-file", help="text file with profile lines (test data; never profile.json)")
    x.add_argument("--last-failure")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with open(a.item_json, encoding="utf-8") as f:
        item = json.load(f)
    ctx = {"agent": a.agent, "last_failure": a.last_failure}
    if a.profile_file:
        with open(a.profile_file, encoding="utf-8") as f:
            ctx["profile_lines"] = f.read()
    print(build(a.kind, item, ctx))
    print(json.dumps(size_report(a.kind, item, ctx), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
