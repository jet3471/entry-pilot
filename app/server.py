"""Local web server for the app window (127.0.0.1 only, random port, per-launch token).

The window is Edge/Chrome in --app mode pointed at http://127.0.0.1:<port>/?c=<launch_code>. The page exchanges the
one-time launch code once via POST /api/session for the real token kept in memory, then strips it from the URL.
"""
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app import deps, engine, logins, notify, paths, settings, telegram

TOKEN = secrets.token_urlsafe(24)
OPEN_CODE = secrets.token_urlsafe(24)
LAUNCH_TTL = 300
SOCKET_TIMEOUT = 15
REQUEST_DEADLINE = 30
XLSX_SYNC_INTERVAL = 60
MAX_THREADS = 32
MAX_JSON_BYTES = 2 * 1024 * 1024
STREAM_CHUNK = 64 * 1024
UPLOAD_ROUTES = {"/api/upload": 30 * 1024 * 1024, "/api/item/upload": 30 * 1024 * 1024}
PORT = [0]
_launch_lock = threading.Lock()
_LAUNCH_CODES: dict[str, float] = {}
_xlsx_sync_lock = threading.Lock()
_XLSX_SYNC_STATE: dict[str, dict] = {}
_LOG_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
_LOG_SPACED_PW_RE = re.compile(
    r"(?<![A-Za-z0-9_])[a-z]{4}[ \u00a0]+[a-z]{4}[ \u00a0]+[a-z]{4}[ \u00a0]+[a-z]{4}(?![A-Za-z0-9_])"
)
_LOG_PW_CONTEXT_RE = re.compile(r"(?:app[ \t]+passwords?|apppasswords|앱[ \t]*비밀번호)", re.IGNORECASE)
_LOG_UNSPACED_PW_RE = re.compile(r"(?<![A-Za-z0-9_])[a-z]{16}(?![A-Za-z0-9_])")
_STATIC_MAILBOT_ERR_PREFIX = "AI가 앱 비밀번호를 만들지 못했습니다"
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; "
           "base-uri 'none'; frame-ancestors 'none'")
FILE_CSP = "default-src 'none'; sandbox"
RASTER_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/logo.svg": ("logo.svg", "image/svg+xml"),
          "/fonts/PretendardVariable.woff2": (os.path.join("fonts", "PretendardVariable.woff2"), "font/woff2"),
          **{f"/guide/tg{i}.png": (os.path.join("guide", f"tg{i}.png"), "image/png") for i in range(1, 6)},
          **{f"/guide/gm{i}.png": (os.path.join("guide", f"gm{i}.png"), "image/png") for i in range(1, 3)}}
_CLIP_TRIED = [""]  # digest of the last clipboard app password tried (the password itself is never kept on disk)
_MAIL_CLIP = {"pw": "", "masked": ""}  # detected clipboard app password awaiting user confirmation (in memory only)
_TG_CLIP = {"tok": "", "info": None}  # verified clipboard bot candidate awaiting user confirmation (in memory only)
LAST_BEAT = [time.time()]
LOG_LINES: list = []


class FileResponse:
    def __init__(self, path: str, ctype: str, headers: dict | None = None):
        self.path = path
        self.ctype = ctype
        self.headers = headers


def new_launch_code() -> str:
    now = time.time()
    with _launch_lock:
        for k, t in list(_LAUNCH_CODES.items()):
            if now - t > LAUNCH_TTL:
                _LAUNCH_CODES.pop(k, None)
        code = secrets.token_urlsafe(24)
        _LAUNCH_CODES[code] = now
        return code


def exchange_launch_code(code) -> str | None:
    if not isinstance(code, str) or not code:
        return None
    now = time.time()
    with _launch_lock:
        issued = _LAUNCH_CODES.pop(code, None)
    if issued is None or issued <= 0 or now - issued > LAUNCH_TTL:
        return None
    return TOKEN


def open_window(port: int | None = None) -> dict:
    p = port or PORT[0]
    if not p:
        return {"ok": False, "error": "no port"}
    LAST_BEAT[0] = time.time()
    code = new_launch_code()
    from app import main as app_main
    app_main._open_window(f"http://127.0.0.1:{p}/?c={code}")
    return {"ok": True}


def _scrub_log_line(msg: str) -> str:
    s = _LOG_TOKEN_RE.sub("[hidden]", str(msg or ""))
    s = _LOG_SPACED_PW_RE.sub("[hidden]", s)
    if _LOG_PW_CONTEXT_RE.search(s):
        out = []
        for ln in s.splitlines(keepends=True):
            probe = ln.replace(_STATIC_MAILBOT_ERR_PREFIX, "")
            out.append(_LOG_UNSPACED_PW_RE.sub("[hidden]", ln) if _LOG_PW_CONTEXT_RE.search(probe) else ln)
        s = "".join(out)
    return s


