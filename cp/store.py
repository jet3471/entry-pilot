"""SQLite access: schema, connection, single-writer lock, row helpers.

One writer at a time: `with store.writer() as db:` takes the lock file (holder judged only by whether its pid is
alive; never by age) and runs everything inside one IMMEDIATE transaction. Readers use `store.connect()`.
"""
import contextlib
import ctypes
import json
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from cp import config

SCHEMA_VERSION = 2

# Columns beyond the design table (assumption): deadline_raw (unparseable deadline text, task order),
# site_host (exact host for site_kind families), notes (scout notes / owner share note; replaces the old
# notes + share_note fields).
SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, region TEXT NOT NULL DEFAULT 'KR', title TEXT, title_en TEXT, org TEXT, url TEXT,
  source_url TEXT, alt_urls TEXT DEFAULT '[]', form_type TEXT, site_kind TEXT, site_host TEXT,
  deadline TEXT DEFAULT '', deadline_time TEXT DEFAULT '', deadline_raw TEXT, tz TEXT,
  prize TEXT, prize_min_value INTEGER, winners INTEGER, all_rewarded INTEGER, effort_min INTEGER,
  login_required INTEGER, conditions TEXT, result_date TEXT, eligibility TEXT DEFAULT 'unknown',
  state TEXT NOT NULL, lane TEXT, need TEXT, reason TEXT, question TEXT, verify_notes TEXT, legacy_notes TEXT,
  notes TEXT, answers TEXT, claimed_by TEXT, claimed_at TEXT, added_by TEXT, added_at TEXT, updated_at TEXT,
  tries TEXT DEFAULT '{}', flags TEXT DEFAULT '{}', evidence TEXT, submitted_at TEXT, submitted_by TEXT,
  work TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS items_state ON items(state);
CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, at TEXT, agent TEXT,
  action TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS history_item ON history(item_id);
