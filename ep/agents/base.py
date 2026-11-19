"""Adapter base class, process launch and reaping for agent runs.

launch() starts one child process and inserts a `runs` row; reap() records the end of every finished run. Classification
of a finished run (`ok | spent | limit | auth | crash`) is by log text and exit code; the scheduler decides what the
class means for the item and the agent.
"""
import contextlib
import os
import re
import secrets
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from ep import config, store

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
LOG_TAIL_CHARS = 4000
SHORT_LOG_CHARS = 1500  # exit-0 logs up to this size are still checked for limit/spent notices

# Result classification markers.
SPENT_RE = re.compile(r"out of credits|add credits|usage balance exhausted|payment required|billing/enable", re.I)
LIMIT_RE = re.compile(r"usage limit|rate limit|quota exceeded|hit your (?:\S+ )?limit|\b429\b", re.I)
AUTH_RE = re.compile(r"not logged in|\b401\b|authentication", re.I)
# Phrase-only forms, used when the exit code is unknown (adopted orphan run): bare numbers are too common in a log.
SPENT_PHRASE_RE = SPENT_RE
LIMIT_PHRASE_RE = re.compile(r"usage limit|rate limit|quota exceeded|hit your (?:\S+ )?limit", re.I)
AUTH_PHRASE_RE = re.compile(r"not logged in|authentication (failed|error|required)", re.I)

# Popen objects of runs started by this process: run id -> Popen (only these have a known exit code).
PROCS: dict = {}
RUN_TASKS: dict = {}
_BOOT_REAPED: set = set()

TG_TOKEN_RE = re.compile(r"(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")
APP_PW_GROUPED_RE = re.compile(r"(?<![A-Za-z0-9])(?:[a-z]{4}[ \u00a0]){3}[a-z]{4}(?![A-Za-z0-9])")


def _sys32_exe(name: str) -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(root, "System32", name)


def scrub_text(text: str, secrets=None) -> str:
    if not text:
        return text
    if secrets:
        for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
            text = text.replace(secret, "[hidden]")
    text = TG_TOKEN_RE.sub("[hidden]", text)
    return APP_PW_GROUPED_RE.sub("[hidden]", text)


def scrub_log(path: str, secrets=None):
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        clean = scrub_text(raw, secrets=secrets)
        if clean != raw:
            with open(path, "w", encoding="utf-8") as f:
                f.write(clean)
    except OSError:
        pass


class LaunchError(Exception):
    """The child process could not be started; `run_id` is the (already ended) runs row."""

    def __init__(self, message, run_id=None):
        super().__init__(message)
        self.run_id = run_id


class Adapter:
    """One agent CLI. Subclasses set `name` and implement command(); env()/classify() may be extended."""
    name = ""

    @property
    def capabilities(self) -> set:
        return set(config.CAPABILITIES.get(self.name, set()))

    def effort(self, kind: str):
        """Reasoning effort for this kind of task, or None when the CLI has no such option."""
        return None

    def pick_account(self, db):
        """Account for the next run (claude account name, codex home path) or None. May raise RuntimeError when no
        account is usable."""
        return None

    def command(self, prompt: str, kind: str) -> list:
        raise NotImplementedError

    def env(self, base: dict, kind: str, account=None) -> dict:
        """Child environment: a copy of `base` plus the account variables. Subclasses scrub and add their own."""
        return dict(base)

    def classify(self, log_text: str, code) -> str:
        return classify_log(log_text, code)


