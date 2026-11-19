"""User-adjustable contest policy and its prompt rendering.

Values come from DEFAULTS, overridden by <data>\\policy.json (any subset of keys). Keys starting with "_" or ending in
"_note", and "names", are metadata and are never rendered. Invalid values are ignored and listed in POLICY["errors"];
unknown keys are kept as given and listed in POLICY["unknown_keys"] (never silently dropped).

  foreign_scope   JP/US entries:
                  'email_phone_only' attempt entries that ask only for email and/or phone;
                      local mailing address or residency required -> manual --need residency;
                      local phone number required -> manual --need local_phone
                  'open_only' only notices that explicitly accept entrants abroad; others excluded
                  'try_all' anything not residency_required
                  'surveys_comments' only surveys and comment entries that are not residency_required
  x_actions       the X account logged in in the entry browser: 'all' (follow, repost, like, reply) | 'follow_like' | 'none'
  foreign_signup  new accounts on JP/US sites: 'auto_all' (any site, entry email, consent logged) |
                  'auto_major' (social login or major giveaway platforms only) | 'ask' | 'exclude'
  line_campaigns  LINE friend-add campaigns: 'manual' (manual --need app) | 'exclude'
  kr_signup       'allowed_list_only' (new KR sign-ups only on the sites in kr_signup_allow; with an empty list every
                      new KR sign-up becomes manual --need login) | 'auto_simple'
  kr_signup_allow list of KR site names the user allows new sign-ups on (default: empty)
  kr_signup_deny  list of KR site names that are never entered (default: empty)
  proposal_policy contests: who accepts a finished analysis (state proposal): 'owner' (the user, in the app) | 'auto'
                  (the scheduler accepts it once the audit passed or no audit is needed; off by default)
  review_policy   contests: which drafted works wait for the user before submission: 'all' (default) | 'big_prize' (only
                  when the top prize is >= review_min_prize_krw or unknown) | 'none' (never used by the app)
  review_min_prize_krw   KRW threshold of a 'big' contest
  notify_batch_size, notify_deadline_days, notify_report_hours   notices (cp/notify.py)
  audit_enabled   an adversarial audit of big-prize analyses; it needs an auditor agent (Grok), which the app does not
                  connect, so it is off by default
  gemini_daily_usd, ram_floor_gb, max_agents, grok, start   scheduler settings (not rendered into prompts)

  Contests ([공모전] items; chosen by the user in the app's "공모전 고르기" step, written to policy.json by the app).
  The values below are the defaults; no personal data.
  contest_fields          fields kept (CONTEST_FIELDS ids); a contest in no kept field is left out by the crawler
  contest_onsite_writing  'exclude' | 'allow'   writing or an exam at a venue (백일장, 현장 시험)
  contest_onsite_final    'exclude' | 'ask_over' | 'allow'   in-person presentation or judging round;
                          ask_over: ask the user when the top prize is over contest_prize_ask_krw or not stated
  contest_mail_only       'allow' | 'exclude'   entries taken only by post or in person (the user posts it)
  contest_ceremony        'allow' | 'exclude'   winners must attend the award ceremony
"""
import json
import os

try:
    from cp import config as _config
except Exception:  # pragma: no cover - prompts still work before config exists
    _config = None

DEFAULTS = {
    "foreign_scope": "email_phone_only",
    "x_actions": "all",
    "foreign_signup": "auto_all",
    "line_campaigns": "manual",
    "kr_signup": "allowed_list_only",
    "grok": "off",                         # scheduler only; Grok is not supported in the app
    "proposal_policy": "owner",            # the user accepts every analysis in the app
    "review_policy": "all",                # the user sees every finished work before it is submitted
    "review_min_prize_krw": 1000000,       # 'big' = top prize >= this (or unknown)
    "notify_batch_size": 5,                # notify when this many items wait (proposals, small-prize works)
    "notify_deadline_days": 3,             # an item whose deadline is this close is notified at once
    "notify_report_hours": [9, 21],        # daily report of what is waiting, local hours
    "audit_enabled": False,
    "notify_email": True,
    "gemini_daily_usd": 10,
    "ram_floor_gb": 4,
    "max_agents": 3,
    "contest_fields": ["write", "ai"],     # writing/idea and AI/data/dev
    "contest_onsite_writing": "exclude",
    "contest_onsite_final": "ask_over",    # excluded unless the top prize is over contest_prize_ask_krw
    "contest_prize_ask_krw": 5000000,
    "contest_mail_only": "allow",
    "contest_ceremony": "allow",
}

