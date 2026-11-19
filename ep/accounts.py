"""Account handling for the scheduler: Claude account pick, Codex home rotation, blocked flags.

kv keys used here:
  claude_account            Claude account currently chosen
  claude_spent              JSON list of Claude accounts that failed with a spent/limit marker
  codex_home                Codex home folder currently used
  codex_spent               JSON list of Codex home folders that were spent
  blocked.<agent>           reason text; the agent is never launched while this exists

Every login is the user's own, kept by the AI's CLI in one folder per account. No tokens are read or printed here.
Functions that write take an open sqlite connection; when the caller already holds a write transaction
(db.in_transaction) the write joins it, otherwise it takes the store lock.
"""
import contextlib
import json
import os
import sys

from ep import config, store

# Name given to the default Codex home (~/.codex); the other homes are named by their folder name.
DEFAULT_CODEX_HOME_NAME = "default"

LAST_USAGE_ERROR = ""  # text of the last failed usage() call


@contextlib.contextmanager
def _write(db):
    """Join the caller's open write transaction, or take the store lock for one."""
    if db.in_transaction:
        yield db
    else:
        with store.writer(db):
            yield db


# ---------- usage ----------
def usage(agent: str, db=None, live: bool = False) -> list:
    """Live usage rows [{id, status, stale, limits:[{kind, percent, resetsAt}]}] for `claude` or `codex`.

    The engine has no live usage source: limits are seen from run logs only, so this returns []. It is the one place a
    usage source can be plugged in; every caller already handles an empty list."""
    global LAST_USAGE_ERROR
    LAST_USAGE_ERROR = ""
    return []


def summarize(rows: list) -> list:
    """Usage rows reduced to what `ep accounts` prints (no e-mail, no source/detail text, no tokens)."""
    out = []
    for r in rows or []:
        out.append({"id": r.get("id"), "status": r.get("status"), "stale": bool(r.get("stale")), "at": r.get("at"),
                    "limits": [{"kind": x.get("kind"), "percent": x.get("percent"), "resets_at": x.get("resetsAt")}
                               for x in (r.get("limits") or [])]})
    return out


def _percent(row: dict, kind: str):
    for x in row.get("limits") or []:
        if x.get("kind") == kind and isinstance(x.get("percent"), (int, float)):
            return x["percent"]
    return None


# ---------- Claude ----------
def claude_rank(rows: list, spent=(), current: str | None = None) -> list:
    """Eligible Claude accounts, best first: lowest weekly_all percent; ties -> `current`, then config order.
    Excluded: accounts in `spent`; accounts whose weekly_all reading is 100 or more (stale or not: the reading itself is
    the only signal we have), or whose non-stale 5 hour (session) reading is 100 or more. Accounts without a reading
    stay eligible and sort last."""
    by_id = {r.get("id"): r for r in rows or []}
    ranked = []
    for i, acct in enumerate(config.CLAUDE_ACCOUNTS):
        if acct in spent:
            continue
        row = by_id.get(acct)
        weekly = _percent(row, "weekly_all") if row else None
        session = _percent(row, "session") if row else None
        if weekly is not None and weekly >= 100:
            continue
        if row and not row.get("stale") and session is not None and session >= 100:
            continue
        ranked.append((weekly if weekly is not None else 1000, 0 if acct == current else 1, i, acct))
    ranked.sort()
    return [a for _, _, _, a in ranked]


def claude_pick(db) -> str:
    """Choose the Claude account (lowest weekly usage, not spent, not at 100 %), store it in kv `claude_account`.
    Returns "" when no account is eligible. Reads usage through usage(), so tests can monkeypatch it."""
    rows = usage("claude")
    if not rows and LAST_USAGE_ERROR:
        with _write(db):
            store.add_error(db, "system", LAST_USAGE_ERROR + "; Claude account chosen without usage data")
    spent = store.kv_get(db, "claude_spent", []) or []
    cur = store.kv_get(db, "claude_account")
    ranked = claude_rank(rows, spent if isinstance(spent, list) else [], cur if isinstance(cur, str) else None)
    if not ranked:
        return ""
    if ranked[0] != cur:
        with _write(db):
            store.kv_set(db, "claude_account", ranked[0])
    return ranked[0]