def classify_log(log_text: str, code) -> str:
    """'ok' | 'spent' | 'limit' | 'auth' | 'crash'.

    Exit code 0 is 'ok' (an agent that finished cleanly is not judged by words in its transcript).
    A non-zero code is judged by the markers in the log tail: spent, then limit, then auth, else 'crash'.
    Unknown code (None: adopted orphan run) uses phrase markers only and is 'ok' when none matches."""
    text = log_text or ""
    if code == 0:
        # a CLI that printed only a limit/credit notice and exited 0 (for example "You've hit your 5-hour limit")
        # did no work: judge such short logs by the phrase markers.
        if len(text) <= SHORT_LOG_CHARS:
            for cls, rx in (("spent", SPENT_PHRASE_RE), ("limit", LIMIT_PHRASE_RE)):
                if rx.search(text):
                    return cls
        return "ok"
    if code is None:
        for cls, rx in (("spent", SPENT_PHRASE_RE), ("limit", LIMIT_PHRASE_RE), ("auth", AUTH_PHRASE_RE)):
            if rx.search(text):
                return cls
        return "ok"
    for cls, rx in (("spent", SPENT_RE), ("limit", LIMIT_RE), ("auth", AUTH_RE)):
        if rx.search(text):
            return cls
    return "crash"


def _timed_out(r: dict, now: datetime) -> bool:
    """True when the run has lived longer than config.RUN_TIMEOUT_SEC for its kind (explicit per-kind limit)."""
    limit = config.RUN_TIMEOUT_SEC.get(r.get("kind"), config.RUN_TIMEOUT_DEFAULT_SEC)
    try:
        started = datetime.fromisoformat(r.get("started") or "")
    except (TypeError, ValueError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=now.tzinfo)
    return (now - started).total_seconds() > limit


def kill_tree(pid: int) -> bool:
    """Kill a process and its children (taskkill /T /F). Returns True when taskkill reported success."""
    try:
        r = subprocess.run([_sys32_exe("taskkill.exe"), "/PID", str(int(pid)), "/T", "/F"], capture_output=True,
                           text=True, encoding="mbcs", errors="replace", timeout=30, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


# ---------- logs and runs rows ----------
def log_tail(path: str, n: int = LOG_TAIL_CHARS) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n * 4))
            return f.read().decode("utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def log_path(agent: str, kind: str, tag=None, create: bool = True) -> str:
    """<data>\\logs\\<yyyymmdd-hhmmss>-<agent>-<kind>[-<id>].log ; a number is appended when the name exists.
    create=False (dry run) does not create the logs folder."""
    if create:
        os.makedirs(config.logs_dir(), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{agent}-{kind}" + (f"-{tag}" if tag else "")
    path, n = os.path.join(config.logs_dir(), base + ".log"), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(config.logs_dir(), f"{base}-{n}.log")
    return path


@contextlib.contextmanager
def _write(db):
    if db.in_transaction:
        yield db
    else:
        with store.writer(db):
            yield db


def common_env(item_id=None, base=None) -> dict:
    """Variables every child gets: UTF-8 output, EVENT_ITEM; TERM and Gemini keys removed."""
    env = dict(os.environ if base is None else base)
    for k in list(env):
        if k.upper().startswith(("GEMINI_", "GOOGLE_API", "LCH_")) or k.upper() == "GEMINI_API_KEY":
            env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    if item_id:
        env["EVENT_ITEM"] = item_id
    else:
        env.pop("EVENT_ITEM", None)
    env.pop("TERM", None)  # Git Bash's TERM made prompt_toolkit crash
    return env


def _label(adapter, account):
    """runs.account: claude account name, codex account name (never a path or token)."""
    if not account:
        return None
    if adapter.name == "codex":
        from ep import accounts
        return accounts.codex_name(account)
    return str(account)


def is_reparse(path: str) -> bool:
    """True when `path` is a symlink or NTFS junction/reparse point."""
    if not path:
        return False
    if os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path):
        return True
    try:
        st = os.lstat(path)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400 or getattr(st, "st_reparse_tag", 0))
    except OSError:
        return False