# Contest fields: id -> (Korean label shown in the app, keywords matched in the site's field text and the title).
CONTEST_FIELDS = {
    "write": ("글, 아이디어", r"문학|글|시나리오|수기|문예|에세이|수필|소설|아이디어|기획|논문|리포트|정책|제안|네이밍|슬로건|스토리|창작|카피|표어|독후감|감상문"),
    "ai": ("AI, 데이터, 개발", r"ai|인공지능|데이터|웹|모바일|it|sw|소프트웨어|게임|개발|해커톤|앱|프로그래밍|코딩|디지털|빅데이터"),
    "photo": ("사진", r"사진|포토"),
    "video": ("영상", r"영상|ucc|유튜브|숏폼|쇼츠|뮤직비디오"),
    "art": ("미술, 디자인", r"미술|그림|디자인|일러스트|웹툰|만화|캐릭터|회화|포스터"),
    "music": ("음악", r"음악|작곡|노래|가요|뮤지컬|밴드|싱어"),
    "supporters": ("서포터즈, 기자단", r"서포터즈|기자단|홍보대사|모니터링단|앰배서더"),
    "volunteer": ("봉사", r"봉사"),
    "other": ("그 밖의 분야", ""),
}
# Fields that are activities rather than contest work: a contest matching one of them is left out unless that field
# is kept, even when it also matches a kept field.
CONTEST_ACTIVITY_FIELDS = ("supporters", "volunteer")
CHOICES = {
    "foreign_scope": ("email_phone_only", "open_only", "try_all", "surveys_comments"),
    "x_actions": ("all", "follow_like", "none"),
    "foreign_signup": ("auto_all", "auto_major", "ask", "exclude"),
    "line_campaigns": ("manual", "exclude"),
    "kr_signup": ("allowed_list_only", "auto_simple"),
    "grok": ("off_until_needed", "on", "off"),
    "proposal_policy": ("owner", "auto"),
    "review_policy": ("big_prize", "all", "none"),
    "contest_onsite_writing": ("exclude", "allow"),
    "contest_onsite_final": ("exclude", "ask_over", "allow"),
    "contest_mail_only": ("allow", "exclude"),
    "contest_ceremony": ("allow", "exclude"),
}
FREE_TEXT = ("start",)  # scheduler settings kept as given
NUMERIC = ("gemini_daily_usd", "ram_floor_gb", "max_agents", "contest_prize_ask_krw", "review_min_prize_krw",
           "notify_batch_size", "notify_deadline_days")
BOOLEAN = ("line_registered", "audit_enabled", "notify_email")
LIST_OF_INT = ("notify_report_hours",)
STRING_LISTS = ("kr_signup_allow", "kr_signup_deny")  # user data: lists of KR site names, matched literally on Korean pages
METADATA = ("names",)

# Defaults are empty: the allow and deny lists are the user's own decisions (policy.json kr_signup_allow / kr_signup_deny).
# Site names are Korean because they are matched literally against Korean pages.
KR_SIGNUP_ALLOW = ()
KR_SIGNUP_DENY = ()
GIVEAWAY_PLATFORMS = ("gleam.io", "rafflecopter.com", "kingsumo.com", "woobox.com", "viralsweep.com")


def policy_path() -> str:
    base = _config.data_dir() if _config else (os.environ.get("CP_DATA_DIR")
                                               or (os.environ.get("CP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EntryPilot")) + r"\data")
    return os.path.join(base, "policy.json")


