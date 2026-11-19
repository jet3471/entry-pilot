"""EntryPilot.exe entry point.

  EntryPilot.exe                 open the app window (starts the local server once; a second start only opens a window)
  EntryPilot.exe --uninstall     remove the app (Windows "Apps" entry calls this)
  EntryPilot.exe -X utf8 x.py    behaves like python (app/pyshim.py), for the engine and the AI agents
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request


def _open_window(url: str):
    if os.environ.get("EP_TEST_NO_SHELL"):
        return
    from app import deps, paths
    browser = deps.find_browser()
    prof = os.path.join(paths.runtime_dir(), "window-profile")
    if browser:
        extra = []
        port = os.environ.get("EP_WINDOW_CDP_PORT", "")
        if port.isdigit():  # tests and recorded demos drive the window; never set for normal use
            extra = [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"]
        subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={prof}", "--window-size=1240,860",
                          "--no-first-run", "--no-default-browser-check", "--disable-features=Translate", *extra],
                         close_fds=True)
    else:
        import webbrowser
        webbrowser.open(url)


def run_file_path(app_folder: str | None = None) -> str:
    """Store per-installation run-<hash8>.json under %LOCALAPPDATA%\\EntryPilot (portable build too)."""
    from app import paths
    folder = app_folder if app_folder is not None else paths.app_dir()
    norm = os.path.normcase(os.path.normpath(os.path.realpath(folder)))
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, paths.APP_NAME, f"run-{digest}.json")


def write_run_file(run_file: str, port: int, open_code: str):
    from app import secure
    os.makedirs(os.path.dirname(run_file), exist_ok=True)
    tmp = f"{run_file}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("")
        secure.restrict_acl(tmp)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"port": port, "pid": os.getpid(), "open_code": open_code}, f)
        os.replace(tmp, run_file)
        secure.restrict_acl(run_file)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _request_open_window(info: dict) -> bool:
    port = info.get("port")
    open_code = info.get("open_code")
    if not isinstance(port, int) or isinstance(port, bool) or not isinstance(open_code, str) or not open_code:
        return False
    try:
        data = json.dumps({"open_code": open_code}).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/open-window", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200 and bool(json.load(r).get("ok"))
    except Exception:
        return False


def gui():
    from app import paths
    paths.ensure_dirs()
    run_file = run_file_path()
    try:
        with open(run_file, encoding="utf-8") as f:
            info = json.load(f)
        if isinstance(info, dict) and _request_open_window(info):
            return 0
    except (OSError, ValueError, KeyError):
        pass

    from app import engine, server
    srv = server.serve()
    port = srv.server_address[1]
    write_run_file(run_file, port, server.OPEN_CODE)
    server.log(f"app {paths.VERSION} started on port {port}")
    server.start_background()
    server.open_window(port)
    # Live while the window is open (heartbeat every 5 s) or the engine runs (Telegram buttons must keep working).
    while True:
        time.sleep(5)
        idle = time.time() - server.LAST_BEAT[0]
        if idle > 45 and not engine.running() and not engine.contest_running():
            server.log("window closed and engine stopped: exit")
            break
    try:
        os.remove(run_file)
    except OSError:
        pass
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    from app import pyshim
    if os.path.basename(sys.executable).lower() == "python.exe" and getattr(sys, "frozen", False):
        return pyshim.run(argv)  # the console twin is only ever python
    if pyshim.looks_like_python_call(argv):
        return pyshim.run(argv)
    if argv[:1] == ["--uninstall"]:
        from app import installer, paths
        if paths.portable():
            return 0  # a portable copy is removed by deleting its folder
        if installer.confirm_uninstall():
            installer.uninstall()
            time.sleep(2)
        return 0
    if argv[:1] == ["--version"]:
        from app import paths
        print(paths.VERSION)
        return 0
    return gui()


if __name__ == "__main__":
    sys.exit(main())