def _check_no_reparse_to_data(target: str, data_root: str | None = None) -> str:
    """Verify `target` and every parent up to `data_root` have no reparse points. Raises LaunchError if unsafe."""
    root_abs = os.path.abspath(data_root or config.data_dir())
    target_abs = os.path.abspath(target)
    try:
        common = os.path.commonpath([os.path.normcase(root_abs), os.path.normcase(target_abs)])
    except ValueError:
        raise LaunchError(f"task path outside data dir: {target}") from None
    if common != os.path.normcase(root_abs):
        raise LaunchError(f"task path outside data dir: {target}")
    if os.path.lexists(root_abs) and is_reparse(root_abs):
        raise LaunchError(f"data folder is a reparse point: {root_abs}")
    rel = os.path.relpath(target_abs, root_abs)
    cur = root_abs
    if rel != ".":
        for part in rel.split(os.sep):
            if part in ("", ".", ".."):
                raise LaunchError(f"invalid task path: {target}")
            cur = os.path.join(cur, part)
            if os.path.lexists(cur) and is_reparse(cur):
                raise LaunchError(f"tasks path is a reparse point: {cur}")
    return target_abs


def _restrict_tasks_acl(tasks_dir: str) -> None:
    try:
        from app import secure as _app_secure
    except ImportError:
        return
    try:
        _app_secure.restrict_acl(tasks_dir)
    except OSError as e:
        raise LaunchError(f"cannot restrict tasks folder ACL ({tasks_dir}): {e}") from None


def _ensure_tasks_dir() -> str:
    data_root = os.path.abspath(config.data_dir())
    tasks_dir = os.path.join(data_root, "tasks")
    _check_no_reparse_to_data(tasks_dir, data_root)
    os.makedirs(tasks_dir, exist_ok=True)
    _check_no_reparse_to_data(tasks_dir, data_root)
    real_root = os.path.realpath(data_root)
    real_tasks = os.path.realpath(tasks_dir)
    try:
        common = os.path.commonpath([os.path.normcase(real_root), os.path.normcase(real_tasks)])
    except ValueError:
        raise LaunchError(f"tasks folder escaped data dir: {tasks_dir}") from None
    if common != os.path.normcase(real_root):
        raise LaunchError(f"tasks folder escaped data dir: {tasks_dir}")
    _restrict_tasks_acl(tasks_dir)
    return tasks_dir


def _safe_task_prefix(agent: str, kind: str, key) -> str:
    raw = f"{agent}-{kind}-{key or 'run'}"
    safe = re.sub(r'[\\/:*?"<>|\r\n\x00]+', "_", raw).strip(". ")[:64]
    if not safe or safe.lower() == "agent-guide":
        safe = "run"
    return safe


def _safe_task_filename(agent: str, kind: str, key) -> str:
    return _safe_task_prefix(agent, kind, key) + ".md"


def _random_task_filename(agent: str, kind: str, key) -> str:
    return f"{_safe_task_prefix(agent, kind, key)}-{secrets.token_hex(16)}.md"


def _check_file_links_and_reparse(path: str, fd: int | None = None) -> None:
    if is_reparse(path):
        raise LaunchError(f"task file is a reparse point: {path}")
    try:
        if fd is not None:
            st_fd = os.fstat(fd)
            if getattr(st_fd, "st_nlink", 1) > 1:
                raise LaunchError(f"task file has multiple hard links: {path}")
        st_path = os.stat(path)
    except LaunchError:
        raise
    except OSError as e:
        raise LaunchError(f"cannot stat task file ({path}): {e}") from None
    if getattr(st_path, "st_nlink", 1) > 1:
        raise LaunchError(f"task file has multiple hard links: {path}")


def cleanup_task_file(path: str | None):
    if not path or not isinstance(path, str) or not (path.endswith(".md") or path.endswith(".tmp")):
        return
    if os.path.basename(path).lower() == "agent-guide.md":
        return
    data_root = os.path.abspath(config.data_dir())
    tasks_dir = os.path.join(data_root, "tasks")
    try:
        _check_no_reparse_to_data(tasks_dir, data_root)
        _check_no_reparse_to_data(path, data_root)
    except LaunchError:
        return
    tasks_root = os.path.realpath(tasks_dir)
    real = os.path.realpath(path)
    if os.path.normcase(real).startswith(os.path.normcase(tasks_root) + os.sep):
        with contextlib.suppress(OSError):
            os.remove(real)