def load_policy(path: str | None = None) -> dict:
    """DEFAULTS merged with valid overrides from policy.json. Problems are listed, never swallowed."""
    pol = dict(DEFAULTS)
    pol["kr_signup_allow"], pol["kr_signup_deny"] = list(KR_SIGNUP_ALLOW), list(KR_SIGNUP_DENY)
    errors, unknown = [], []
    path = path or policy_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("policy.json is not a JSON object")
            for k, v in data.items():
                if k.startswith("_") or k.endswith("_note") or k in METADATA:
                    continue
                if k in CHOICES:
                    if v in CHOICES[k]:
                        pol[k] = v
                    else:
                        errors.append(f"invalid {k}={v!r}; kept {pol[k]!r}")
                elif k in NUMERIC:
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                        pol[k] = v
                    else:
                        errors.append(f"invalid {k}={v!r}; kept {pol[k]!r}")
                elif k in FREE_TEXT:
                    pol[k] = v
                elif k == "contest_fields":
                    if isinstance(v, list) and all(x in CONTEST_FIELDS for x in v):
                        pol[k] = list(v)
                    else:
                        errors.append(f"invalid {k}={v!r}; kept {pol[k]!r}")
                elif k in STRING_LISTS:
                    if isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v):
                        pol[k] = [x.strip() for x in v]
                    else:
                        errors.append(f"invalid {k}={v!r}; kept {pol[k]!r}")
                elif k in LIST_OF_INT:
                    if isinstance(v, list) and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 23
                                                   for x in v):
                        pol[k] = v
                    else:
                        errors.append(f"invalid {k}={v!r}; kept {pol[k]!r}")
                elif k in BOOLEAN:
                    if isinstance(v, bool):
                        pol[k] = v
                    else:
                        errors.append(f"invalid {k}={v!r}; kept {pol[k]!r}")
                else:
                    pol[k] = v
                    unknown.append(k)
        except Exception as e:  # unreadable file: defaults stay, the error is visible
            errors.append(f"cannot read {path}: {e}")
    if errors:
        pol["errors"] = errors
    if unknown:
        pol["unknown_keys"] = unknown
    return pol


POLICY = load_policy()
_BOOT_POLICY = dict(POLICY)


def active_policy(policy: dict | None = None) -> dict:
    """Return `policy` when given, else load the current policy.json (preserving any in-memory overrides on POLICY)."""
    if policy is not None:
        return policy
    cur = load_policy() if os.path.exists(policy_path()) else {
        **DEFAULTS,
        "kr_signup_allow": list(KR_SIGNUP_ALLOW),
        "kr_signup_deny": list(KR_SIGNUP_DENY),
    }
    for k, v in POLICY.items():
        if _BOOT_POLICY.get(k) != v:
            cur[k] = v
    return cur


def _valid_positive_prize(v) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    iv = int(v)
    return iv if iv > 0 else None


def prize_changed_after_accept(work: dict | None) -> bool:
    """True when the item's prize was modified after the user accepted the analysis."""
    w = work or {}
    if w.get("prize_changed_after_accept"):
        return True
    if "accepted_top_prize_krw" not in w:
        return False
    frozen = _valid_positive_prize(w.get("accepted_top_prize_krw"))
    cur_raw = w.get("top_prize_krw")
    if cur_raw is not None and (isinstance(cur_raw, bool) or not isinstance(cur_raw, (int, float))):
        return True
    if _valid_positive_prize(cur_raw) != frozen:
        return True
    if "accepted_top_prize" in w and (w.get("top_prize") or None) != (w.get("accepted_top_prize") or None):
        return True
    return False


def effective_prize_krw(item: dict) -> int | None:
    """Frozen `accepted_top_prize_krw` once accepted, else `top_prize_krw`. None/<=0 is unknown."""
    w = (item or {}).get("work") or {}
    if "accepted_top_prize_krw" in w or w.get("accepted_at"):
        return _valid_positive_prize(w.get("accepted_top_prize_krw"))
    return _valid_positive_prize(w.get("top_prize_krw"))


def needs_review(item: dict, policy: dict | None = None) -> bool:
    """True when a drafted work must wait for the user (state `review`) before it can be submitted.
    Uses frozen `accepted_top_prize_krw`; an unknown prize (None/0 before acceptance) or any prize change after
    acceptance always requires human review."""
    p = active_policy(policy)
    mode = p.get("review_policy", "all")
    if mode == "all":
        return True
    w = (item or {}).get("work") or {}
    if "accepted_top_prize_krw" not in w or prize_changed_after_accept(w):
        return True
    frozen = _valid_positive_prize(w.get("accepted_top_prize_krw"))
    if frozen is None:
        return True
    if mode == "none":
        return False
    return frozen >= p.get("review_min_prize_krw", 1000000)


def is_big(item: dict, policy: dict | None = None) -> bool:
    """Big-prize contest: frozen top prize >= review_min_prize_krw. An unknown or changed top prize counts as big."""
    p = active_policy(policy)
    w = (item or {}).get("work") or {}
    if prize_changed_after_accept(w):
        return True
    top = effective_prize_krw(item)
    if top is None:
        return True
    return top >= p.get("review_min_prize_krw", 1000000)