def log(msg: str):
    line = f"{dt.datetime.now():%m-%d %H:%M:%S} {_scrub_log_line(msg)}"
    LOG_LINES.append(line)
    del LOG_LINES[:-300]
    try:
        with open(os.path.join(paths.logs_dir(), "app.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------- background tasks (installs) ----------
TASKS: dict = {}


def run_task(name: str, fn):
    t = TASKS.get(name)
    if t and t["state"] == "running":
        return
    TASKS[name] = {"state": "running", "pct": 0, "text": "시작", "error": ""}

    def report(pct, text):
        TASKS[name].update(pct=int(pct), text=text)

    def body():
        try:
            res = fn(report)
            if isinstance(res, dict) and res.get("waiting"):
                TASKS[name].update(state="waiting", warning=str(res.get("warning") or ""))
                log(f"task {name}: waiting")
            else:
                TASKS[name].update(state="done", pct=100)
                log(f"task {name}: done")
        except deps.StepError as e:
            TASKS[name].update(state="error", error=str(e))
            log(f"task {name}: {e}")
        except Exception as e:
            TASKS[name].update(state="error", error=f"{type(e).__name__}: {e}")
            log(f"task {name}: {traceback.format_exc()}")
        finally:
            _login_cache["at"] = 0.0  # read the logins again now that the tools may have changed
    threading.Thread(target=body, daemon=True, name=f"task-{name}").start()


def task_install_all(report):
    s = settings.load()
    agents = [a for a in ("claude", "codex", "gemini") if (s.get("agents") or {}).get(a)]
    steps = [("Node.js", deps.install_node)]
    if "claude" in agents:
        steps.append(("Git", deps.install_git))
    steps.append(("AI 도구", lambda r: deps.install_clis(agents, r)))
    steps.append(("python 연결", deps.link_python))
    steps.append(("엔진 준비", lambda r: (engine.ensure_initialized(),
                                      engine.ensure_contest_initialized() if (s.get("modes") or {}).get("contests") else None,
                                      r(100, "엔진 준비됨"))))
    n = len(steps)
    for i, (label, fn) in enumerate(steps):
        fn(lambda pct, text, i=i, label=label: report((i * 100 + pct) // n, f"[{i + 1}/{n}] {text}"))
    settings.update(installed=paths.VERSION)


# ---------- cached slow reads ----------
_login_cache = {"at": 0.0, "data": {}}


def login_states(force=False) -> dict:
    # Never run the CLIs while a setup task runs: npm may be halfway through writing them.
    busy = (TASKS.get("install") or {}).get("state") == "running"
    if busy and _login_cache["data"]:
        return _login_cache["data"]
    if busy:
        return {a: {"installed": False, "logged_in": False} for a in ("claude", "codex", "gemini")}
    if force or time.time() - _login_cache["at"] > 15:
        s = settings.load()
        _login_cache["data"] = {a: logins.accounts_status(a, s) for a in ("claude", "codex", "gemini")}
        _login_cache["at"] = time.time()
    return _login_cache["data"]


# ---------- profile ----------
PROFILE_FIELDS = ("name", "birth", "gender", "phone", "email", "address", "postcode", "residence_region",
                  "occupation", "job_role", "student", "friend_tags", "school", "major", "grade")


def profile_path():
    return os.path.join(paths.data_dir(), "profile.json")


def read_profile() -> dict:
    try:
        with open(profile_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_profile(body: dict) -> dict:
    p = read_profile()
    for k in PROFILE_FIELDS:
        if k in body:
            v = body[k]
            p[k] = v.strip() if isinstance(v, str) else v
    sns = body.get("sns") or {}
    if isinstance(sns, dict):
        p.setdefault("sns_handles", {})
        for site, handle in sns.items():
            if handle:
                p["sns_handles"][site] = {"handle": str(handle).strip()}
            else:
                p["sns_handles"].pop(site, None)
    b = (p.get("birth") or "").replace(".", "-").replace("/", "-")
    try:
        bd = dt.date.fromisoformat(b)
        today = dt.date.today()
        age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        p.update(birth=bd.isoformat(), age=age, adult=age >= 19, youth_19_34=19 <= age <= 34)
    except ValueError:
        pass
    os.makedirs(paths.data_dir(), exist_ok=True)
    with open(profile_path(), "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    return {"ok": True}


# ---------- browser login window ----------
LOGIN_SITES = {"google": "https://accounts.google.com/", "naver": "https://nid.naver.com/nidlogin.login",
               "kakao": "https://accounts.kakao.com/", "instagram": "https://www.instagram.com/accounts/login/",
               "x": "https://x.com/login", "youtube": "https://www.youtube.com/"}


def open_site_login(site: str, which: str = "events") -> dict:
    url = LOGIN_SITES.get(site)
    if not url:
        return {"ok": False, "error": "unknown site"}
    return open_url_in_app_browser(url, which)


def open_url_in_app_browser(url: str, which: str = "events") -> dict:
    """Show the engine's browser (the one the AI uses) with this page, so the user can log in there.
    which: "events" (the event engine's Chrome) or "contests" (the contest engine has its own Chrome and profile)."""
    contest = which == "contests"
    if contest:
        engine.ensure_contest_initialized()
    r = subprocess.run(engine.python_cmd() + ["-X", "utf8", engine.cp_py() if contest else engine.ep_py(), "chrome", "window", url],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                       env=engine.contest_env() if contest else engine.engine_env(), creationflags=deps.NO_WINDOW,
                       cwd=engine.contest_dir() if contest else paths.data_dir())
    out = (r.stdout or "").strip().splitlines()
    try:
        return json.loads(out[-1]) if out else {"ok": r.returncode == 0}
    except ValueError:
        return {"ok": r.returncode == 0, "detail": out[-1][:200]}


# ---------- request handling ----------
def state() -> dict:
    s = settings.load()
    try:
        st = engine.status() if os.path.isfile(os.path.join(paths.data_dir(), "platform.db")) else {"running": False}
    except Exception as e:
        st = {"running": engine.running(), "error": f"{type(e).__name__}: {e}"}
    try:
        cst = engine.contest_status() if (s.get("modes") or {}).get("contests") else {"running": engine.contest_running()}
    except Exception as e:
        cst = {"running": engine.contest_running(), "error": f"{type(e).__name__}: {e}"}
    return {"version": paths.VERSION, "title": paths.APP_TITLE, "settings": settings.public(s), "deps": deps.summary(),
            "logins": login_states(), "engine": st, "contest_engine": cst, "tasks": TASKS, "frozen": paths.frozen(),
            "portable": paths.portable(), "app_dir": paths.root(), "profile_filled": bool(read_profile().get("name")),
            "integrations_supported": False}


# ---------- contest filters (engine policy.json keys, see ep/policy.py) ----------
CONTEST_KEYS = ("contest_fields", "contest_onsite_writing", "contest_onsite_final", "contest_prize_ask_krw",
                "contest_mail_only", "contest_ceremony")


def _policy_mod():
    engine.configure()  # ep.config reads the EP_* environment when first imported
    from ep import policy
    return policy


def contest_get() -> dict:
    pol = _policy_mod()
    cur = pol.load_policy(os.path.join(paths.data_dir(), "policy.json"))
    return {"values": {k: cur[k] for k in CONTEST_KEYS},
            "fields": [{"id": k, "label": v[0]} for k, v in pol.CONTEST_FIELDS.items()],
            "errors": cur.get("errors", [])}


def contest_set(body: dict) -> dict:
    """Validate the user's contest choices and merge them into data\\policy.json (other keys kept as they are)."""
    pol = _policy_mod()
    new = {}
    for k in CONTEST_KEYS:
        if k not in body:
            continue
        v = body[k]
        if k == "contest_fields":
            if not isinstance(v, list) or not all(x in pol.CONTEST_FIELDS for x in v):
                return {"ok": False, "error": "분야 값이 올바르지 않습니다."}
        elif k == "contest_prize_ask_krw":
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                return {"ok": False, "error": "상금 금액은 0 이상의 숫자로 넣으세요."}
        elif v not in pol.CHOICES[k]:
            return {"ok": False, "error": f"{k} 값이 올바르지 않습니다."}
        new[k] = v
    # the same choices drive the event engine (crawler) and the contest engine (scout, screening): two policy files
    for path in (os.path.join(paths.data_dir(), "policy.json"), os.path.join(paths.data_dir(), "contest", "policy.json")):
        data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                return {"ok": False, "error": f"policy.json을 읽지 못했습니다: {e}"}
        data.update(new)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(path + ".tmp", path)
    settings.update(contest_chosen=True)
    log(f"contest filters saved: {sorted(new)}")
    return {"ok": True} | contest_get()


def _check_browser_path(v) -> str | None:
    """Validated chrome.exe / msedge.exe path (or '' to clear); None when rejected."""
    if not isinstance(v, str):
        return None
    if v == "":
        return ""
    p = v.strip()
    if not p or not deps.valid_browser(p):
        return None
    return os.path.realpath(p)


def save_bot(tok: str, r: dict) -> dict:
    """Keep a checked bot token and open the bot in Telegram with a fresh start code (the user only presses Start)."""
    code = telegram.new_start_code()
    issued_at = telegram._START_CODES.get(code) or time.time()
    uname = r.get("username") or ""
    notify.clear_map()
    settings.update(
        telegram_token=tok,
        telegram_bot=uname,
        telegram_bot_id=str(r.get("id") or ""),
        telegram_chat_id="",
        telegram_start=code,
        telegram_start_at=issued_at,
    )
    bot_url = f"https://t.me/{uname}?start={code}" if uname else ""
    if bot_url and not os.environ.get("EP_TEST_NO_SHELL"):
        os.startfile(bot_url)
    return {"ok": True, "username": uname, "name": r.get("name") or "", "bot_url": bot_url}


def ensure_telegram_link(renew_if_expired: bool = True) -> dict:
    """Return the active bot start link, issuing a fresh start code if missing or expired while waiting."""
    s = settings.load()
    bot = (s.get("telegram_bot") or "").strip()
    if not bot or not settings.get_secret("telegram_token") or s.get("telegram_chat_id"):
        return {"ok": False, "username": bot, "url": "", "renewed": False}
    code = (s.get("telegram_start") or "").strip()
    at = s.get("telegram_start_at")
    renewed = False
    expired = (
        not code
        or telegram._is_code_used(code)
        or not isinstance(at, (int, float))
        or at <= 0
        or time.time() - at > telegram.START_TTL
    )
    if expired:
        if not renew_if_expired:
            return {"ok": False, "username": bot, "url": "", "renewed": False, "expired": True}
        code = telegram.new_start_code()
        at = telegram._START_CODES.get(code) or time.time()
        settings.update(telegram_start=code, telegram_start_at=at)
        renewed = True
    return {"ok": True, "username": bot, "url": f"https://t.me/{bot}?start={code}", "renewed": renewed}


def _q(q: dict, key: str) -> str:
    return (q.get(key) or [""])[0]


def handle_contest(method: str, route: str, q: dict, body: dict):
    """The contest tab: lists, one item, the user's answers, files of a finished work. Binary answers (a picture, a
    file) are returned as (bytes, content type, headers); the window loads them with the token in the query (?t=)."""
    if route == "overview":
        return engine.contest_overview()
    if route in ("item", "detail"):
        d = engine.contest_detail(_q(q, "id"))
        return d if d is not None else {"ok": False, "error": "없는 항목입니다."}
    if route == "decide" and method == "POST":
        iid, act, note = body.get("id") or "", body.get("action") or "", body.get("note") or ""
        r = engine.contest_decide(iid, act, note, draft_hash=body.get("draft_hash"))
        log(f"contest answer {iid} {act}: {r.get('ok')}")
        return r
    if route == "act" and method == "POST":
        return engine.contest_act(body.get("action") or "", body.get("id") or "", body.get("note") or "")
    if route == "preview":
        hdrs = {"Content-Security-Policy": FILE_CSP}
        try:
            n = int(_q(q, "n"))
        except ValueError:
            return (b"", "image/png", hdrs)
        png = engine.contest_preview(_q(q, "id"), _q(q, "pdf"), n)
        return (png, "image/png", hdrs) if png else (b"", "image/png", hdrs)
    if route == "file":
        fp = engine.contest_file(_q(q, "id"), _q(q, "name"))
        if not fp:
            return (b"", "application/octet-stream",
                    {"Content-Security-Policy": FILE_CSP, "Content-Disposition": "attachment"})
        ext = os.path.splitext(fp)[1].lower()
        mime = RASTER_MIMES.get(ext)
        hdrs = {"Content-Security-Policy": FILE_CSP}
        if not mime:
            mime = "application/octet-stream"
            hdrs["Content-Disposition"] = "attachment"
        return FileResponse(fp, mime, hdrs)
    if route == "open-folder" and method == "POST":
        d = engine.contest_folder(body.get("id") or "")
        if not d:
            return {"ok": False, "error": "열 폴더가 없습니다."}
        if not os.environ.get("EP_TEST_NO_SHELL"):
            os.startfile(d)
        return {"ok": True, "path": d}
    return None


def xlsx_pending_items() -> list:
    db_file = os.path.join(paths.data_dir(), "platform.db")
    xlsx_file = os.path.join(paths.data_dir(), "board", "직접_참여_목록.xlsx")
    if not os.path.isfile(db_file) and not os.path.isfile(xlsx_file):
        return []
    engine.configure()
    from ep import cli as ep_cli
    if os.path.isfile(xlsx_file):
        norm = os.path.normcase(os.path.abspath(xlsx_file))
        try:
            mtime = os.path.getmtime(xlsx_file)
        except OSError:
            mtime = None
        if mtime is not None:
            do_sync = False
            now = time.monotonic()
            with _xlsx_sync_lock:
                st = _XLSX_SYNC_STATE.get(norm)
                if st is None or (mtime != st["mtime"] and (now - st["at"]) >= XLSX_SYNC_INTERVAL):
                    _XLSX_SYNC_STATE[norm] = {"at": now, "mtime": st["mtime"] if st else None}
                    do_sync = True
            if do_sync:
                sr = ep_cli.call(["sync-xlsx"])
                if sr.get("ok") and not sr.get("locked"):
                    with _xlsx_sync_lock:
                        cur = _XLSX_SYNC_STATE.get(norm)
                        if cur is not None:
                            cur["mtime"] = mtime
    if not os.path.isfile(db_file):
        return []
    return ep_cli.pending_xlsx()


def handle_public(method: str, path: str, q: dict, body: dict, port: int | None = None):
    if path == "/api/session" and method == "POST":
        if q or not isinstance(body, dict) or set(body.keys()) != {"code"}:
            return {"ok": False, "error": "code", "_status": 403}
        tok = exchange_launch_code(body.get("code"))
        if not tok:
            return {"ok": False, "error": "code", "_status": 403}
        LAST_BEAT[0] = time.time()
        return {"ok": True, "token": tok}
    if path == "/api/open-window" and method == "POST":
        if q or not isinstance(body, dict) or len(body) != 1:
            return {"ok": False, "error": "open_code", "_status": 403}
        given = body.get("open_code") if "open_code" in body else body.get("code")
        if not isinstance(given, str) or not given or not secrets.compare_digest(given, OPEN_CODE):
            return {"ok": False, "error": "open_code", "_status": 403}
        return open_window(port)
    return None


def handle(method: str, path: str, q: dict, body: dict):
    pub = handle_public(method, path, q, body)
    if pub is not None:
        out = dict(pub)
        out.pop("_status", None)
        return out
    if path == "/api/state":
        return state()
    if path == "/api/heartbeat":
        LAST_BEAT[0] = time.time()
        return {"ok": True}
    if path == "/api/settings" and method == "POST":
        changes = {}
        if "chrome" in body:
            checked = _check_browser_path(body["chrome"])
            if checked is None:
                return {"ok": False, "error": "브라우저 경로는 존재하는 chrome.exe 또는 msedge.exe 파일이어야 합니다."}
            changes["chrome"] = checked
        if body.get("notify") in ("telegram", "email", "none"):
            changes["notify"] = body["notify"]
        if isinstance(body.get("modes"), dict):
            changes["modes"] = {
                k: bool(v) for k, v in body["modes"].items() if k in ("events", "contests")
            }
        if isinstance(body.get("agents"), dict):
            changes["agents"] = {
                k: bool(v) for k, v in body["agents"].items() if k in ("claude", "codex", "gemini")
            }
        if "interests" in body:
            iv = body["interests"]
            if isinstance(iv, dict):
                changes["interests"] = {str(k): v for k, v in iv.items() if isinstance(k, str) and len(k) <= 100}
            elif isinstance(iv, (str, list)):
                changes["interests"] = iv
        for k in ("max_agents", "always_approve", "min_prize_krw"):
            if k in body:
                changes[k] = body[k]
        if isinstance(body.get("integrations"), dict):
            # LCH-WIKI, LLM-RELAY and LLM-CONSOLE are not supported yet: the keys are kept, they cannot be switched on
            changes["integrations"] = {
                name: {"enabled": False, "url": str(v.get("url") or "")[:300]}
                for name, v in body["integrations"].items()
                if name in settings.INTEGRATIONS and isinstance(v, dict)
            }
        if isinstance(body.get("accounts"), dict):
            changes["accounts"] = {
                a: max(1, min(logins.MAX_ACCOUNTS, n))
                for a, n in body["accounts"].items()
                if a in logins.QUEUE_AGENTS and isinstance(n, int) and not isinstance(n, bool)
            }
        s = settings.merge(changes)
        return {"ok": True, "settings": settings.public(s)}
    if path == "/api/install" and method == "POST":
        run_task("install", task_install_all)
        return {"ok": True}
    if path == "/api/logins/start" and method == "POST":
        def show(d):
            if "logins" in TASKS:
                TASKS["logins"]["device"] = d
        run_task("logins", lambda r: logins.login_queue(r, show=show))
        return {"ok": True}
    if path == "/api/logins/open-url" and method == "POST":
        d = (TASKS.get("logins") or {}).get("device") or {}
        if d.get("url"):
            logins.open_url(d["url"])
            if d.get("code"):
                logins.copy_text(d["code"])
        return {"ok": bool(d.get("url"))}
    if path.startswith("/api/login/"):
        agent = path.rsplit("/", 1)[1]
        if agent not in ("claude", "codex", "gemini"):
            return {"ok": False, "error": "unknown"}
        if method == "POST":
            if agent not in logins.QUEUE_AGENTS:
                return {"ok": False, "error": "Gemini는 API 키로 연결합니다."}
            return logins.start_login(agent, int(body.get("i") or 1))
        return logins.status(agent)
    if path.startswith("/api/logout/") and method == "POST":
        r = logins.logout(path.rsplit("/", 1)[1], int(body.get("i") or 1))
        login_states(force=True)
        return r
    if path == "/api/logins/refresh":
        return login_states(force=True)
    if path == "/api/gemini-key" and method == "POST":
        key = (body.get("key") or "").strip()
        r = logins.check_gemini_key(key)
        if r.get("ok"):
            settings.set_secret("gemini_api_key", key)
            login_states(force=True)
        return r
    if path == "/api/telegram/token" and method == "POST":
        tok = (body.get("token") or "").strip()
        try:
            r = telegram.verify(tok)
        except telegram.TgError as e:
            return {"ok": False, "error": str(e)}
        _TG_CLIP.update(tok="", info=None)
        return save_bot(tok, r)
    if path == "/api/email/to" and method == "POST":
        to = (body.get("to") or "").strip()
        if to and ("@" not in to or " " in to):
            return {"ok": False, "error": "받는 이메일 주소를 확인하세요."}
        settings.update(email_to=to)
        return {"ok": True}
    if path == "/api/email/connect" and method == "POST":
        from app import mailer
        _MAIL_CLIP.update(pw="", masked="")
        r = mailer.connect(body.get("address") or "", body.get("password") or "")
        if r.get("ok"):
            settings.update(notify="email")
        return r
    if path == "/api/email/open" and method == "POST":
        from app import mailer
        url = mailer.OPEN_URLS.get(body.get("what") or "")
        if not url:
            return {"ok": False, "error": "unknown page"}
        os.startfile(url)  # the user's default browser
        return {"ok": True}
    if path == "/api/email/clip":
        # while the e-mail panel is open the window asks every 2 s: show a masked preview, connect only on confirm.
        from app import mailer
        addr = ((q.get("address") or [""])[0]).strip()
        if mailer.provider(addr) != "google":
            return {"ok": True, "found": False}
        pw = mailer.password_in_clipboard()
        if not pw or pw == settings.get_secret("email_password"):
            _MAIL_CLIP.update(pw="", masked="")
            return {"ok": True, "found": False}
        digest = hashlib.sha256(pw.encode()).hexdigest()
        if digest == _CLIP_TRIED[0]:
            _MAIL_CLIP.update(pw="", masked="")
            return {"ok": True, "found": False}
        masked = mailer.mask_app_password(pw)
        _MAIL_CLIP.update(pw=pw, masked=masked)
        return {"ok": True, "found": True, "pending": True, "masked": masked}
    if path == "/api/email/confirm" and method == "POST":
        from app import mailer
        pw, masked = _MAIL_CLIP["pw"], _MAIL_CLIP["masked"]
        if not pw:
            return {"ok": False, "error": "확인할 앱 비밀번호가 없습니다. 비밀번호를 다시 복사하세요."}
        want_masked = (body.get("masked") or "").strip()
        if want_masked and want_masked != masked:
            return {"ok": False, "error": "복사된 앱 비밀번호가 바뀌었습니다. 다시 확인하세요."}
        _CLIP_TRIED[0] = hashlib.sha256(pw.encode()).hexdigest()
        _MAIL_CLIP.update(pw="", masked="")
        r = mailer.connect((body.get("address") or "").strip(), pw)
        if r.get("ok"):
            settings.update(notify="email")
        return {"ok": bool(r.get("ok")), "error": r.get("error", "")}
    if path == "/api/email/info":
        from app import mailbot
        return {"ok": True, "agents": mailbot.agents(), "need": mailbot.LAST["need"]}
    if path == "/api/email/open-google" and method == "POST":
        from app import mailbot
        settings.update(notify="email", mailbot_consent=dt.datetime.now().isoformat(timespec="seconds"))
        mailbot.LAST["need"] = ""
        return mailbot.open_page()
    if path == "/api/email/start" and method == "POST":
        from app import mailbot, mailer
        if mailer.provider((body.get("address") or "").strip()) != "google":
            return {"ok": False, "error": "AI가 만들기는 Gmail만 됩니다. 직접 만들기를 쓰세요."}
        run_task("mailbot", lambda r: mailbot.run(r, body.get("agent") or "", body.get("address") or ""))
        return {"ok": True}
    if path == "/api/telegram/reset" and method == "POST":
        _TG_CLIP.update(tok="", info=None)
        TASKS.pop("tgbot", None)
        notify.clear_map()
        settings.set_secret("telegram_token", "")
        settings.update(telegram_bot="", telegram_bot_id="", telegram_chat_id="",
                        telegram_start="", telegram_start_at=0)
        return {"ok": True}
    if path == "/api/tgbot/info":
        from app import tgbot
        lnk = ensure_telegram_link(renew_if_expired=True)
        return {"ok": True, "agents": tgbot.agents(), "retry": tgbot.LAST["retry"],
                "bot_url": lnk.get("url", ""), "renewed": bool(lnk.get("renewed"))}
    if path == "/api/telegram/link":
        return ensure_telegram_link(renew_if_expired=True)
    if path == "/api/tgbot/cancel" and method == "POST":
        # the user gave up after a refused name or username: remove the Telegram Web session data from this browser
        from app import tgbot
        tgbot.LAST["retry"] = ""
        cleared = tgbot.clear_web_data()
        msg = ("텔레그램 웹 데이터를 지웠습니다. 휴대폰 텔레그램 설정 > 기기에서 Telegram Web 세션도 끊으세요."
               if cleared else
               "경고: 브라우저의 텔레그램 웹 기록을 지우지 못했습니다. 열린 브라우저에서 직접 로그아웃하고 "
               "휴대폰 텔레그램의 설정 > 기기에서 Telegram Web 세션을 직접 종료하세요.")
        return {"ok": cleared, "cleared": cleared, "message": msg, "error": "" if cleared else msg}
    if path == "/api/tgbot/open-web" and method == "POST":
        from app import tgbot
        settings.update(notify="telegram", tgbot_consent=dt.datetime.now().isoformat(timespec="seconds"))
        return tgbot.open_web()
    if path == "/api/tgbot/start" and method == "POST":
        from app import tgbot
        bad = tgbot.check_inputs((body.get("name") or "").strip(), (body.get("username") or "").strip().lstrip("@"))
        if bad:
            return {"ok": False, "error": bad}
        def _tgbot_task(r):
            out = tgbot.run(r, body.get("agent") or "", body.get("name") or "", body.get("username") or "")
            st = settings.load()
            if st.get("telegram_chat_id"):
                settings.update(telegram_start="", telegram_start_at=0)
            return out
        run_task("tgbot", _tgbot_task)
        return {"ok": True}
    if path == "/api/telegram/open" and method == "POST":
        # BotFather to make a bot, or the user's own bot with a start code so the app knows which chat is theirs
        s = settings.load()
        if body.get("what") == "bot" and s.get("telegram_bot"):
            lnk = ensure_telegram_link(renew_if_expired=True)
            if lnk.get("url") and not os.environ.get("EP_TEST_NO_SHELL"):
                os.startfile(lnk["url"])
            return {"ok": True, "bot_url": lnk.get("url", ""), "renewed": bool(lnk.get("renewed"))}
        if not os.environ.get("EP_TEST_NO_SHELL"):
            os.startfile("https://t.me/BotFather")
        return {"ok": True}
    if path == "/api/telegram/clip":
        # while the Telegram step is open the window asks every 2 s: verify a copied bot token, save only on confirm
        tok = telegram.token_in_clipboard()
        if not tok or tok == settings.get_secret("telegram_token"):
            _TG_CLIP.update(tok="", info=None)
            return {"ok": True, "found": False}
        if tok == _TG_CLIP["tok"]:
            info = _TG_CLIP["info"]
            if not info:
                return {"ok": True, "found": False}
            return {"ok": True, "found": True, "pending": True,
                    "username": info.get("username") or "", "name": info.get("name") or ""}
        try:
            r = telegram.verify(tok)
        except telegram.TgError as e:
            _TG_CLIP.update(tok=tok, info=None)
            return {"ok": False, "found": True, "error": str(e)}
        _TG_CLIP.update(tok=tok, info=r)
        return {"ok": True, "found": True, "pending": True,
                "username": r.get("username") or "", "name": r.get("name") or ""}
    if path == "/api/telegram/confirm" and method == "POST":
        tok, info = _TG_CLIP["tok"], _TG_CLIP["info"]
        if not tok or not info:
            return {"ok": False, "error": "확인할 봇 토큰이 없습니다. 토큰을 다시 복사하세요."}
        want_user = (body.get("username") or "").strip()
        if want_user and want_user != (info.get("username") or ""):
            return {"ok": False, "error": "복사된 봇 정보가 바뀌었습니다. 다시 확인하세요."}
        _TG_CLIP.update(tok="", info=None)
        return save_bot(tok, info)
    if path == "/api/telegram/find" and method == "POST":
        s = settings.load()
        renew = bool(body.get("renew"))
        code = (s.get("telegram_start") or "").strip()
        if not code:
            if renew and s.get("telegram_bot") and settings.get_secret("telegram_token") and not s.get("telegram_chat_id"):
                lnk = ensure_telegram_link(renew_if_expired=True)
                return {"ok": False, "waiting": True, "renewed": True, "bot_url": lnk.get("url", ""),
                        "username": lnk.get("username", ""),
                        "error": "새 연결 코드를 발급했습니다. 봇 링크를 눌러 시작(START)을 누르세요."}
            return {"ok": False, "error": "연결 코드가 없습니다. 봇 연결을 다시 시작하세요."}
        at = s.get("telegram_start_at")
        if not isinstance(at, (int, float)) or at <= 0 or time.time() - at > telegram.START_TTL:
            if renew and s.get("telegram_bot") and settings.get_secret("telegram_token") and not s.get("telegram_chat_id"):
                lnk = ensure_telegram_link(renew_if_expired=True)
                return {"ok": False, "waiting": True, "expired": True, "renewed": True,
                        "bot_url": lnk.get("url", ""), "username": lnk.get("username", ""),
                        "error": "연결 코드가 만료되어 새로 발급했습니다. 봇 링크를 다시 눌러 시작(START)을 누르세요."}
            settings.update(telegram_start="", telegram_start_at=0)
            return {"ok": False, "expired": True, "error": "연결 코드가 만료되었습니다. 봇 다시 열기를 눌러 새로 시작하세요."}
        try:
            r = telegram.find_chat(settings.get_secret("telegram_token"), code, started_at=at)
            if r.get("ok"):
                if str(s.get("telegram_chat_id") or "") != str(r["chat_id"]):
                    notify.clear_map()
                settings.update(telegram_chat_id=r["chat_id"], notify="telegram", telegram_start="", telegram_start_at=0)
                if (TASKS.get("tgbot") or {}).get("state") == "waiting":
                    TASKS["tgbot"].update(state="done", pct=100, text="봇 연결됨")
                telegram.send(f"{paths.APP_TITLE} 연결됨. 직접 해야 하는 일과 AI가 묻는 것을 여기로 보냅니다.")
                return r
            if renew and s.get("telegram_bot"):
                lnk = ensure_telegram_link(renew_if_expired=True)
                return {**r, "waiting": True, "renewed": bool(lnk.get("renewed")),
                        "bot_url": lnk.get("url", ""), "username": lnk.get("username", "")}
            return r
        except telegram.TgError as e:
            return {"ok": False, "error": str(e)}
    if path == "/api/telegram/test" and method == "POST":
        try:
            telegram.send("테스트 메시지입니다.", [[{"text": "확인", "callback_data": "noop:0"}]])
            return {"ok": True}
        except telegram.TgError as e:
            return {"ok": False, "error": str(e)}
    if path == "/api/profile":
        if method == "POST":
            return write_profile(body)
        p = read_profile()
        return {k: p.get(k, "") for k in PROFILE_FIELDS} | {
            "sns": {k: (v or {}).get("handle", "") for k, v in (p.get("sns_handles") or {}).items()}}
    if path == "/api/site-login" and method == "POST":
        return open_site_login(body.get("site") or "", "contests" if body.get("engine") == "contests" else "events")
    if path == "/api/secrets/unbound" and method == "GET":
        from app import secret_binding
        return secret_binding.unbound_secrets()
    if path == "/api/secrets/bind" and method == "POST":
        from app import secret_binding
        return secret_binding.bind_secret(body.get("key") or "", body.get("domain") or "", body.get("engine") or "")
    if path == "/api/contest":
        return contest_set(body) if method == "POST" else contest_get()
    if path == "/api/finish-setup" and method == "POST":
        settings.update(setup_done=True)
        return {"ok": True}
    if path == "/api/engine/start" and method == "POST":
        r = engine.start_modes()
        log(f"engine start: {r}")
        return r
    if path == "/api/engine/stop" and method == "POST":
        r = engine.stop_modes()
        log("engine stop requested")
        return r
    if path.startswith("/api/contest/"):
        return handle_contest(method, path[len("/api/contest/"):], q, body)
    if path == "/api/items":
        st = (q.get("state") or ["owner"])[0]
        return {"items": engine.items(st, int((q.get("limit") or ["50"])[0])),
                "xlsx_pending": xlsx_pending_items()}
    if path == "/api/xlsx/pending":
        return {"ok": True, "items": xlsx_pending_items()}
    if path == "/api/xlsx/confirm" and method == "POST":
        if body.get("all"):
            return {"ok": False, "error": "화면에 표시된 항목 목록(id·version·hash)만 확인할 수 있습니다.", "refresh": True}
        engine.configure()
        from ep import cli as ep_cli
        if "items" in body:
            if not isinstance(body["items"], list):
                return {"ok": False, "error": "확인할 항목 목록 형식이 올바르지 않습니다."}
            return ep_cli.confirm_xlsx(items=body["items"])
        return ep_cli.confirm_xlsx(
            item_id=str(body.get("id") or ""),
            version=body.get("version"),
            content_hash=str(body.get("hash") or body.get("content_hash") or ""),
            pending_id=body.get("pending_id"),
        )
    if path == "/api/item/act" and method == "POST":
        return engine.act(body.get("action") or "", body.get("id") or "", body.get("note") or "")
    if path == "/api/log":
        return {"engine": engine.tail_log(80), "contest": engine.contest_tail_log(80), "app": LOG_LINES[-80:]}
    if path == "/api/open-folder" and method == "POST":
        os.startfile(paths.root())
        return {"ok": True}
    if path == "/api/uninstall" and method == "POST":
        from app import installer
        if paths.portable():
            return {"ok": False, "error": "포터블판은 폴더를 직접 지우면 됩니다."}
        return installer.uninstall(keep_data=bool(body.get("keep_data")))
    return None


class _DeadlineSocketIO(io.RawIOBase):
    """Socket stream wrapper enforcing both idle socket timeout and total per-request receive deadline."""

    def __init__(self, sock, handler):
        self._raw = sock.makefile("rb", buffering=0)
        self._sock = sock
        self._handler = handler

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int | None:
        h = self._handler
        idle = h.timeout
        dl = h._req_deadline
        if dl is not None:
            rem = dl - time.monotonic()
            if rem <= 0:
                raise TimeoutError("request deadline exceeded")
            eff = rem if idle is None else min(idle, rem)
            self._sock.settimeout(max(0.001, eff))
        elif idle is not None:
            self._sock.settimeout(idle)
        n = self._raw.readinto(b)
        if n and h._req_deadline is None and h._arm_on_first_byte:
            h._arm_on_first_byte = False
            if h.request_deadline is not None:
                h._req_deadline = time.monotonic() + h.request_deadline
        if h._req_deadline is not None and time.monotonic() > h._req_deadline:
            raise TimeoutError("request deadline exceeded")
        return n

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class Handler(BaseHTTPRequestHandler):
    server_version = "EntryPilot"
    timeout = SOCKET_TIMEOUT
    request_deadline = REQUEST_DEADLINE

    def setup(self):
        self._req_deadline = None
        self._arm_on_first_byte = False
        self._req_count = 0
        super().setup()
        old_rfile = self.rfile
        bufsize = self.rbufsize if self.rbufsize > 0 else io.DEFAULT_BUFFER_SIZE
        self.rfile = io.BufferedReader(_DeadlineSocketIO(self.connection, self), buffer_size=bufsize)
        with contextlib.suppress(Exception):
            old_rfile.close()

    def _check_deadline(self):
        if self._req_deadline is not None and time.monotonic() > self._req_deadline:
            raise TimeoutError("request deadline exceeded")

    def _clear_deadline(self):
        self._req_deadline = None
        self._arm_on_first_byte = False
        if self.timeout is not None:
            with contextlib.suppress(OSError):
                self.connection.settimeout(self.timeout)

    def handle_one_request(self):
        if self.request_deadline is not None:
            if self._req_count == 0:
                self._req_deadline = time.monotonic() + self.request_deadline
                self._arm_on_first_byte = False
            else:
                self._req_deadline = None
                self._arm_on_first_byte = True
        else:
            self._req_deadline = None
            self._arm_on_first_byte = False
        self._req_count += 1
        super().handle_one_request()

    def parse_request(self):
        ok = super().parse_request()
        if not ok:
            self._clear_deadline()
            return False
        try:
            self._check_deadline()
        except TimeoutError:
            self.close_connection = True
            self._clear_deadline()
            with contextlib.suppress(OSError):
                self._send(408, b'{"ok":false,"error":"request timeout"}')
            return False
        if self.command != "POST":
            self._clear_deadline()
        return True

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", headers: dict | None = None):
        hdrs = {"Content-Security-Policy": APP_CSP, **(headers or {})}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, ctype: str, headers: dict | None = None):
        try:
            f = open(path, "rb")
        except OSError:
            return self._send(404, b"not found" if ctype.startswith("text/") else b"", ctype, headers)
        try:
            size = os.fstat(f.fileno()).st_size
            hdrs = {"Content-Security-Policy": APP_CSP, **(headers or {})}
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            remaining = size
            while remaining > 0:
                chunk = f.read(min(STREAM_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        finally:
            f.close()

    def _host_ok(self):
        h = (self.headers.get("Host") or "").split(":")[0]
        return h in ("127.0.0.1", "localhost")

    def _read_json_body(self, max_bytes: int):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._clear_deadline()
            self._send(411, b'{"ok":false,"error":"content-length required"}')
            return None
        raw_cl = self.headers.get("Content-Length")
        if raw_cl is None or raw_cl == "":
            self._clear_deadline()
            return {}
        try:
            n = int(raw_cl)
        except ValueError:
            self.close_connection = True
            self._clear_deadline()
            self._send(400, b'{"ok":false,"error":"bad content-length"}')
            return None
        if n < 0:
            self.close_connection = True
            self._clear_deadline()
            self._send(400, b'{"ok":false,"error":"bad content-length"}')
            return None
        if n > max_bytes:
            self.close_connection = True
            self._clear_deadline()
            self._send(413, b'{"ok":false,"error":"payload too large"}')
            return None
        if n == 0:
            self._clear_deadline()
            return {}
        chunks, left = [], n
        try:
            self._check_deadline()
            while left > 0:
                self._check_deadline()
                buf = self.rfile.read(min(STREAM_CHUNK, left))
                if not buf:
                    break
                chunks.append(buf)
                left -= len(buf)
            self._check_deadline()
        except TimeoutError:
            self.close_connection = True
            self._clear_deadline()
            with contextlib.suppress(OSError):
                self._send(408, b'{"ok":false,"error":"request timeout"}')
            return None
        self._clear_deadline()
        if left > 0:
            self.close_connection = True
            self._send(400, b'{"ok":false,"error":"incomplete body"}')
            return None
        try:
            parsed = json.loads(b"".join(chunks).decode("utf-8"))
        except ValueError:
            self._send(400, b'{"ok":false,"error":"bad json"}')
            return None
        if not isinstance(parsed, dict):
            self._send(400, b'{"ok":false,"error":"bad json"}')
            return None
        return parsed

    def _dispatch(self, method):
        if not self._host_ok():
            self.close_connection = True
            self._clear_deadline()
            return self._send(403, b"{}")
        u = urlparse(self.path)
        if u.path in STATIC and method == "GET":
            name, ctype = STATIC[u.path]
            return self._send_file(os.path.join(paths.bundle_dir(), "ui", name), ctype)
        if not u.path.startswith("/api/"):
            self._clear_deadline()
            return self._send(404, b"not found", "text/plain")
        q = parse_qs(u.query)
        if u.path in ("/api/session", "/api/open-window"):
            if method != "POST":
                self.close_connection = True
                self._clear_deadline()
                return self._send(403, b'{"ok":false,"error":"forbidden"}')
            body = self._read_json_body(MAX_JSON_BYTES)
            if body is None:
                return
            srv_port = getattr(self.server, "server_address", (None, 0))[1]
            r = handle_public(method, u.path, q, body, port=srv_port)
            status = r.pop("_status", 200)
            if status != 200:
                self.close_connection = True
            return self._send(status, json.dumps(r, ensure_ascii=False).encode("utf-8"))
        tok = self.headers.get("X-EP-Token")
        if method == "GET" and u.path in ("/api/contest/preview", "/api/contest/file"):
            tok = tok or (q.get("t") or [""])[0]  # <img src> cannot send a header
        if not isinstance(tok, str) or not secrets.compare_digest(tok, TOKEN):
            self.close_connection = True
            self._clear_deadline()
            return self._send(403, b'{"ok":false,"error":"token"}')
        body = {}
        if method == "POST":
            cap = UPLOAD_ROUTES.get(u.path, MAX_JSON_BYTES)
            body = self._read_json_body(cap)
            if body is None:
                return
        try:
            r = handle(method, u.path, q, body)
        except Exception as e:
            log(f"api {u.path}: {traceback.format_exc()}")
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if r is None:
            return self._send(404, b'{"ok":false,"error":"no route"}')
        if isinstance(r, FileResponse):
            return self._send_file(r.path, r.ctype, r.headers)
        if isinstance(r, tuple):
            data, ctype, *rest = r
            return self._send(200 if data else 404, data, ctype, rest[0] if rest else None)
        self._send(200, json.dumps(r, ensure_ascii=False, default=str).encode("utf-8"))

    def do_GET(self):
        try:
            self._dispatch("GET")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def do_POST(self):
        try:
            self._dispatch("POST")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    max_threads = MAX_THREADS

    def __init__(self, server_address, RequestHandlerClass):
        super().__init__(server_address, RequestHandlerClass)
        self._thread_sem = threading.BoundedSemaphore(self.max_threads)

    def process_request(self, request, client_address):
        if not self._thread_sem.acquire(timeout=SOCKET_TIMEOUT):
            self.shutdown_request(request)
            return
        try:
            t = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
            t.start()
        except Exception:
            self._thread_sem.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._thread_sem.release()


def serve(port: int = 0) -> ThreadingHTTPServer:
    srv = BoundedHTTPServer(("127.0.0.1", port), Handler)
    PORT[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    return srv


def start_background():
    w = notify.Watcher(log)
    w.start()
    p = telegram.Poller(notify.on_button, log, notify.on_reply)
    p.start()
    return w, p