def claude_env(base: dict | None = None, account: str | None = None) -> dict:
    """Environment for a Claude child: copy of `base` (default os.environ) without any CLAUDE* or GEMINI* variable, plus
    CLAUDE_CONFIG_DIR of `account` (default: the account claude_pick() chooses). The login lives in that folder
    (`claude auth login`). Raises RuntimeError when no account is available."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not k.upper().startswith(("CLAUDE", "GEMINI_", "GOOGLE_API", "LCH_")) and k.upper() != "GEMINI_API_KEY"}
    if not account:
        db = store.connect()
        try:
            account = claude_pick(db)
        finally:
            db.close()
        if not account:
            raise RuntimeError("no Claude account available (all spent)")
    env["CLAUDE_CONFIG_DIR"] = config.CLAUDE_CONFIG_DIRS[config.CLAUDE_ACCOUNTS.index(account)]
    return env


# ---------- Codex ----------
def codex_homes() -> list:
    """Configured Codex homes that have auth.json, in config order, de-duplicated by normalized path AND by account
    name. The same Codex account can be logged in under two folders; they share one credit balance, so only the first is
    kept. Without this a spent folder could be chosen again and relaunched every loop because codex_switch matches the
    failed run to the already-spent folder by name."""
    seen_path, seen_name, out = set(), set(), []
    for h in config.CODEX_HOMES:
        if not os.path.exists(os.path.join(h, "auth.json")):
            continue
        key = os.path.normcase(os.path.normpath(h))
        name = codex_name(h)
        if key in seen_path or name in seen_name:
            continue
        seen_path.add(key)
        seen_name.add(name)
        out.append(h)
    return out


def _same(a, b) -> bool:
    return bool(a) and bool(b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def codex_name(home: str) -> str:
    """Short account name of a Codex home."""
    if _same(home, os.path.join(config.HOME, ".codex")):
        return DEFAULT_CODEX_HOME_NAME
    return os.path.basename(os.path.normpath(home))


def _codex_spent(db) -> list:
    v = store.kv_get(db, "codex_spent", [])
    return v if isinstance(v, list) else []


def codex_home(db) -> str:
    """kv `codex_home` while it still has auth.json and is not spent, else the first home not in `codex_spent`.
    Returns "" when every home is spent or none exists."""
    homes = codex_homes()
    spent = _codex_spent(db)
    cur = store.kv_get(db, "codex_home")
    if isinstance(cur, str):
        for h in homes:
            if _same(h, cur) and not any(_same(h, s) for s in spent):
                return h
    for h in homes:
        if not any(_same(h, s) for s in spent):
            if not _same(h, cur):
                with _write(db):
                    store.kv_set(db, "codex_home", h)
            return h
    return ""


def codex_switch(db, failed_home: str | None) -> bool:
    """A Codex run failed with a spent/limit marker on `failed_home`. Returns True when a usable home exists now (this
    call switched, or another run already switched); False when every home is spent (caller blocks codex)."""
    homes = codex_homes()
    cur = codex_home(db)
    if failed_home and cur and not _same(failed_home, cur):
        return True  # another run already moved on from the failed home (old runner rule)
    failed = failed_home or cur
    spent = _codex_spent(db)
    if failed and not any(_same(failed, s) for s in spent):
        spent.append(failed)
    with _write(db):
        store.kv_set(db, "codex_spent", spent)
    for h in homes:
        if not any(_same(h, s) for s in spent):
            with _write(db):
                store.kv_set(db, "codex_home", h)
                store.add_error(db, "system", f"Codex account {codex_name(failed) if failed else '?'} spent; "
                                              f"switched to {codex_name(h)}")
            return True
    return False


def limit_transient(agent: str, account: str | None) -> bool:
    """True when the live usage of `account` shows every window under 100 % and the reading is not stale, so a limit
    message in a log is treated as transient. Stale, missing or unreadable -> False
    (a stale reading does not excuse a limit failure). `account` for Codex is a home path or its name."""
    if agent not in ("claude", "codex") or not account:
        return False
    name = codex_name(account) if agent == "codex" and (os.sep in account or "/" in account) else account
    for r in usage(agent):
        if r.get("id") == name:
            pcts = [x.get("percent") for x in r.get("limits") or []]
            return (not r.get("stale") and r.get("status") == "ok" and bool(pcts)
                    and all(isinstance(p, (int, float)) and p < 100 for p in pcts))
    return False


# ---------- blocked flags ----------
def blocked(db) -> dict:
    return {k.removeprefix("blocked."): v for k, v in store.kv_prefix(db, "blocked.").items() if v}


def block(db, agent: str, reason: str, record_error: bool = True):
    """Mark an agent blocked (kv blocked.<agent> = reason) and leave an errors row."""
    if agent not in config.AGENTS:
        raise ValueError(f"unknown agent {agent!r}")
    with _write(db):
        store.kv_set(db, f"blocked.{agent}", reason or "blocked")
        if record_error:
            store.add_error(db, agent, f"{agent} blocked: {reason}")


def unblock(db, agent: str) -> bool:
    """Remove the block, clear the spent lists of that agent (so rotation starts over) and resolve its open errors
    Returns True when a block existed."""
    if agent not in config.AGENTS:
        raise ValueError(f"unknown agent {agent!r}")
    had = store.kv_get(db, f"blocked.{agent}") is not None
    with _write(db):
        store.kv_del(db, f"blocked.{agent}")
        if agent == "codex":
            store.kv_del(db, "codex_spent")
            store.kv_del(db, "codex_home")  # rotation starts over from the first home
        if agent == "claude":
            store.kv_del(db, "claude_spent")
        db.execute("UPDATE errors SET resolved=1 WHERE agent=? AND resolved=0", (agent,))
    return had


# ---------- ep accounts ----------
def main(argv) -> int:
    """`ep accounts`: Codex homes, current choices, blocked flags (one JSON line)."""
    for s in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            s.reconfigure(encoding="utf-8")
    db = store.connect()
    try:
        claude_rows, codex_rows = usage("claude"), usage("codex")
        errs = [e for e in (LAST_USAGE_ERROR,) if e]
        homes = codex_homes()
        spent_c = store.kv_get(db, "claude_spent", []) or []
        cur = store.kv_get(db, "claude_account")
        out = {"ok": True, "claude": summarize(claude_rows), "codex": summarize(codex_rows),
               "codex_home": codex_home_readonly(db), "codex_homes": [codex_name(h) for h in homes],
               "codex_spent": [codex_name(h) for h in _codex_spent(db)],
               "claude_account": cur, "claude_spent": spent_c,
               "claude_next": (claude_rank(claude_rows, spent_c, cur if isinstance(cur, str) else None) or [None])[0],
               "blocked": blocked(db)}
        if errs:
            out["usage_errors"] = errs
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    finally:
        db.close()


def codex_home_readonly(db) -> str:
    """Like codex_home() but never writes (for the CLI report); the home is shown by its short name."""
    homes, spent, cur = codex_homes(), _codex_spent(db), store.kv_get(db, "codex_home")
    for h in ([h for h in homes if _same(h, cur)] if isinstance(cur, str) else []) + homes:
        if not any(_same(h, s) for s in spent):
            return codex_name(h)
    return ""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
