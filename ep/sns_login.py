"""Login helper for the SNS accounts. The user logs in in the entry Chrome (ep.chrome);
the login cookies stay in that profile and this tool only records whether each site is logged in, in sns.json.
Passwords are never written anywhere.

  python -m ep.sns_login instagram|threads|facebook|x|youtube|naver
  python -m ep.sns_login login <site>
      open the login page in a visible window, wait until the login cookie appears (up to 15 minutes), record it in
      sns.json and close that window
  python -m ep.sns_login status [--no-launch]
      check every SNS login cookie again, update sns.json and print one JSON line. With --no-launch and Chrome not
      running, Chrome is not started and the cached sns.json is printed instead

sns.json: <data dir>\\sns.json
  {"instagram": {"logged_in": true, "checked_at": "...", "expires": "..."}, ...}

Before Chrome is moved to the user's screen, the agents are paused with `python ep.py block <agent> --reason "sns login"`
and resumed afterwards with `python ep.py resume <agent>` (failures there are warnings, never blocking).
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    from ep.config import DATA_DIR
except Exception:
    DATA_DIR = os.environ.get("EP_DATA_DIR") or (os.environ.get("EP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EntryPilot")) + r"\data"

from ep import chrome

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EP_PY = os.path.join(PROJECT, "ep.py")
DB = os.path.join(DATA_DIR, "platform.db")
SNS_FILE = os.path.join(DATA_DIR, "sns.json")
KST = timezone(timedelta(hours=9))
SNS = {
    "instagram": {"name": "Instagram", "login": "https://www.instagram.com/accounts/login/", "cookies": [(".instagram.com", "sessionid")]},
    "threads": {"name": "Threads", "login": "https://www.threads.com/login", "cookies": [(".threads.com", "sessionid"), (".threads.net", "sessionid")]},
    "facebook": {"name": "Facebook", "login": "https://www.facebook.com/login/", "cookies": [(".facebook.com", "c_user")]},
    "x": {"name": "X", "login": "https://x.com/i/flow/login", "cookies": [(".x.com", "auth_token"), (".twitter.com", "auth_token")]},
    "youtube": {"name": "YouTube", "login": "https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com/", "cookies": [(".youtube.com", "LOGIN_INFO"), (".google.com", "SID")]},
    "naver": {"name": "Naver", "login": "https://nid.naver.com/nidlogin.login", "cookies": [(".naver.com", "NID_AUT")]},
}
NAVER_SHARED_ID = ""  # when set, only that Naver account is accepted; empty: any Naver account the user logs in with
# Agents paused while Chrome is on the user's screen (restarting Chrome cuts every CDP connection); every agent may use Chrome.
PAUSE_AGENTS = ("claude", "codex", "grok", "gemini")
PAUSE_REASON = "sns login"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RESUME_AFTER = False
_paused_here = set()


def now():
    return datetime.now(KST).isoformat(timespec="seconds")


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr, flush=True)


def load_sns():
    try:
        with open(SNS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sns(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SNS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SNS_FILE)


def cookie_domain_matches(cookie_domain: str, target_domain: str) -> bool:
    """True when `cookie_domain` is an exact host or dot-boundary subdomain match for `target_domain`."""
    want = (target_domain or "").strip().lstrip(".").rstrip(".").lower()
    raw = (cookie_domain or "").strip().lower()
    if not want or not raw:
        return False
    cdom = raw[1:] if raw.startswith(".") else raw
    if not cdom or cdom.startswith(".") or cdom.endswith(".") or ".." in cdom:
        return False
    return cdom == want or cdom.endswith("." + want)


def find_login_cookie(cookies, wanted):
    """Return the login cookie if it exists and has not expired."""
    t = time.time()
    for dom, name in wanted:
        for c in cookies:
            if (c.get("name") == name and cookie_domain_matches(c.get("domain", ""), dom)
                    and (c.get("expires", -1) in (-1, None) or c["expires"] > t)):
                return c
    return None


def record(d, key, cookie):
    d[key] = {"logged_in": bool(cookie), "checked_at": now(),
              "expires": (datetime.fromtimestamp(cookie["expires"], KST).isoformat(timespec="seconds") if cookie and cookie.get("expires", -1) > 0 else None)}


# ---------- pausing agents through the core CLI ----------
def run_ep(*args, timeout=60):
    """Run `python ep.py ...`; returns the parsed JSON line or None (with a warning) on any failure."""
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", EP_PY, *args], capture_output=True, timeout=timeout,
                           cwd=PROJECT, creationflags=NO_WINDOW)
        lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
        j = json.loads(lines[-1]) if lines else None
        if r.returncode != 0 or not isinstance(j, dict) or j.get("ok") is False:
            err = (j.get("error") if isinstance(j, dict) else None) or (r.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
            warn(f"ep.py {' '.join(args[:2])} failed (code {r.returncode}): {str(err)[:160]}")
            return None
        return j
    except Exception as e:
        warn(f"ep.py {' '.join(args[:2])} failed ({type(e).__name__}: {str(e)[:160]})")
        return None


def blocked_flags():
    """{agent: reason} of blocked agents, or None if it cannot be read. From `ep.py status` ("blocked"), else from the
    kv table (blocked.<agent>) of platform.db (read only)."""
    j = run_ep("status")
    b = (j or {}).get("blocked")
    if isinstance(b, dict):
        return {k: str(v) for k, v in b.items() if v}
    if isinstance(b, list):
        return {str(x.get("agent")): str(x.get("reason", "")) for x in b if isinstance(x, dict) and x.get("agent")}
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT key, value FROM kv WHERE key LIKE 'blocked.%'").fetchall()
        finally:
            con.close()
        return {k.split(".", 1)[1]: str(v) for k, v in rows if v}
    except Exception as e:
        warn(f"cannot read blocked flags ({type(e).__name__})")
        return None


def running_jobs():
    """Number of unfinished runs of the paused agents (platform.db runs table, read only), or None if unknown."""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            q = f"SELECT count(*) FROM runs WHERE ended IS NULL AND agent IN ({','.join('?' * len(PAUSE_AGENTS))})"
            return con.execute(q, PAUSE_AGENTS).fetchone()[0]
        finally:
            con.close()
    except Exception as e:
        warn(f"cannot read running jobs from platform.db ({type(e).__name__}: {str(e)[:120]})")
        return None


def runner_pause():
    """Moving Chrome to the user's screen cuts the connections of running agents. Block new work and wait until the
    running jobs end (at most 6 minutes)."""
    flags = blocked_flags() or {}
    for a in PAUSE_AGENTS:
        if flags.get(a) and PAUSE_REASON not in flags[a]:
            continue  # already blocked for another reason; leave it as is (the old tool used setdefault)
        if run_ep("block", a, "--reason", PAUSE_REASON) is not None:
            _paused_here.add(a)
    if running_jobs() is None:
        warn("running jobs are unknown, so not waiting for them")
        return
    print("Waiting for running automated entries to finish (up to 6 minutes)", end="", flush=True)
    deadline = time.time() + 360
    while time.time() < deadline:
        n = running_jobs()
        if not n:
            break
        print(".", end="", flush=True)
        time.sleep(5)
    print()


def runner_resume():
    """After the login, hide Chrome again and resume the agents paused for the login (agents blocked for other reasons stay
    blocked)."""
    print("Hiding Chrome again:", json.dumps(chrome.hide(), ensure_ascii=False))
    flags = blocked_flags()
    if flags is None:
        targets = sorted(_paused_here)
    else:
        targets = [a for a in PAUSE_AGENTS if flags.get(a) and (PAUSE_REASON in flags[a] or "login" in flags[a].lower())]
    for a in targets:
        run_ep("resume", a)
    left = blocked_flags()
    print("Automated entries resume. Still blocked:", sorted(left) if left is not None else "unknown")


def naver_account_ok(ctx):
    """When NAVER_SHARED_ID is set, automation resumes only with that Naver account.
    It is empty by default: the user's own login is accepted. The ID itself is never printed."""
    if not NAVER_SHARED_ID:
        return True, True
    pg = ctx.new_page()
    try:
        pg.goto("https://nid.naver.com/user2/help/myInfoV2?lang=ko_KR", timeout=20000)
        pg.wait_for_timeout(2000)
        m = re.search(r"([A-Za-z0-9_.-]+)@naver\.com", pg.inner_text("body"))
        nid = m.group(1) if m else None
    except Exception as e:
        nid = None
        print("Account ID check failed:", type(e).__name__)
    finally:
        pg.close()
    return nid == NAVER_SHARED_ID, nid is not None