def _scope_lines(p: dict, region: str, kind: str) -> list:
    s = p["foreign_scope"]
    if s == "email_phone_only":
        if kind == "scout":
            return [f"Eligibility ({region}): prefer entries that ask only for email and/or phone. Still list entries "
                    "that need a local address, residency or local phone, with eligibility residency_required."]
        return [f"Eligibility ({region}): enter when only email and/or phone (plus name, age, country) are needed. "
                "Local mailing address or residency required (form or terms): not exclude but manual --need residency "
                "--reason '<what>'; local phone number required: manual --need local_phone --reason '<what>'. "
                "Never invent an address, postcode, state or local number."]
    return [f"Eligibility ({region}): " + {
        "open_only": "enter only if the notice explicitly accepts entrants outside the country; residency_required "
                     "or unclear: exclude.",
        "try_all": "enter anything not residency_required; residency_required: exclude.",
        "surveys_comments": "enter only surveys and comment entries that are not residency_required; exclude others.",
    }[s]]


def contest_field_ok(text: str, policy: dict | None = None) -> bool:
    """True when a contest whose field text / title is `text` belongs to a field the user kept."""
    import re
    p = policy or POLICY
    keep = set(p.get("contest_fields") or [])
    t = (text or "").lower()
    hit = {k for k, (_, pat) in CONTEST_FIELDS.items() if pat and re.search(pat, t)}
    if any(k in hit and k not in keep for k in CONTEST_ACTIVITY_FIELDS):
        return False
    if hit & keep:
        return True
    return not hit and "other" in keep


def contest_lines(p: dict, kind: str) -> list:
    """Rules for [공모전] items (verify and submit prompts). The crawler already applied the field choice."""
    if kind == "scout":
        labels = [CONTEST_FIELDS[k][0] for k in p.get("contest_fields") or [] if k in CONTEST_FIELDS]
        skip = ["contests outside the fields the user wants (" + (", ".join(labels) or "none") + ")"]
        if p.get("contest_onsite_writing", "exclude") == "exclude":
            skip.append("writing or exams taken at a venue (백일장, 현장 시험)")
        if p.get("contest_onsite_final", "ask_over") == "exclude":
            skip.append("contests with an in-person final round")
        if p.get("contest_mail_only") == "exclude":
            skip.append("entries taken only by post or in person")
        if p.get("contest_ceremony") == "exclude":
            skip.append("contests where winners must attend a ceremony")
        return ["Also skip: " + "; ".join(skip) + "."]
    rules = []
    w = p.get("contest_onsite_writing", "exclude")
    rules.append("writing or an exam at a venue (백일장, 현장 시험): " + ("exclude 'onsite writing'" if w == "exclude" else "allowed; manual --need other --reason 'on-site writing: <date, place>'"))
    f = p.get("contest_onsite_final", "ask_over")
    lim = int(p.get("contest_prize_ask_krw") or 0)
    rules.append("in-person presentation, interview or demo in a final round (online final is fine): " + {
        "exclude": "exclude 'on-site final'",
        "ask_over": f"if the largest single prize is over {lim:,} KRW or no amount is stated, cp owner --question "
                    "'on-site final <date, place>, top prize <amount>: enter?'; otherwise exclude 'on-site final'",
        "allow": "allowed; write the date and place in --notes",
    }[f])
    rules.append("entries taken only by post or in person: " + ("exclude 'mail only'" if p.get("contest_mail_only") == "exclude" else "allowed; when the work is ready, manual --need other --reason 'post the entry: <address, deadline>'"))
    rules.append("winners must attend the award ceremony: " + ("exclude 'ceremony required'" if p.get("contest_ceremony") == "exclude" else "allowed; note it in --notes"))
    labels = [CONTEST_FIELDS[k][0] for k in p.get("contest_fields") or [] if k in CONTEST_FIELDS]
    rules.append("fields the user wants: " + (", ".join(labels) or "none") + "; a contest outside them: exclude 'field not chosen'")
    return ["Contests ([공모전] items), judged only from the notice text, quoting the sentence: " + "; ".join(rules) + "."]