def _write_exclusive_file(path: str, content: str, allow_cleanup: bool = True) -> None:
    if os.path.lexists(path):
        if is_reparse(path):
            raise LaunchError(f"task file is a reparse point: {path}")
        raise LaunchError(f"task file already exists: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise LaunchError(f"task file already exists: {path}") from None
    except OSError as e:
        raise LaunchError(f"cannot create task file ({path}): {e}") from None
    try:
        _check_file_links_and_reparse(path, fd)
        data = content.encode("utf-8")
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
    except BaseException as e:
        with contextlib.suppress(OSError):
            os.close(fd)
        if allow_cleanup:
            cleanup_task_file(path)
        else:
            with contextlib.suppress(OSError):
                os.remove(path)
        if isinstance(e, LaunchError):
            raise
        raise LaunchError(f"cannot write task file ({path}): {e}") from None
    else:
        os.close(fd)


def _ensure_guide_file(tasks_dir: str, text: str) -> str:
    guide = os.path.join(tasks_dir, "agent-guide.md")
    if os.path.lexists(guide):
        _check_file_links_and_reparse(guide)
        try:
            with open(guide, encoding="utf-8") as f:
                if f.read() == text:
                    return guide
        except OSError as e:
            raise LaunchError(f"cannot read agent guide ({guide}): {e}") from None
        tmp_guide = os.path.join(tasks_dir, f".agent-guide-{secrets.token_hex(16)}.tmp")
        _write_exclusive_file(tmp_guide, text)
        try:
            _check_file_links_and_reparse(guide)
            os.replace(tmp_guide, guide)
        except BaseException as e:
            cleanup_task_file(tmp_guide)
            if isinstance(e, LaunchError):
                raise
            raise LaunchError(f"cannot update agent guide ({guide}): {e}") from None
        return guide
    _write_exclusive_file(guide, text, allow_cleanup=False)
    return guide


def _task_file_with_path(agent: str, kind: str, key, prompt: str) -> tuple[str, str]:
    src = os.path.join(config.PROJECT_DIR, "prompts", "agent-guide.md")
    try:
        with open(src, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise LaunchError(f"cannot read agent guide ({src}): {e}") from None
    d = _ensure_tasks_dir()
    guide = _ensure_guide_file(d, text)
    path = os.path.join(d, _random_task_filename(agent, kind, key))
    _write_exclusive_file(path, prompt)
    return ((f"Read {guide} and then {path}, both completely, and do exactly the single task written in {path}. "
             f"When it is done, stop."), path)


def task_file_instruction(agent: str, kind: str, key, prompt: str) -> str:
    """One-shot run: the full task goes into a .md file under <data_dir>\\tasks, the agent gets one short line that
    points at it. Raises LaunchError if the tasks folder is unsafe or the guide/task file cannot be read/written."""
    return _task_file_with_path(agent, kind, key, prompt)[0]


def _run_alive(r: dict) -> bool:
    p = PROCS.get(r["id"])
    if p is not None:
        return p.poll() is None
    return bool(r.get("pid")) and store.pid_alive(r["pid"])


def reap_orphan_tasks(db=None) -> list[str]:
    """Delete leftover task .md files older than the newest running task (or all leftover .md files if none running)."""
    own = db is None
    db = db or store.connect()
    deleted: list[str] = []
    try:
        data_root = os.path.abspath(config.data_dir())
        tasks_dir = os.path.join(data_root, "tasks")
        if not os.path.lexists(tasks_dir):
            return deleted
        try:
            _check_no_reparse_to_data(tasks_dir, data_root)
        except LaunchError:
            return deleted
        if not os.path.isdir(tasks_dir):
            return deleted

        live_runs = [r for r in active_runs(db) if _run_alive(r)]
        protected: set[str] = set()
        protected_prefixes: list[str] = []
        newest_ts: float | None = None
        kst = ZoneInfo(config.KST_TZ)

        for r in live_runs:
            cands = [
                RUN_TASKS.get(r["id"]),
                store.kv_get(db, f"run.task.{r['id']}"),
            ]
            if r.get("item_id"):
                cands.append(os.path.join(tasks_dir, _safe_task_filename(r["agent"], r["kind"], r["item_id"])))
            scout_out = store.kv_get(db, f"scout.out.{r['id']}")
            if isinstance(scout_out, str) and scout_out:
                cands.append(os.path.join(tasks_dir, _safe_task_filename(r["agent"], "scout", os.path.basename(scout_out))))
                protected_prefixes.append((_safe_task_prefix(r["agent"], "scout", os.path.basename(scout_out)) + "-").lower())
            file_mtimes: list[float] = []
            for cp in cands:
                if isinstance(cp, str) and cp and os.path.lexists(cp) and not is_reparse(cp):
                    protected.add(os.path.normcase(os.path.realpath(cp)))
                    with contextlib.suppress(OSError):
                        file_mtimes.append(os.path.getmtime(cp))
            ts = None
            try:
                dt_started = datetime.fromisoformat(r.get("started") or "")
                if dt_started.tzinfo is None:
                    dt_started = dt_started.replace(tzinfo=kst)
                ts = dt_started.timestamp()
            except (TypeError, ValueError):
                pass
            candidates_ts = ([ts] if ts is not None else []) + file_mtimes
            if candidates_ts:
                r_ts = max(candidates_ts)
                newest_ts = r_ts if newest_ts is None else max(newest_ts, r_ts)

        try:
            entries = list(os.listdir(tasks_dir))
        except OSError:
            return deleted
        for name in entries:
            if not name.lower().endswith(".md") or name.lower() == "agent-guide.md":
                continue
            fp = os.path.join(tasks_dir, name)
            if is_reparse(fp) or not os.path.isfile(fp):
                continue
            if os.path.normcase(os.path.realpath(fp)) in protected:
                continue
            if any(name.lower().startswith(pfx) for pfx in protected_prefixes):
                continue
            if live_runs:
                if newest_ts is None:
                    continue
                try:
                    mtime = os.path.getmtime(fp)
                except OSError:
                    continue
                if mtime >= newest_ts:
                    continue
            try:
                os.remove(fp)
                deleted.append(fp)
            except OSError:
                pass
        return deleted
    finally:
        if own:
            db.close()


def launch(adapter: Adapter, kind: str, item, prompt: str, db, tag=None, account=None,
           command=None) -> int:
    """Start the agent process for one task and insert the runs row. Returns the run id.

    item: item dict (its id goes to EVENT_ITEM and runs.item_id) or None (scout, backfill). tag: log-name suffix when
    there is no item (for example the scout region). account: already chosen account (default: adapter.pick_account).
    stdin is a closed pipe (NUL looks like a TTY to some CLIs)."""
    item_id = item.get("id") if item else None
    account = account or adapter.pick_account(db)
    env = adapter.env(common_env(item_id), kind, account)
    started = store.now_iso()
    path = log_path(adapter.name, kind, item_id or tag, create=False)
    task_path = ""
    try:
        _check_no_reparse_to_data(os.path.join(config.data_dir(), "tasks"), config.data_dir())
        if command:  # e.g. the KR scout wrapper (ep.scout_run) that runs the agent itself after crawling
            cmd = list(command)
        else:
            instr, task_path = _task_file_with_path(adapter.name, kind, item_id or tag, prompt)
            cmd = adapter.command(instr, kind)
        path = log_path(adapter.name, kind, item_id or tag, create=True)
        os.makedirs(config.AGENT_CWD, exist_ok=True)
        with open(path, "w", encoding="utf-8") as logf:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT, env=env,
                                 cwd=config.AGENT_CWD, creationflags=NO_WINDOW)
        p.stdin.close()
    except (LaunchError, OSError) as e:
        cleanup_task_file(task_path)
        with _write(db):
            cur = db.execute("INSERT INTO runs(kind, item_id, agent, account, pid, log, started, ended, code, outcome) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (kind, item_id, adapter.name, _label(adapter, account), None, path, started,
                              store.now_iso(), -1, "launch_failed"))
            store.add_error(db, adapter.name, f"{kind} launch failed: {type(e).__name__}: {e}")
        raise LaunchError(f"{adapter.name} {kind}: {e}", cur.lastrowid) from None
    with _write(db):
        cur = db.execute("INSERT INTO runs(kind, item_id, agent, account, pid, log, started) VALUES(?,?,?,?,?,?,?)",
                         (kind, item_id, adapter.name, _label(adapter, account), p.pid, path, started))
        run_id = cur.lastrowid
        if task_path:
            store.kv_set(db, f"run.task.{run_id}", task_path)
        if item_id:
            store.add_history(db, item_id, adapter.name, "run-start", f"{kind} run {run_id}")
    if task_path:
        RUN_TASKS[run_id] = task_path
    PROCS[run_id] = p
    return run_id


