"""Paths, constants, regions, models, parallelism and RAM gate values.

Path functions read the environment on every call so tests can point EP_DATA_DIR / EP_OWNER_DIR at a temp dir.
The module-level constants (DATA_DIR, OWNER_DIR, ...) are evaluated once at import for convenience.
"""
import os

# Entry Pilot (standalone build): every path comes from the environment the app sets before it starts the engine
# (app/engine.py). Nothing here points at one person's PC.
STANDALONE = True
HOME = os.path.expanduser("~")
APP_DIR = os.environ.get("EP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or HOME, "EntryPilot")
DEFAULT_DATA_DIR = os.path.join(APP_DIR, "data")
DEFAULT_OWNER_DIR = os.path.join(APP_DIR, "data", "board")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    return os.environ.get("EP_DATA_DIR") or DEFAULT_DATA_DIR


def owner_dir() -> str:
    return os.environ.get("EP_OWNER_DIR") or DEFAULT_OWNER_DIR


def db_path() -> str:
    return os.path.join(data_dir(), "platform.db")


def lock_path() -> str:
    return os.path.join(data_dir(), "platform.lock")


def profile_path() -> str:
    return os.path.join(data_dir(), "profile.json")


def legacy_board_path() -> str:
    return os.path.join(data_dir(), "board.json")


def legacy_runs_path() -> str:
    return os.path.join(data_dir(), "runs.json")


def consents_path() -> str:
    # Legacy consent log; owned by ep/consent.py (not migrated into SQLite).
    return os.path.join(data_dir(), "consents.jsonl")


def logs_dir() -> str:
    return os.path.join(data_dir(), "logs")


def scout_inbox_dir() -> str:
    return os.path.join(data_dir(), "scout-inbox")


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

# xlsx layout of the user's list (shared with ep/render.py). Sheet and column names are Korean UI strings.
XLSX_SHEET_MANUAL = "직접 참여"
XLSX_SHEET_OWNER = "확인 필요"
XLSX_COL_ID = "ID"
XLSX_COL_DONE = "내 처리"
XLSX_COL_ANSWER = "내 답"
XLSX_DONE_VALUES = ("했음", "완료", "O", "o", "done")
XLSX_SKIP_VALUES = ("안함", "안 함", "제외", "X", "x", "skip")

# ---------- regions ----------
REGIONS = {
    "KR": {"rank": 1, "tz": "Asia/Seoul", "lang": "ko"},
    "JP": {"rank": 2, "tz": "Asia/Tokyo", "lang": "ja"},
    "US": {"rank": 3, "tz": "America/New_York", "lang": "en"},
}
DEFAULT_REGION = "KR"
KST_TZ = "Asia/Seoul"
PLATFORM_START = ""  # when set (YYYY-MM-DD), `ep migrate` excludes items with an earlier deadline

# ---------- agents, capabilities, lanes ----------
AGENTS = ("claude", "codex", "gemini", "grok")
# The AIs the user connected in the app (comma list); the others are blocked for the whole run (scheduler start).
ENABLED_AGENTS = tuple(a for a in os.environ.get("EP_AGENTS", "claude,codex").split(",") if a in AGENTS)
ACTORS = AGENTS + ("owner", "system")
LANES = ("form", "browser")
CAPABILITIES = {
    "claude": {"form", "browser"},
    "codex": {"form", "browser"},
    "grok": {"form", "browser"},
    "gemini": {"form"},
}
# residency / local_phone: foreign forms needing a local address or a local phone number
MANUAL_NEEDS = ("login", "identity", "payment", "sns", "upload", "app", "kakao", "residency", "local_phone", "other")

# Agent preference order per job kind.
AGENT_PREFERENCE = {
    "verify": ("codex", "claude", "grok", "gemini"),
    "submit_browser": ("codex", "claude", "grok"),
    "submit_form": ("gemini", "codex", "claude", "grok"),
    "scout": ("claude", "codex", "gemini", "grok"),  # Gemini can hit 429 under load; the judge call is cheap anywhere
}

# ---------- models and accounts ----------
MODELS = {
    "claude": os.environ.get("EP_CLAUDE_MODEL") or "claude-sonnet-5-5",
    "codex": os.environ.get("EP_CODEX_MODEL") or "gpt-6.1-terra",
    "gemini": os.environ.get("EP_GEMINI_MODEL") or "gemini-2.5-flash",  # official Gemini CLI
    "grok": "grok-4.7",
    "translate": "claude-haiku-5-5",
}
CODEX_EFFORT = {"verify": "low", "scout": "low", "submit": "medium"}
GROK_EFFORT = "low"
# Entry Pilot: one login folder per account (EP_*_DIRS / EP_CODEX_HOMES, os.pathsep separated, account 1 first).
# When an account hits its limit the engine moves to the next (accounts.codex_switch, accounts.claude_pick).
CODEX_HOMES = [d for d in (os.environ.get("EP_CODEX_HOMES") or "").split(os.pathsep) if d] \
    or [os.environ.get("EP_CODEX_HOME") or os.path.join(APP_DIR, "logins", "codex")]
CLAUDE_CONFIG_DIRS = [d for d in (os.environ.get("EP_CLAUDE_CONFIG_DIRS") or "").split(os.pathsep) if d] \
    or [os.environ.get("EP_CLAUDE_CONFIG_DIR") or os.path.join(APP_DIR, "logins", "claude")]
CLAUDE_ACCOUNTS = tuple(str(i + 1) for i in range(len(CLAUDE_CONFIG_DIRS)))  # "1", "2", ...
CLAUDE_CONFIG_DIR = CLAUDE_CONFIG_DIRS[0]
GEMINI_CLI = ""  # official Gemini CLI from the app's runtime folder (agents/gemini.py)
GROK_EXE = os.path.join(HOME, ".grok", "bin", "grok.exe")
AGENT_CWD = os.path.join(data_dir(), "work")  # empty folder: no CLAUDE.md, AGENTS.md or .codex project config

# ---------- scheduler, parallelism, RAM gate ----------
LOOP_SEC = 15
RAM_FLOOR_GB = 4.0
MAX_AGENTS = int(os.environ.get("EP_MAX_AGENTS") or 3)  # one personal login per AI
PER_AGENT_MAX = {"codex": 2, "claude": 2, "gemini": 1}
MAX_BROWSER = 2  # a home PC
PIPELINE_MAX = 40
ACTIVE_REGIONS = ("KR",)  # Korea only; JP and US are excluded from scouting, promotion and the board
SCOUT_QUOTA = {"KR": 3, "JP": 0, "US": 0}  # per round
SYNC_XLSX_SEC = 60
RENDER_SEC = 600  # how often the status page and xlsx are rebuilt when something changed
TAB_MAX_AGE_MIN = 25
VERIFY_MAX_TRIES = 2  # per agent kind
SUBMIT_MAX_FAILS = 4  # the 4th failure moves the item to manual/other
IG_MAX_TRIES = 3
NO_RESULT_MAX = 3

# ---------- prompts ----------
PROMPT_MAX_CHARS = 9000

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

# ---------- scheduler runtime ----------
SCHEDULER_PID_KEY = "scheduler.pid"        # kv key of the single-instance guard
BACKFILL_EVERY_SEC = 600                   # at most one backfill run per 10 minutes
BACKFILL_LIMIT = 20                        # items per backfill run
GEMINI_SCOUT_RUN_USD = 1.5                 # estimated USD per Gemini scout run (the CLI reports no cost; midpoint estimate)
# A run that lives longer than this is killed (taskkill /T) and recorded as outcome `timeout`.
RUN_TIMEOUT_SEC = {"verify": 1800, "submit": 2700, "scout": 2700, "backfill": 900, "render": 900}
RUN_TIMEOUT_DEFAULT_SEC = 2700
CRASH_BLOCK_AFTER = 3                      # consecutive crashes of one agent (one scheduler process) before it is blocked
CHROME_ENSURE_TIMEOUT_SEC = 25             # a Chrome start must not stall the loop longer than this
SCOUT_MIN_INTERVAL_SEC = 180
CRAWLER_SCOUT = True  # KR scout = crawler + one judging call
SCOUT_MAX_ACTIVE = 3  # concurrent scout runs
RENDER_UNAVAILABLE_KEY = "render_unavailable"
BACKFILL_UNAVAILABLE_KEY = "backfill_unavailable"
