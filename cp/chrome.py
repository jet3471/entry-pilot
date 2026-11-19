"""Entry Chrome of the contest engine with remote debugging (CDP 9223, own profile in the contest data folder; the
event engine uses 9222). One shared profile is used by submissions and site logins.

  python -m cp.chrome ensure        if 9223 does not answer, start Chrome on the hidden desktop (entrypilot-contests); otherwise leave it
  python -m cp.chrome window URL    open URL in a new visible window at (80,80) (for the user's login); if Chrome runs on the
                                    hidden desktop it is restarted on the user's screen first
  python -m cp.chrome show          restart Chrome on the user's screen (only when the user must log in)
  python -m cp.chrome hide          close Chrome and start it again on the hidden desktop (after the user's login)
  python -m cp.chrome status        running or not, mode, page list
  python -m cp.chrome tabs          list open CDP targets (targetId, type, url, title)
  python -m cp.chrome close-stale --minutes N [--keep-url U]
                                    close page targets first seen more than N minutes ago; at least one tab stays open;
                                    tabs whose URL contains U are never closed. First-seen times: chrome-tabs-seen.json

Work happens on a virtual desktop so the user's keyboard and mouse are never taken. Chrome is
therefore started by deskrun.exe (the app's runtime folder) on a separate desktop (WinSta0\\entrypilot-contests, never
switched to). Windows on that desktop do not appear on the user's screen and cannot receive input. Without deskrun.exe
the window starts off screen.

Verified facts: Chrome ignores PowerShell -WindowStyle Hidden and STARTUPINFO SW_HIDE and shows its
window. Started with --window-position=-10000,100 it sits off screen, and together with
--disable-backgrounding-occluded-windows requestAnimationFrame keeps running off screen or minimized, so Playwright clicks
do not stall. A window created by CDP Target.createTarget(newWindow) appears at the same (off-screen) place, so it is moved
on screen with Browser.setWindowBounds.
Every command prints one JSON line.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

try:
    from cp.config import DATA_DIR
except Exception:
    DATA_DIR = os.environ.get("CP_DATA_DIR") or (os.environ.get("CP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EntryPilot")) + r"\data\contest"

PROFILE = os.path.join(DATA_DIR, "chrome-profile")
ALLOWED_BROWSERS = ("chrome.exe", "msedge.exe")

try:
    from cp.browser_check import candidate_browser_paths, valid_browser
except ImportError:
    from browser_check import candidate_browser_paths, valid_browser


def _find_chrome():
    """Chrome from the app settings (CP_CHROME), else the usual install places, else Edge (also Chromium)."""
    for c in candidate_browser_paths(os.environ.get("CP_CHROME") or ""):
        if c and valid_browser(c):
            return c
    return r"C:\Program Files\Google\Chrome\Application\chrome.exe"


CHROME = _find_chrome()
_IMPORT_CHROME = CHROME
CDP = "http://127.0.0.1:9223"
FLAGS = ["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9223", f"--user-data-dir={PROFILE}",
         "--no-first-run", "--no-default-browser-check", "--window-position=-10000,100", "--window-size=1200,900",
         "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
         "--mute-audio", "--hide-crash-restore-bubble", "--disable-features=Translate", "about:blank"]
DESK = "entrypilot-contests"
DESKRUN = (os.environ.get("CP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EntryPilot")) + r"\runtime\deskrun.exe"
MODE_FILE = os.path.join(DATA_DIR, "chrome-mode.txt")  # whether Chrome currently runs "hidden" or "visible"
TABS_SEEN = os.path.join(DATA_DIR, "chrome-tabs-seen.json")  # targetId -> epoch seconds first seen by close-stale


class _Exit(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False), flush=True)
    raise _Exit(code)


def alive():
    try:
        with urllib.request.urlopen(CDP + "/json/version", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def mode():
    try:
        return open(MODE_FILE, encoding="utf-8").read().strip()
    except OSError:
        return "visible"


def launch(hidden=True):
    env_ch = os.environ.get("CP_CHROME") or ""
    if env_ch and not valid_browser(env_ch):
        return {"ok": False, "error": "invalid browser executable: must be existing chrome.exe or msedge.exe"}
    exe = CHROME if CHROME != _IMPORT_CHROME else (env_ch or _find_chrome())
    # Re-verify right before launch (re-checks Authenticode if the cached entry is older than 10 minutes).
    if not valid_browser(exe):
        return {"ok": False, "error": "invalid browser executable: must be existing chrome.exe or msedge.exe"}
    exe = os.path.realpath(exe)
    os.makedirs(PROFILE, exist_ok=True)
    # Detached so Chrome outlives its parent (a Codex run etc.). deskrun holds the desktop until Chrome exits.
    if hidden and not os.path.isfile(DESKRUN):
        hidden = False  # no hidden-desktop helper: the window still starts off screen (--window-position)
    if hidden:
        cmd = [DESKRUN, DESK, exe, *[f'"{f}"' if " " in f else f for f in FLAGS]]
    else:
        cmd = [exe, *FLAGS]
    p = subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        v = alive()
        if v:
            with open(MODE_FILE, "w", encoding="utf-8") as mf:
                mf.write("hidden" if hidden else "visible")
            return {"ok": True, "started": True, "pid": p.pid, "mode": "hidden" if hidden else "visible", "browser": v.get("Browser")}
        time.sleep(0.5)
    return {"ok": False, "error": "Chrome did not answer on 9223 within 30 seconds", "pid": p.pid}


def close(browser=None):
    """Close with CDP Browser.close (cookies and logins stay in the profile). Returns a failure if not closed within 20 s.
    If a Playwright browser is already connected, pass it in (sync_playwright cannot be nested)."""
    if not alive():
        return {"ok": True, "closed": False}

    def send(b):
        try:
            b.new_browser_cdp_session().send("Browser.close")
        except Exception:
            pass  # the connection may drop while closing
    if browser is not None:
        send(browser)
    else:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            send(pw.chromium.connect_over_cdp(CDP))
    for _ in range(40):
        if not alive():
            time.sleep(2)  # time for the profile lock to be released
            return {"ok": True, "closed": True}
        time.sleep(0.5)
    return {"ok": False, "error": "Chrome did not close within 20 seconds"}


def _powershell_exe() -> str:
    return os.path.join(os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows",
                        "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def profile_running() -> bool:
    """A chrome.exe or msedge.exe whose command line uses this profile is running (checked without CDP)."""
    try:
        out = subprocess.run([_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" | "
                              "ForEach-Object { $_.CommandLine }"],
                             capture_output=True, text=True, timeout=20, encoding="mbcs", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return False
    return PROFILE.lower() in (out or "").lower()


def ensure():
    v = alive()
    if v:
        return {"ok": True, "started": False, "mode": mode(), "browser": v.get("Browser")}
    if profile_running():
        # a Codex run whose sandbox could not reach 127.0.0.1:9223 started a second Chrome on this profile;
        # Chrome restarted and the Naver login (session cookies) was lost. A running profile is never started again.
        return {"ok": False, "error": "Chrome with the entry profile is running but CDP 127.0.0.1:9223 does not answer "
                                      "from here (sandboxed run?); not starting a second Chrome"}
    return launch(hidden=True)


def hide():
    if alive() and mode() == "hidden":
        return {"ok": True, "started": False, "mode": "hidden"}
    c = close()
    return launch(hidden=True) if c["ok"] else c


def show(browser=None):
    """Only when the user must log in. Closes the hidden-desktop Chrome and starts it on the user's screen
    (connections of running automated work are cut)."""
    if alive() and mode() != "hidden":
        return {"ok": True, "started": False, "mode": "visible"}
    c = close(browser)
    return launch(hidden=False) if c["ok"] else c


def window(url):
    r = show()
    if not r["ok"]:
        return r
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(CDP)
        cdp = b.new_browser_cdp_session()
        t = cdp.send("Target.createTarget", {"url": url, "newWindow": True})
        w = cdp.send("Browser.getWindowForTarget", {"targetId": t["targetId"]})
        cdp.send("Browser.setWindowBounds", {"windowId": w["windowId"],
                                             "bounds": {"left": 80, "top": 80, "width": 1100, "height": 900, "windowState": "normal"}})
        cdp.send("Target.activateTarget", {"targetId": t["targetId"]})
        return {"ok": True, "targetId": t["targetId"], "windowId": w["windowId"]}


def status():
    v = alive()
    if not v:
        return {"ok": True, "running": False}
    try:
        with urllib.request.urlopen(CDP + "/json/list", timeout=2) as r:
            pages = [{"id": p["id"], "url": p.get("url", "")[:120], "title": p.get("title", "")[:60]} for p in json.load(r) if p.get("type") == "page"]
    except Exception as e:
        pages = str(e)
    return {"ok": True, "running": True, "mode": mode(), "browser": v.get("Browser"), "pages": pages}


def _targets(timeout=10):
    with urllib.request.urlopen(CDP + "/json/list", timeout=timeout) as r:
        return json.load(r)


def tabs():
    if not alive():
        return {"ok": True, "running": False, "tabs": []}
    try:
        ts = _targets()
    except Exception as e:
        return {"ok": False, "running": True, "error": f"CDP target list failed: {str(e)[:200]}"}
    return {"ok": True, "running": True, "count": len(ts),
            "tabs": [{"targetId": t.get("id"), "type": t.get("type"), "url": t.get("url", "")[:200], "title": t.get("title", "")[:80]} for t in ts]}


def _load_seen():
    try:
        d = json.load(open(TABS_SEEN, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_seen(d):
    os.makedirs(os.path.dirname(TABS_SEEN), exist_ok=True)
    tmp = TABS_SEEN + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, TABS_SEEN)


def close_stale(minutes, keep_url=None):
    """Tab sweep (TAB_MAX_MIN 25): tabs pile up in the entry Chrome
    and CDP stalls (11-01 07:4x: 34 tabs, repeated CDP timeouts), so page targets first seen more than `minutes` ago are
    closed. At least one tab is kept. keep_url: tabs whose URL contains this text are never closed (assumption)."""
    if not alive():
        return {"ok": True, "running": False, "closed": 0}
    try:
        pages = [x for x in _targets() if x.get("type") == "page"]
    except Exception as e:
        return {"ok": False, "running": True, "error": f"tab sweep error: {str(e)[:200]}"}
    seen = _load_seen()
    t = time.time()
    ids = [x["id"] for x in pages]
    for i in ids:
        seen.setdefault(i, t)
    for k in [k for k in seen if k not in ids]:
        seen.pop(k)
    keep = {x["id"] for x in pages if keep_url and keep_url in (x.get("url") or "")}
    old = [i for i in ids if i not in keep and t - seen[i] > minutes * 60]
    if len(old) >= len(ids):
        old = old[1:]
    closed, errors = [], []
    for i in old:
        try:
            urllib.request.urlopen(f"{CDP}/json/close/{i}", timeout=10).read()
            seen.pop(i, None)
            closed.append(i)
        except Exception as e:
            errors.append({"targetId": i, "error": str(e)[:160]})
    _save_seen(seen)
    r = {"ok": not errors, "running": True, "open": len(ids), "closed": len(closed), "closed_ids": closed}
    if errors:
        r["errors"] = errors
    return r


def _main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "ensure":
        r = ensure()
    elif cmd == "window" and len(argv) > 1:
        r = window(argv[1])
    elif cmd == "hide":
        r = hide()
    elif cmd == "show":
        r = show()
    elif cmd == "tabs":
        r = tabs()
    elif cmd == "close-stale":
        import argparse
        ap = argparse.ArgumentParser(prog="cp chrome close-stale")
        ap.add_argument("--minutes", type=float, required=True)
        ap.add_argument("--keep-url")
        a = ap.parse_args(argv[1:])
        r = close_stale(a.minutes, a.keep_url)
    else:
        r = status()  # unknown commands print status, as the old tool did
    out(r, 0 if r.get("ok") else 1)


def main(argv):
    try:
        _main(list(argv))
    except _Exit as e:
        return e.code
    except SystemExit as e:  # argparse errors
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