def status(no_launch=False):
    if no_launch and not chrome.alive():
        d = load_sns()
        return {"ok": True, "checked": False, "note": "Chrome not running; cached sns.json", "sns": d}, 0
    r = chrome.ensure()
    if not r["ok"]:
        return {"ok": False, "error": "could not start Chrome", "chrome": r}, 1
    from playwright.sync_api import sync_playwright
    d = load_sns()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(chrome.CDP)
        cookies = b.contexts[0].cookies()
        for key, s in SNS.items():
            record(d, key, find_login_cookie(cookies, s["cookies"]))
    save_sns(d)
    return {"ok": True, "checked": True, "sns": {k: d[k] for k in SNS}}, 0


def login(arg):
    global RESUME_AFTER
    r = chrome.ensure()
    if not r["ok"]:
        print("Could not start Chrome:", json.dumps(r, ensure_ascii=False))
        return 1
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(chrome.CDP)
        ctx = b.contexts[0]
        shown = False
        if arg in SNS and chrome.mode() == "hidden" and not find_login_cookie(ctx.cookies(), SNS[arg]["cookies"]):
            # On the hidden desktop the user cannot see the login window. Restart Chrome on the user's screen first 
            runner_pause()
            shown = True
            r = chrome.show(b)
            if not r["ok"]:
                print("Could not restart Chrome on the user's screen:", json.dumps(r, ensure_ascii=False))
                return 1
            b = pw.chromium.connect_over_cdp(chrome.CDP)
            ctx = b.contexts[0]
        if not shown and arg in SNS and chrome.mode() != "hidden":
            # A previous login attempt ended with Chrome visible and the pause still set: hide and resume on success this time
            flags = blocked_flags() or {}
            if any(PAUSE_REASON in v for v in flags.values()):
                shown = True
        if arg not in SNS:
            print("Unknown SNS:", arg, "known:", ", ".join(SNS))
            return 2
        s = SNS[arg]
        c = find_login_cookie(ctx.cookies(), s["cookies"])
        if c:
            d = load_sns()  # re-read right before saving so changes by other waiting processes are not overwritten
            record(d, arg, c)
            save_sns(d)
            print(f"{s['name']}: already logged in (cookie checked). To switch accounts, log out on that site and run this again.")
            return 0
        cdp = b.new_browser_cdp_session()
        t = cdp.send("Target.createTarget", {"url": s["login"], "newWindow": True})
        w = cdp.send("Browser.getWindowForTarget", {"targetId": t["targetId"]})
        cdp.send("Browser.setWindowBounds", {"windowId": w["windowId"], "bounds": {"left": 80, "top": 80, "width": 1100, "height": 900, "windowState": "normal"}})
        cdp.send("Target.activateTarget", {"targetId": t["targetId"]})
        print(f"Opened the {s['name']} login window. Log in there. Once logged in, this tool saves "
              f"it and closes the window by itself. (Up to 15 minutes; Ctrl+C to stop)")
        deadline = time.time() + 15 * 60
        try:
            while time.time() < deadline:
                time.sleep(2)
                c = find_login_cookie(ctx.cookies(), s["cookies"])
                if c:
                    d = load_sns()  # re-read right before saving (waiting processes would overwrite each other)
                    record(d, arg, c)
                    save_sns(d)
                    print(f"\n{s['name']}: login confirmed and saved ({SNS_FILE}). Cookie expires: {d[arg]['expires'] or 'browser session'}")
                    time.sleep(2)
                    try:
                        cdp.send("Target.closeTarget", {"targetId": t["targetId"]})
                    except Exception:
                        pass
                    ok = True
                    if arg == "naver":
                        ok, read = naver_account_ok(ctx)
                        if ok:
                            print(f"Logged-in Naver account is the expected account {NAVER_SHARED_ID}.")
                        else:
                            print(f"The logged-in Naver account is {'not' if read else 'not confirmed as'} the expected account "
                                  f"{NAVER_SHARED_ID}. Automated entries are not resumed. Log out and log in again with {NAVER_SHARED_ID}.")
                    if shown and ok:
                        b.close()
                        RESUME_AFTER = True  # calling chrome.hide() inside the Playwright connection nests the sync API; main calls it afterwards
                    return 0
                print(".", end="", flush=True)
        except KeyboardInterrupt:
            print("\nStopped. The window stays open.")
            return 1
        print(f"\n{s['name']}: no login cookie within 15 minutes. The window stays open. After logging in, run the status check.")
        return 1


def main(argv):
    global RESUME_AFTER
    RESUME_AFTER = False
    argv = list(argv)
    if argv and argv[0] == "login":
        argv = argv[1:]
    arg = argv[0] if argv else "status"
    if arg == "status":
        res, code = status(no_launch="--no-launch" in argv)
        print(json.dumps(res, ensure_ascii=False), flush=True)
        return code
    code = login(arg)
    if RESUME_AFTER:
        runner_resume()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