def policy_lines(region: str, kind: str = "submit", policy: dict | None = None) -> str:
    """Short English sentences with the policy rules relevant to `region` and task `kind`."""
    p = policy or POLICY
    region = (region or "KR").upper()
    if kind in ("draft", "audit"):
        return ""
    out = []
    if region == "KR":
        allow, deny = list(p.get("kr_signup_allow") or []), list(p.get("kr_signup_deny") or [])
        refused = (" Refused sites, exclude: " + ", ".join(deny) + ".") if deny else ""
        easy = " Easy login with the account logged in in the entry browser and channel adds are not sign-ups."
        if p.get("kr_signup") == "auto_simple":
            # Agents may self-register on KR sites that take email or the logged-in Naver/Kakao, as long as no phone
            # identity check / government auth / payment is needed.
            if kind == "verify":
                out.append("Sign-ups (KR): you MAY register on a site that accepts email or the logged-in Naver/Kakao "
                           "without 휴대폰 본인인증; then write 'sign-up allowed: <site>' in --notes. A site that needs "
                           "휴대폰 본인인증, 정부 간편인증(PASS) or payment to register: manual --need login --reason "
                           "'<what> (<url>)'." + refused + easy)
            elif kind != "scout":
                out.append("Sign-ups (KR): register with email or the logged-in Naver/Kakao when no 휴대폰 본인인증 is "
                           "required (password via cp secret set <SITEKEY>_PW --domain <exact https host> --generate 16; cp consent add --kind "
                           "member_signup; optional marketing/third-party consent off). Needs 휴대폰 본인인증, 정부 인증 "
                           "or payment: manual --need login. Logging in with the account in the entry browser and "
                           "channel adds are not sign-ups.")
        elif kind == "verify":
            if allow:
                out.append("Sign-ups (KR): only on allowed sites: " + ", ".join(allow) + "; then write "
                           "'sign-up allowed: <site>' in --notes." + refused + " Any other KR site needing a new "
                           "account: manual --need login --reason 'sign-up not yet allowed: <site> (<url>)'." + easy)
            else:
                out.append("Sign-ups (KR): no new account is allowed. A KR site needing a new account: manual --need "
                           "login --reason 'sign-up not yet allowed: <site> (<url>)'." + refused + easy)
        elif kind != "scout":
            # The verifier saw the allow list and marked allowed sites in the notes (keeps submit prompts short).
            out.append("Sign-ups (KR): only when the item notes say 'sign-up allowed' (legacy '가입 허용'); otherwise "
                       "manual --need login --reason 'sign-up not yet allowed: <site> (<url>)'. Logging in with the "
                       "account in the entry browser and channel adds are not sign-ups.")
    else:
        out += _scope_lines(p, region, kind)
        if kind != "scout":
            out.append(f"Sign-ups ({region}): " + {
                "auto_all": "you may create an account on any site with the profile email (password via cp secret "
                            "set <SITEKEY>_PW --domain <exact https host> --generate 16); log each with cp consent add --kind member_signup.",
                "auto_major": "social sign-in with the account logged in in the entry browser, or a new account on "
                              + ", ".join(GIVEAWAY_PLATFORMS) + " (cp consent add --kind member_signup); any other new "
                              "account: manual --need login 'sign-up not yet allowed: <site>'.",
                "ask": "no new accounts: manual --need login 'sign-up not yet allowed: <site>'.",
                "exclude": "no new accounts: exclude 'needs sign-up'.",
            }[p["foreign_signup"]])
    out += contest_lines(p, kind)
    out.append({"all": "X: the X account logged in in the entry browser may follow, repost, like and reply.",
                "follow_like": "X: follow and like only; an entry needing repost or reply: exclude.",
                "none": "X: no actions; an entry needing any X action: exclude."}[p["x_actions"]])
    if region == "JP":
        if p["line_campaigns"] == "exclude":
            out.append("LINE friend-add campaigns: exclude 'LINE campaign'.")
        else:
            out.append("LINE-only campaigns: manual --need app --reason 'LINE friend add'.")
    return "\n".join("- " + s for s in out)


if __name__ == "__main__":
    import sys
    r = sys.argv[1] if len(sys.argv) > 1 else "KR"
    k = sys.argv[2] if len(sys.argv) > 2 else "submit"
    print(json.dumps({k2: v for k2, v in POLICY.items()}, ensure_ascii=False))
    print(policy_lines(r, k))
