"""The engines as seen by the app: `ep` (online events) and `cp` (contests, the second half of this file).

The scheduler runs as its own process: `<python> -X utf8 <bundle>\\ep.py run`, where <python> is the app exe (frozen)
or the current interpreter (source). Every value that used to point at one person's PC comes from engine_env().
Short commands (status, list, owner actions) run in this process through ep.cli.call after configure() has set the
same environment variables.
"""
import json
import os
import subprocess
import sys

from app import deps, logins, paths, settings

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def engine_env(base: dict | None = None) -> dict:
    s = settings.load()
    enabled = [a for a, on in (s.get("agents") or {}).items() if on]
    # Keep GEMINI_API_KEY out of the shared engine env; GeminiAdapter injects it only for Gemini runs.
    # PATH includes per-agent <cli_prefix>\<agent> bin dirs first, plus legacy <cli_prefix> fallback.
    env = logins.agent_env("", base=base)
    for k in list(env):
        if k.upper().startswith(("GEMINI_", "GOOGLE_API", "LCH_")):
            env.pop(k, None)
    env.update({
        "EP_APP_DIR": paths.root(),
        "EP_DATA_DIR": paths.data_dir(),
        "EP_OWNER_DIR": os.path.join(paths.data_dir(), "board"),
        "EP_AGENTS": ",".join(enabled),
        "EP_MAX_AGENTS": str(int(s.get("max_agents") or 3)),
        "EP_CLAUDE_CONFIG_DIR": logins.claude_dir(),
        "EP_CODEX_HOME": logins.codex_dir(),
        # every account folder the user set up; the engine moves to the next one when an account hits its limit
        "EP_CLAUDE_CONFIG_DIRS": os.pathsep.join(logins.account_dir("claude", i)
                                                 for i in range(1, max(1, logins.account_count("claude", s)) + 1)),
        "EP_CODEX_HOMES": os.pathsep.join(logins.account_dir("codex", i)
                                          for i in range(1, max(1, logins.account_count("codex", s)) + 1)),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    ch = s.get("chrome") or ""
    br = ch if (ch and deps.valid_browser(ch)) else deps.find_browser()
    if br and deps.valid_browser(br):
        env["EP_CHROME"] = br
    c = deps.resolve_cli_cmd(deps.cli_command("claude"), "claude")
    if len(c) == 1 and c[0].lower().endswith(".exe"):
        env["EP_CLAUDE_EXE"] = c[0]
    c = deps.resolve_cli_cmd(deps.cli_command("codex"), "codex")
    if len(c) == 1 and c[0].lower().endswith(".exe"):
        env["EP_CODEX_EXE"] = c[0]
    c = deps.resolve_cli_cmd(deps.cli_command("gemini"), "gemini")
    if c:
        env["EP_GEMINI_CMD"] = json.dumps(c)
    f = integrations_file()
    if f:
        env["EP_INTEGRATIONS_FILE"] = f
    return env


def integrations_file() -> str:
    """Hook for the external tools (LCH-WIKI, LLM-RELAY, LLM-CONSOLE). They are not supported yet, so this is "" and the
    engines get no variable. When a later version supports one, it writes its settings to a JSON file and returns the
    path here; the engines then see it as EP_INTEGRATIONS_FILE / CP_INTEGRATIONS_FILE."""
    return ""


def configure():
    """Put the engine environment into this process before `ep` is imported (ep.config reads it at import)."""
    for k, v in engine_env().items():
        if k.startswith("EP_"):
            os.environ[k] = v


def python_cmd() -> list:
    if paths.frozen():
        shim = deps.python_shim()
        return [shim if os.path.isfile(shim) else sys.executable]
    return [sys.executable]


def ep_py() -> str:
    return os.path.join(paths.bundle_dir(), "ep.py")


def _call(argv: list) -> dict:
    configure()
    import ep.cli  # noqa: E402  (after configure)
    try:
        return ep.cli.call(argv)
    except SystemExit as e:
        return {"ok": False, "error": f"exit {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def ensure_initialized():
    """First run: create the database, profile.json and policy.json defaults, and migrate plaintext secrets."""
    configure()
    os.makedirs(paths.data_dir(), exist_ok=True)
    from ep import config, secrets, store  # noqa: E402
    db = store.connect()
    db.close()
    secrets.ensure_secrets_file()
    secrets.migrate()
    prof = config.profile_path()
    if os.path.isfile(prof):
        secrets.restrict_acl(prof)


def status() -> dict:
    st = _call(["status"])
    st["running"] = running()
    return st


def items(state: str, limit: int = 50) -> list:
    r = _call(["list", "--state", state, "--limit", str(limit)])
    for key in ("items", "rows", "list"):
        if isinstance(r.get(key), list):
            return r[key]
    return r if isinstance(r, list) else []


def pid_file() -> str:
    return os.path.join(paths.data_dir(), "engine.pid")


def running(pidfile: str | None = None) -> bool:
    try:
        pid = int(open(pidfile or pid_file()).read().strip())
    except (OSError, ValueError):
        return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def start() -> dict:
    if running():
        return {"ok": True, "already": True}
    s = settings.load()
    picked = [a for a, on in (s.get("agents") or {}).items() if on]
    dirs = {a: logins.logged_in_dirs(a) for a in picked if a in logins.QUEUE_AGENTS}
    ready = [a for a in picked if (dirs[a] if a in dirs else logins.status(a).get("logged_in"))]
    if not ready:
        return {"ok": False, "error": "로그인된 AI가 없습니다. 설정에서 AI에 로그인하세요."}
    ensure_initialized()
    from ep import store  # noqa: E402
    db = store.connect()
    try:
        with store.writer(db):
            store.kv_set(db, "stop", False)
        from ep import accounts  # noqa: E402
        for a in ready:  # a block from before the login ("no account") must not outlive the login
            accounts.unblock(db, a)
    finally:
        db.close()
    os.makedirs(paths.logs_dir(), exist_ok=True)
    log = open(os.path.join(paths.logs_dir(), "scheduler.log"), "a", encoding="utf-8")
    env = engine_env()
    env["EP_AGENTS"] = ",".join(ready)  # only AIs that are logged in right now
    if dirs.get("claude"):  # only accounts that are logged in right now
        env["EP_CLAUDE_CONFIG_DIRS"] = os.pathsep.join(dirs["claude"])
        env["EP_CLAUDE_CONFIG_DIR"] = dirs["claude"][0]
    if dirs.get("codex"):
        env["EP_CODEX_HOMES"] = os.pathsep.join(dirs["codex"])
        env["EP_CODEX_HOME"] = dirs["codex"][0]
    p = subprocess.Popen(python_cmd() + ["-X", "utf8", ep_py(), "run", "--force"], cwd=paths.data_dir(),
                         env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=NO_WINDOW | DETACHED, close_fds=True)
    open(pid_file(), "w").write(str(p.pid))
    return {"ok": True, "pid": p.pid, "agents": ready}


def stop() -> dict:
    """Same as `ep stop`: set the stop flag; the scheduler finishes its active runs and exits."""
    configure()
    from ep import store  # noqa: E402
    db = store.connect()
    try:
        with store.writer(db):
            store.kv_set(db, "stop", True)
    finally:
        db.close()
    return {"ok": True}


def act(action: str, item_id: str, note: str = "") -> dict:
    """User actions on one item. done = the user did it (manual) / answered (owner); retry; skip."""
    configure()
    import ep.cli  # noqa: E402
    return ep.cli.owner_act(action, item_id, note)


def tail_log(n: int = 60) -> list:
    p = os.path.join(paths.logs_dir(), "scheduler.log")
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 20000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


# ======================= contest engine (`cp`) =======================
def contest_dir() -> str:
    return os.path.join(paths.data_dir(), "contest")


def contest_env(base: dict | None = None) -> dict:
    """The environment of the contest engine: the same values as engine_env(), named CP_*, with its own data folder
    (platform.db, policy.json, Chrome profile on its own port) under data\\contest. The profile is shared."""
    env = {}
    for k, v in engine_env(base).items():
        if k.upper().startswith(("GEMINI_", "GOOGLE_API", "LCH_")):
            continue
        if k.startswith("EP_"):
            env["CP_" + k[3:]] = v
        else:
            env[k] = v
    env["CP_APP_DIR"] = paths.root()
    env["CP_DATA_DIR"] = contest_dir()
    env["CP_OWNER_DIR"] = os.path.join(contest_dir(), "board")
    env["CP_PROFILE_PATH"] = os.path.join(paths.data_dir(), "profile.json")
    return env


def configure_contest():
    """Put the contest environment into this process before `cp` is imported (cp.config reads it at import)."""
    for k, v in contest_env().items():
        if k.startswith("CP_"):
            os.environ[k] = v


def cp_py() -> str:
    return os.path.join(paths.bundle_dir(), "cp.py")


def contest_pid_file() -> str:
    return os.path.join(contest_dir(), "engine.pid")


def contest_running() -> bool:
    return running(contest_pid_file())


def _contest_call(argv: list) -> dict:
    configure_contest()
    import cp.cli  # noqa: E402  (after configure_contest)
    try:
        return cp.cli.call(argv)
    except SystemExit as e:
        return {"ok": False, "error": f"exit {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def ensure_contest_initialized():
    """Create the contest database and its folders (the engine's own migrate and defaults)."""
    configure_contest()
    os.makedirs(os.path.join(contest_dir(), "logs"), exist_ok=True)
    from cp import config, secrets, store  # noqa: E402
    db = store.connect()
    db.close()
    secrets.ensure_secrets_file()
    secrets.migrate()
    prof = config.profile_path()
    if os.path.isfile(prof):
        secrets.restrict_acl(prof)


def contest_status() -> dict:
    if not os.path.isfile(os.path.join(contest_dir(), "platform.db")):
        return {"running": contest_running(), "counts": {}}
    st = _contest_call(["status"])
    st["running"] = contest_running()
    return st


def _with_db(fn):
    configure_contest()
    from cp import store  # noqa: E402
    db = store.connect()
    try:
        return fn(db)
    finally:
        db.close()


def contest_overview() -> dict:
    ensure_contest_initialized()
    from cp import review  # noqa: E402
    return _with_db(review.overview)


def contest_detail(iid: str) -> dict | None:
    from cp import review  # noqa: E402
    return _with_db(lambda db: review.detail(db, iid))


def contest_decide(iid: str, action: str, note: str = "", draft_hash: str | None = None) -> dict:
    """The user's answer on one contest item: yes (통과), revise (수정, with a memo), skip (안 함).
    draft_hash is the hash of the draft the user saw; it must match the current draft (same as /api/contest/decide)."""
    configure_contest()
    from cp import cmdbase, review, store  # noqa: E402
    try:
        msg = review.decide(iid, action, note, draft_hash=draft_hash, require_hash=True)
    except review.StaleDraftError as e:
        return {"ok": False, "error": str(e), "stale": True, "refresh": True}
    except review.InvalidSendSpecError as e:
        return {"ok": False, "error": str(e), "revised": True, "refresh": True}
    except (cmdbase.CmdError, store.StoreError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "message": msg, "next": _with_db(lambda db: review.next_waiting(db, iid))}


def contest_file(iid: str, name: str) -> str:
    from cp import review  # noqa: E402
    return _with_db(lambda db: review.file_path(db, iid, name))


def contest_preview(iid: str, pdf: str, n: int):
    from cp import review  # noqa: E402
    return _with_db(lambda db: review.preview_png(db, iid, pdf, n))


def contest_folder(iid: str) -> str:
    from cp import review  # noqa: E402
    return _with_db(lambda db: review.folder(db, iid))


def contest_act(action: str, item_id: str, note: str = "") -> dict:
    """User actions on a contest item that is not waiting for an answer: done (the user did it), retry, skip."""
    configure_contest()
    from cp import transitions  # noqa: E402
    return transitions.owner_act(action, item_id, note)


def start_contest() -> dict:
    """Start the contest scheduler (python cp.py run --force) like start() does for events."""
    if contest_running():
        return {"ok": True, "already": True}
    s = settings.load()
    picked = [a for a, on in (s.get("agents") or {}).items() if on]
    dirs = {a: logins.logged_in_dirs(a) for a in picked if a in logins.QUEUE_AGENTS}
    ready = [a for a in picked if (dirs[a] if a in dirs else logins.status(a).get("logged_in"))]
    if not ready:
        return {"ok": False, "error": "로그인된 AI가 없습니다. 설정에서 AI에 로그인하세요."}
    ensure_contest_initialized()
    from cp import accounts, store  # noqa: E402
    db = store.connect()
    try:
        with store.writer(db):
            store.kv_set(db, "stop", False)
        for a in ready:
            accounts.unblock(db, a)
    finally:
        db.close()
    logs = os.path.join(contest_dir(), "logs")
    os.makedirs(logs, exist_ok=True)
    log = open(os.path.join(logs, "scheduler.log"), "a", encoding="utf-8")
    env = contest_env()
    env["CP_AGENTS"] = ",".join(ready)
    if dirs.get("claude"):
        env["CP_CLAUDE_CONFIG_DIRS"] = os.pathsep.join(dirs["claude"])
        env["CP_CLAUDE_CONFIG_DIR"] = dirs["claude"][0]
    if dirs.get("codex"):
        env["CP_CODEX_HOMES"] = os.pathsep.join(dirs["codex"])
        env["CP_CODEX_HOME"] = dirs["codex"][0]
    p = subprocess.Popen(python_cmd() + ["-X", "utf8", cp_py(), "run", "--force"], cwd=contest_dir(),
                         env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=NO_WINDOW | DETACHED, close_fds=True)
    open(contest_pid_file(), "w").write(str(p.pid))
    return {"ok": True, "pid": p.pid, "agents": ready}


def stop_contest() -> dict:
    ensure_contest_initialized()
    from cp import store  # noqa: E402
    db = store.connect()
    try:
        with store.writer(db):
            store.kv_set(db, "stop", True)
    finally:
        db.close()
    return {"ok": True}


def contest_tail_log(n: int = 60) -> list:
    p = os.path.join(contest_dir(), "logs", "scheduler.log")
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 20000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


def start_modes() -> dict:
    """Start the engine of every mode that is on in the settings (events: ep, contests: cp)."""
    modes = settings.load().get("modes") or {}
    out = {}
    if modes.get("events", True):
        out["events"] = start()
    if modes.get("contests"):
        out["contests"] = start_contest()
    if not out:
        return {"ok": False, "error": "켜진 모드가 없습니다.", "modes": {}}
    errs = [v.get("error") for v in out.values() if not v.get("ok")]
    first = out.get("events") or out.get("contests")
    return dict(first, ok=not errs, error="; ".join(e for e in errs if e), modes=out) if errs else dict(first, ok=True, modes=out)


def stop_modes() -> dict:
    out = {"events": stop()}
    if os.path.isfile(os.path.join(contest_dir(), "platform.db")):
        out["contests"] = stop_contest()
    return {"ok": True, "modes": out}