CREATE TABLE IF NOT EXISTS pool(id TEXT PRIMARY KEY, region TEXT DEFAULT 'KR', title TEXT, org TEXT, url TEXT,
  source_url TEXT, form_type TEXT, deadline TEXT, prize TEXT, login_required INTEGER, conditions TEXT,
  result_date TEXT, winners INTEGER, prize_min_value INTEGER, all_rewarded INTEGER, effort_min INTEGER,
  added_at TEXT, added_by TEXT, promoted_to TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, item_id TEXT, agent TEXT,
  account TEXT, pid INTEGER, log TEXT, started TEXT, ended TEXT, code INTEGER, outcome TEXT,
  tokens_in INTEGER, tokens_out INTEGER);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, agent TEXT, message TEXT,
  resolved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS owner_answers(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, kind TEXT, text TEXT,
  at TEXT, applied INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS i18n(hash TEXT PRIMARY KEY, en TEXT, ko TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS wins(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, won_at TEXT, prize TEXT,
  note TEXT, recorded_by TEXT, at TEXT);
"""
# consents: stay in the legacy consents.jsonl owned by cp/consent.py (task order); no table here.

# `work` (contest clone): {work_type, theme, format, submit_method, top_prize_krw, draft_dir, draft_files, facts_used,
# review_note, revisions}. JSON so new contest fields need no migration.
JSON_COLS = {"alt_urls", "answers", "tries", "flags", "work"}
ITEM_COLS = ("id", "region", "title", "title_en", "org", "url", "source_url", "alt_urls", "form_type", "site_kind",
             "site_host", "deadline", "deadline_time", "deadline_raw", "tz", "prize", "prize_min_value", "winners",
             "all_rewarded", "effort_min", "login_required", "conditions", "result_date", "eligibility", "state",
             "lane", "need", "reason", "question", "verify_notes", "legacy_notes", "notes", "answers", "claimed_by",
             "claimed_at", "added_by", "added_at", "updated_at", "tries", "flags", "evidence", "submitted_at",
             "submitted_by", "work")
POOL_COLS = ("id", "region", "title", "org", "url", "source_url", "form_type", "deadline", "prize", "login_required",
             "conditions", "result_date", "winners", "prize_min_value", "all_rewarded", "effort_min", "added_at",
             "added_by", "promoted_to")


class StoreError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(ZoneInfo(config.KST_TZ)).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or config.db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; transactions are explicit
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(items)")}
    if "work" not in cols:  # database created before schema 2
        db.execute("ALTER TABLE items ADD COLUMN work TEXT DEFAULT '{}'")
    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return db


# ---------- single-writer lock ----------
def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        k32 = ctypes.windll.kernel32
    except AttributeError:  # non-Windows fallback
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


class FileLock:
    """Lock file holding the writer pid. A stale lock is taken over only when its pid is no longer alive.
    Waiting gives up after `wait_sec` with a visible error (it never assumes the holder is dead)."""

    RELEASE_WAIT_SEC = 30.0

    def __init__(self, path: str | None = None, wait_sec: float = 120.0):
        self.path = path or config.lock_path()
        self.wait_sec = wait_sec

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        start = time.monotonic()
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except (FileExistsError, PermissionError):
                try:
                    with open(self.path, encoding="utf-8") as f:
                        holder = int(f.read().strip() or 0)
                except (OSError, ValueError):
                    holder = 0
                if holder and holder != os.getpid() and not pid_alive(holder):
                    with contextlib.suppress(OSError):
                        os.remove(self.path)
                    continue
                if time.monotonic() - start > self.wait_sec:
                    raise StoreError(f"writer lock held by pid {holder}: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        # On Windows os.remove fails while a waiting process has the lock file open to read the holder pid.
        # The failure used to be suppressed, the file stayed with the pid of a live process, and every writer (this
        # process too) waited for ever. Now the removal is retried, and a lock that cannot be removed is an error.
        start = time.monotonic()
        while True:
            try:
                os.remove(self.path)
                return
            except FileNotFoundError:
                return
            except OSError as e:
                if time.monotonic() - start > self.RELEASE_WAIT_SEC:
                    raise StoreError(f"writer lock not released, remove failed: {e}: {self.path}")
                time.sleep(0.02)


@contextlib.contextmanager
def writer(db: sqlite3.Connection | None = None):
    """Lock + IMMEDIATE transaction. Commits on success, rolls back on any exception."""
    own = db is None
    db = db or connect()
    try:
        with FileLock():
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
    finally:
        if own:
            db.close()


# ---------- row helpers ----------
def _dec(col, v):
    if col in JSON_COLS:
        if v in (None, ""):
            return [] if col == "alt_urls" else ({} if col in ("tries", "flags", "work") else None)
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _enc(col, v):
    if col in JSON_COLS and v is not None and not isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def row_to_item(row) -> dict | None:
    if row is None:
        return None
    return {k: _dec(k, row[k]) for k in row.keys()}


def get_item(db, item_id: str) -> dict | None:
    return row_to_item(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())


def items(db, where: str = "1", params=()) -> list[dict]:
    return [row_to_item(r) for r in db.execute(f"SELECT * FROM items WHERE {where}", params)]


def insert_item(db, it: dict):
    cols = [c for c in ITEM_COLS if c in it]
    db.execute(f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
               [_enc(c, it[c]) for c in cols])


def update_item(db, item_id: str, **fields):
    if not fields:
        return
    fields.setdefault("updated_at", now_iso())
    bad = [c for c in fields if c not in ITEM_COLS]
    if bad:
        raise StoreError(f"unknown item fields: {bad}")
    sets = ", ".join(f"{c}=?" for c in fields)
    db.execute(f"UPDATE items SET {sets} WHERE id=?", [_enc(c, v) for c, v in fields.items()] + [item_id])


def add_history(db, item_id, agent, action, detail="", at=None):
    db.execute("INSERT INTO history(item_id, at, agent, action, detail) VALUES(?,?,?,?,?)",
               (item_id, at or now_iso(), agent, action, detail or ""))


def history(db, item_id: str) -> list[dict]:
    return [dict(r) for r in db.execute("SELECT * FROM history WHERE item_id=? ORDER BY seq", (item_id,))]


def insert_pool(db, p: dict):
    cols = [c for c in POOL_COLS if c in p]
    db.execute(f"INSERT INTO pool({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
               [_enc(c, p[c]) for c in cols])


def pool_rows(db, where: str = "1", params=()) -> list[dict]:
    return [dict(r) for r in db.execute(f"SELECT * FROM pool WHERE {where} ORDER BY id", params)]


# ---------- kv ----------
# Values: plain strings are stored as-is; anything else (bool, number, dict, list) as JSON.
# kv_get decodes JSON when it can and otherwise returns the raw string.
def kv_get(db, key: str, default=None):
    r = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r[0])
    except (ValueError, TypeError):
        return r[0]


def kv_set(db, key: str, value):
    v = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, v))


def kv_del(db, key: str):
    db.execute("DELETE FROM kv WHERE key=?", (key,))


def kv_prefix(db, prefix: str) -> dict:
    out = {}
    for k, v in db.execute("SELECT key, value FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)):
        try:
            out[k] = json.loads(v)
        except (ValueError, TypeError):
            out[k] = v
    return out


# ---------- ids ----------
def next_item_id(db) -> str:
    """Item ids C001.. (config.ITEM_PREFIX). kv next_id holds the next number."""
    n = kv_get(db, "next_id")
    if not isinstance(n, int):
        n = 1
    mx = 0
    pfx = config.ITEM_PREFIX
    for (iid,) in db.execute("SELECT id FROM items WHERE id LIKE ?", (pfx + "%",)):
        if iid[1:].isdigit():
            mx = max(mx, int(iid[1:]))
    n = max(n, mx + 1)
    kv_set(db, "next_id", n + 1)
    return f"{pfx}{n:03d}"


def next_pool_id(db) -> str:
    mx = 0
    for (pid,) in db.execute("SELECT id FROM pool WHERE id LIKE 'P%'"):
        if pid[1:].isdigit():
            mx = max(mx, int(pid[1:]))
    return f"P{mx + 1:03d}"


def add_error(db, agent: str, message: str) -> int:
    cur = db.execute("INSERT INTO errors(at, agent, message, resolved) VALUES(?,?,?,0)",
                     (now_iso(), agent, message))
    return cur.lastrowid
