"""Start `ep run` on the hidden desktop `entrypilot-events` (work on a virtual desktop so the
user's keyboard and mouse are never taken).

  python -X utf8 ep\\runner_hidden.py run            arguments are passed on to `ep.py` unchanged (default: run)
  python -X utf8 ep\\runner_hidden.py run --once

Outer process: starts deskrun.exe entrypilot-events <python> -X utf8 <this file> <args> detached and prints
{"ok": true, "deskrun_pid": N}. Inner process (env EP_HIDDEN=1, running inside the hidden desktop): runs
`python -X utf8 ep.py <args>` with cwd = the repo, stdin DEVNULL, stdout/stderr appended to <data>\\logs\\scheduler.log,
and waits for it. Every child of the scheduler (agents, Chrome) inherits the hidden desktop.
"""
import json
import os
import subprocess
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from ep import config  # noqa: E402

DESKRUN = (os.environ.get("EP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EntryPilot")) + r"\runtime\deskrun.exe"
DESK = "entrypilot-events"
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x00000008, 0x00000200


def inner(args) -> int:
    os.makedirs(config.logs_dir(), exist_ok=True)
    log_file = os.path.join(config.logs_dir(), "scheduler.log")
    with open(log_file, "a", encoding="utf-8") as log:
        return subprocess.call([sys.executable, "-X", "utf8", os.path.join(PROJECT, "ep.py"), *args], cwd=PROJECT,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def outer(args) -> int:
    if not os.path.exists(DESKRUN):
        print(json.dumps({"ok": False, "error": f"deskrun.exe not found: {DESKRUN}"}))
        return 1
    env = dict(os.environ, EP_HIDDEN="1")
    p = subprocess.Popen([DESKRUN, DESK, sys.executable, "-X", "utf8", f'"{os.path.abspath(__file__)}"', *args],
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps({"ok": True, "deskrun_pid": p.pid}))
    return 0


def main(argv) -> int:
    args = list(argv) or ["run"]
    if os.environ.get("EP_HIDDEN") == "1":
        return inner(args)
    return outer(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
