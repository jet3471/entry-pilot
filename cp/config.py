"""Paths, constants, regions, models, parallelism and RAM gate values.

Path functions read the environment on every call so tests can point CP_DATA_DIR / CP_OWNER_DIR at a temp dir.
The module-level constants (DATA_DIR, OWNER_DIR, ...) are evaluated once at import for convenience.
"""
import os

# Entry Pilot contest engine (package `cp`, ported from the author's contest engine). Every path comes from the
# environment the app sets before it starts the engine (app/engine.py, CP_*); nothing points at one person's PC.
STANDALONE = True
HOME = os.path.expanduser("~")
APP_DIR = os.environ.get("CP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or HOME, "EntryPilot")
DEFAULT_DATA_DIR = os.path.join(APP_DIR, "data", "contest")
DEFAULT_OWNER_DIR = os.path.join(APP_DIR, "data", "contest", "board")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_SRC = os.path.join(PROJECT_DIR, "prompts_contest")  # the contest prompts


def data_dir() -> str:
    return os.environ.get("CP_DATA_DIR") or DEFAULT_DATA_DIR


def owner_dir() -> str:
    return os.environ.get("CP_OWNER_DIR") or DEFAULT_OWNER_DIR


def db_path() -> str:
    return os.path.join(data_dir(), "platform.db")


def lock_path() -> str:
    return os.path.join(data_dir(), "platform.lock")


def profile_path() -> str:
    # the app keeps one profile for both engines (CP_PROFILE_PATH); without it the contest data folder has its own
    return os.environ.get("CP_PROFILE_PATH") or os.path.join(data_dir(), "profile.json")


def consents_path() -> str:
    # Legacy consent log; owned by cp/consent.py (not migrated into SQLite).
    return os.path.join(data_dir(), "consents.jsonl")


def logs_dir() -> str:
    return os.path.join(data_dir(), "logs")


def scout_inbox_dir() -> str:
    return os.path.join(data_dir(), "scout-inbox")


def tools_path() -> str:
    """The list of tools agents may use (programs, CLIs, browser control); agents read it instead of asking the user."""
    return os.path.join(data_dir(), "tools.json")


def owner_facts_path() -> str:
    """Facts about the user that drafting agents may use in works (the user may write this file; optional)."""
    return os.path.join(owner_dir(), "내_정보.md")


def notices_dir() -> str:
    """The outbox: one JSON file per notice, sent and deleted by the app (cp/notify.py)."""
    return os.path.join(data_dir(), "notices")


def drafts_dir() -> str:
    return os.path.join(data_dir(), "drafts")


def prompts_dir() -> str:
    return os.path.join(data_dir(), "prompts")


def analysis_dir() -> str:
    """One analysis dossier per contest: <data>\\analysis\\<ID>.md (written by `cp verify --analysis-file`)."""
    return os.path.join(data_dir(), "analysis")


def known_events_path() -> str:
    return os.path.join(data_dir(), "known-events.txt")


def owner_xlsx_path() -> str:
    return os.path.join(owner_dir(), "직접_참여_목록.xlsx")


def owner_html_path() -> str:
    return os.path.join(owner_dir(), "현황판.html")


def full_list_html_path() -> str:
    return os.path.join(owner_dir(), "전체목록.html")


DATA_DIR = data_dir()
OWNER_DIR = owner_dir()

# Owner xlsx layout (shared with cp/render.py). Sheet and column names are the user's Korean UI strings.
XLSX_SHEET_MANUAL = "직접 참여"
XLSX_SHEET_OWNER = "확인 필요"
XLSX_COL_ID = "ID"
XLSX_COL_DONE = "내 처리"
XLSX_COL_ANSWER = "내 답"
XLSX_DONE_VALUES = ("했음", "완료", "O", "o", "done")
XLSX_SKIP_VALUES = ("안함", "안 함", "제외", "X", "x", "skip")
# Third sheet: , drafted works waiting for approval (state `review`).
XLSX_SHEET_REVIEW = "검토 필요"
XLSX_APPROVE_VALUES = ("승인", "approve", "ok", "OK", "Ok", "좋음")
XLSX_REVISE_VALUES = ("수정", "revise", "다시")

# ---------- regions ----------
REGIONS = {
    "KR": {"rank": 1, "tz": "Asia/Seoul", "lang": "ko"},
    "JP": {"rank": 2, "tz": "Asia/Tokyo", "lang": "ja"},
    "US": {"rank": 3, "tz": "America/New_York", "lang": "en"},
}
DEFAULT_REGION = "KR"
KST_TZ = "Asia/Seoul"
PLATFORM_START = "2026-11-08"  # contests with an earlier deadline are never added
ITEM_PREFIX = "C"  # item ids C001..; pool ids stay P001..

# ---------- agents, capabilities, lanes ----------
AGENTS = ("claude", "codex", "gemini", "grok")
# The AIs the user connected in the app (comma list); the others are blocked for the whole run (scheduler start).
ENABLED_AGENTS = tuple(a for a in os.environ.get("CP_AGENTS", "claude,codex").split(",") if a in AGENTS)
ACTORS = AGENTS + ("owner", "system")  # "owner" is the internal name of the user's own actions
LANES = ("form", "browser")  # no phone lanes
CAPABILITIES = {  # (assumption)
    "claude": {"form", "browser"},
    "codex": {"form", "browser"},
    "grok": {"form", "browser"},
    "gemini": {"form"},
}
# residency / local_phone: foreign forms needing a local address or a local phone number
# unsupported: the work needs a tool this app does not have (HWP/HWPX files, image or video generation); the user does it
MANUAL_NEEDS = ("login", "identity", "payment", "sns", "upload", "app", "kakao", "residency", "local_phone", "unsupported", "other")

# Agent preference order per job kind. Grok is not supported in the app (it needs a paid account the
# app does not connect), so the audit step has no agent and is skipped (see AUDIT_AGENTS).
AGENT_PREFERENCE = {
    "draft": ("claude", "codex"),  # writing quality first (assumption)
    "verify": ("codex", "claude"),  # the analysis dossier
    "audit": ("grok",),
    "submit_browser": ("codex", "claude"),
    "submit_form": ("codex", "claude", "gemini"),
    "scout": ("claude", "codex", "gemini"),  # the scout is a crawler plus one judging call (assumption)
}
AUDIT_AGENTS = ("grok",)  # an audit only runs when one of these is connected; the app never connects them

# ---------- models and accounts ----------
MODELS = {
    "claude": os.environ.get("CP_CLAUDE_MODEL") or "claude-sonnet-5-5",
    "codex": os.environ.get("CP_CODEX_MODEL") or "gpt-6.1-terra",
    "gemini": os.environ.get("CP_GEMINI_MODEL") or "gemini-2.5-flash",  # official Gemini CLI (assumption)
    "grok": "grok-4.7",
    "translate": "claude-haiku-5-5",
}
CODEX_EFFORT = {"verify": "medium", "audit": "medium", "scout": "low", "draft": "high", "submit": "medium"}  # (assumption)
GROK_EFFORT = "low"
# One login folder per account (CP_*_DIRS / CP_CODEX_HOMES, os.pathsep separated, account 1 first), set by the app.
CODEX_HOMES = [d for d in (os.environ.get("CP_CODEX_HOMES") or "").split(os.pathsep) if d] \
    or [os.environ.get("CP_CODEX_HOME") or os.path.join(APP_DIR, "logins", "codex")]
CLAUDE_CONFIG_DIRS = [d for d in (os.environ.get("CP_CLAUDE_CONFIG_DIRS") or "").split(os.pathsep) if d] \
    or [os.environ.get("CP_CLAUDE_CONFIG_DIR") or os.path.join(APP_DIR, "logins", "claude")]
CLAUDE_ACCOUNTS = tuple(str(i + 1) for i in range(len(CLAUDE_CONFIG_DIRS)))  # "1", "2", ...
CLAUDE_CONFIG_DIR = CLAUDE_CONFIG_DIRS[0]
GEMINI_SCOUT_MODEL = ""  # empty: the scout uses the same Gemini model as every other job
GEMINI_SCOUT_EFFORT = "medium"
GEMINI_CLI = ""  # official Gemini CLI from the app's runtime folder (agents/gemini.py)
GROK_EXE = ""  # Grok is not supported in the app
AGENT_CWD = os.path.join(data_dir(), "work")  # empty folder: no CLAUDE.md, AGENTS.md or .codex project config
# Hook for external tools (LCH-WIKI, LLM-RELAY, LLM-CONSOLE: not supported yet). When CP_INTEGRATIONS_FILE points to a
# JSON file, a future version reads it; nothing in the engine talks to those tools now, and it is unset by default.
INTEGRATIONS_FILE = os.environ.get("CP_INTEGRATIONS_FILE") or ""

# ---------- scheduler, parallelism, RAM gate ----------
LOOP_SEC = 15
TRANSLATE_PHASE = True  # the scheduler starts `cp translate --dossiers` for untranslated dossiers (tests turn it off)
RAM_FLOOR_GB = 4.0  # (assumption)
MAX_AGENTS = int(os.environ.get("CP_MAX_AGENTS") or 3)  # one personal login per AI (assumption)
PER_AGENT_MAX = {"codex": 2, "claude": 2, "gemini": 1}
MAX_BROWSER = 2  # (assumption) a home PC
PIPELINE_MAX = 30  # proposals and drafts waiting for the user stop scouting when this many wait
ACTIVE_REGIONS = ("KR",)  # Korea only; JP and US scouting stays off
SCOUT_QUOTA = {"KR": 3, "JP": 0, "US": 0}  # per round (assumption)
SYNC_XLSX_SEC = 60
PUBLISH_SEC = 600  # (assumption)
TAB_MAX_AGE_MIN = 25
VERIFY_MAX_TRIES = 2  # per agent kind
SUBMIT_MAX_FAILS = 4  # the 4th failure moves the item to manual/other
NO_RESULT_MAX = 3
DRAFT_MAX_TRIES = 3  # draft runs per item before it goes to the user (a revision request resets the count)
# Notices (cp/notify.py). The app sends them (Telegram, e-mail or none); the engine only decides when one is due.
NOTIFY_RETRY_SEC = 600  # after a notice failed on every channel, wait this long before trying again
AUDIT_MAX_TRIES = 2  # Grok audit runs without a verdict per item before the audit is marked skipped (visible to the user)
AUDIT_MAX_FAILS = 2  # audit verdict "fail" this many times: the item goes to the user instead of another re-analysis

# ---------- prompts / publishing ----------
PROMPT_INLINE_MAX = 3000  # longer prompts go to <data>\prompts\<kind>-<id>-<agent>.md and only the path is sent
# No public status page in the standalone build.
PUBLISH_URL = ""
PUBLISH_KEY = ""
PUBLISH_ENABLED = False
ACTUATOR_ENABLED = False

# ---------- eligibility phrases (extend freely; matched case-insensitively) ----------
ELIGIBILITY_RESIDENCY_PHRASES = [
    "legal residents of the united states",
    "legal resident of the united states",
    "us residents only",
    "u.s. residents only",
    "50 united states",
    "open only to residents of",
    "日本国内在住",
    "日本在住の方",
    "国内にお住まい",
    "국내 거주",
    "ships only within",
    "japan only",
    "us only",
    "u.s. only",
]
ELIGIBILITY_OPEN_PHRASES = [
    "worldwide",
    "international",
    "open to all",
    "海外",
    "全世界",
    "どなたでも",
]

# ---------- site kinds (host suffix -> kind); checked in order ----------
SITE_KIND_HOSTS = [
    ("docs.google.com/forms", "google_form"),
    ("forms.gle", "google_form"),
    ("form.naver.com", "naver_form"),
    ("naver.me", "naver_form"),
    ("moaform.com", "moaform"),
    ("nownsurvey.com", "nownsurvey"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("blog.naver.com", "naver_blog"),
    ("instagram.com", "instagram"),
    ("x.com", "x"),
    ("twitter.com", "x"),
    ("facebook.com", "facebook"),
    ("threads.net", "threads"),
    ("threads.com", "threads"),
    ("pf.kakao.com", "kakao"),
    ("open.kakao.com", "kakao"),
    ("eventhouse.kr", "aggregator"),
    ("supertoday.co.kr", "aggregator"),
    ("gleam.io", "gleam"),
    ("rafflecopter.com", "gleam"),
    ("kingsumo.com", "gleam"),
    ("woobox.com", "gleam"),
    ("viralsweep.com", "gleam"),
    ("surveymonkey.com", "site_form"),
    ("typeform.com", "site_form"),
    ("forms.office.com", "site_form"),
    ("qualtrics.com", "site_form"),
]
SITE_KINDS = ("google_form", "naver_form", "moaform", "nownsurvey", "site_form", "youtube", "naver_blog", "instagram",
              "x", "facebook", "threads", "kakao", "aggregator", "gleam", "jp_campaign", "other")

# ---------- scheduler runtime (builder A) ----------
# All values below are assumptions unless noted.
SCHEDULER_PID_KEY = "scheduler.pid"        # kv key of the single-instance guard
BACKFILL_EVERY_SEC = 600                   # at most one backfill run per 10 minutes
BACKFILL_LIMIT = 20                        # items per backfill run
GEMINI_SCOUT_RUN_USD = 0.0                 # no cost estimate: the app uses the user's own Gemini login or key
# A run that lives longer than this is killed (taskkill /T) and recorded as outcome `timeout` (assumption).
RUN_TIMEOUT_SEC = {"verify": 2400, "audit": 1500, "draft": 3600, "submit": 2700, "scout": 2700, "backfill": 900, "render": 900}
RUN_TIMEOUT_DEFAULT_SEC = 2700
CRASH_BLOCK_AFTER = 3                      # consecutive crashes of one agent (one scheduler process) before it is blocked
CHROME_ENSURE_TIMEOUT_SEC = 25             # a Chrome start must not stall the loop longer than this
SCOUT_MAX_ACTIVE = 2                       # concurrent scout runs
SCOUT_MIN_INTERVAL_SEC = 180               # crawler scouts end fast: at most one start per 3 minutes 
CRAWLER_SCOUT = True                       # KR scout = Python crawler + one judging call 
ACTUATOR_POLL_SEC = 30
RENDER_UNAVAILABLE_KEY = "render_unavailable"
BACKFILL_UNAVAILABLE_KEY = "backfill_unavailable"


def utf8_stdio():
    """Make stdout and stderr UTF-8. Streams that cannot be reconfigured (replaced by a test or a pipe wrapper) are left
    as they are: that is not a failure of the command."""
    import sys
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