def active_runs(db) -> list:
    return [dict(r) for r in db.execute("SELECT * FROM runs WHERE ended IS NULL OR ended='' ORDER BY id")]


def set_outcome(db, run_id: int, outcome: str):
    with _write(db):
        db.execute("UPDATE runs SET outcome=? WHERE id=?", (outcome, run_id))


def reap(db, adapters: dict | None = None) -> list:
    """Record the end of every finished un-ended run. A run is finished when its Popen (started by this process) has an
    exit code, or, for a run of another process, when its pid is no longer alive (exit code then unknown = None).

    Returns a list of dicts {run, kind, item_id, agent, account, code, log, cls, orphan, tail} for the scheduler to
    apply its outcome rules to. runs.outcome is set to the class (`orphan` for a foreign run whose class is 'ok')."""
    adapters = adapters or {}
    done = []
    now = datetime.now(ZoneInfo(config.KST_TZ))
    for r in active_runs(db):
        p = PROCS.get(r["id"])
        timed_out = _timed_out(r, now)
        if p is not None:
            code = p.poll()
            if code is None:
                if not timed_out:
                    continue
                kill_tree(p.pid)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=15)
                code = p.returncode if p.returncode is not None else -9
            orphan = False
        else:
            alive = bool(r["pid"]) and store.pid_alive(r["pid"])
            if alive and not timed_out:
                continue
            if alive:
                kill_tree(r["pid"])
            code, orphan = None, True
        scrub_log(r["log"] or "")
        tail = scrub_text(log_tail(r["log"] or ""))
        ad = adapters.get(r["agent"])
        cls = ad.classify(tail, code) if ad else classify_log(tail, code)
        if timed_out:
            cls = "crash"
        outcome = "timeout" if timed_out else ("orphan" if (orphan and cls == "ok") else cls)
        with _write(db):
            saved_task = store.kv_get(db, f"run.task.{r['id']}")
            store.kv_del(db, f"run.task.{r['id']}")
            db.execute("UPDATE runs SET ended=?, code=?, outcome=? WHERE id=?",
                       (store.now_iso(), code, outcome, r["id"]))
        PROCS.pop(r["id"], None)
        tpath = RUN_TASKS.pop(r["id"], None) or (saved_task if isinstance(saved_task, str) else "")
        cleanup_task_file(tpath)
        if r.get("item_id"):
            still = {(x["agent"], x["kind"], x["item_id"]) for x in active_runs(db)}
            if (r["agent"], r["kind"], r["item_id"]) not in still:
                cleanup_task_file(os.path.join(config.data_dir(), "tasks",
                                               _safe_task_filename(r["agent"], r["kind"], r["item_id"])))
        done.append({"run": r["id"], "kind": r["kind"], "item_id": r["item_id"], "agent": r["agent"],
                     "account": r["account"], "code": code, "log": r["log"], "cls": cls, "orphan": orphan,
                     "tail": tail, "timeout": timed_out})
    boot_key = (os.getpid(), os.path.normcase(os.path.abspath(config.data_dir())))
    if boot_key not in _BOOT_REAPED:
        _BOOT_REAPED.add(boot_key)
        reap_orphan_tasks(db)
    return done
