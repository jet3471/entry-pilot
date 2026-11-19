"""The installed app exe also answers like `python`, because the engine and the AI agents call Python:

    python -X utf8 <app>\\ep.py run           the engine's own child processes (sys.executable is the exe)
    python -m ep.chrome ensure                 engine tools called from agent prompts
    python -X utf8 - < script.py               Playwright scripts an agent writes and pipes in
    python -c "code"                           one-liners
    python script.py args

A copy of the exe named python.exe sits in <app>\\runtime\\bin, which is put first on PATH for the engine and its agents
(app/engine.py), so the agents need no Python installation. Unknown interpreter options are ignored.
"""
import os
import runpy
import sys

# Options of the real interpreter that take a value and are skipped here.
_VALUE_OPTS = {"-X", "-W", "--check-hash-based-pycs"}
_FLAG_OPTS = {"-u", "-B", "-I", "-E", "-s", "-S", "-O", "-OO", "-q", "-b", "-bb", "-d", "-v", "-P", "-R"}


def looks_like_python_call(argv: list) -> bool:
    if not argv:
        return False
    a = argv[0]
    return a in _VALUE_OPTS or a in _FLAG_OPTS or a in ("-m", "-c", "-", "-V", "--version") or a.lower().endswith(".py")


def _setup_streams():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run(argv: list) -> int:
    _setup_streams()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_OPTS:
            i += 2
        elif a.startswith("-X") or a.startswith("-W"):
            i += 1
        elif a in _FLAG_OPTS:
            i += 1
        else:
            break
    rest = argv[i:]
    if not rest:
        print("Entry Pilot python shim: interactive mode is not available", file=sys.stderr)
        return 2
    head = rest[0]
    if head in ("-V", "--version"):
        print("Python %d.%d.%d (Entry Pilot)" % sys.version_info[:3])
        return 0
    try:
        if head == "-m":
            if len(rest) < 2:
                print("-m needs a module name", file=sys.stderr)
                return 2
            sys.argv = [rest[1]] + rest[2:]
            runpy.run_module(rest[1], run_name="__main__", alter_sys=True)
        elif head == "-c":
            sys.argv = ["-c"] + rest[2:]
            exec(compile(rest[1] if len(rest) > 1 else "", "<string>", "exec"), {"__name__": "__main__"})
        elif head == "-":
            sys.argv = ["-"] + rest[1:]
            src = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            exec(compile(src, "<stdin>", "exec"), {"__name__": "__main__"})
        else:
            path = head
            sys.argv = [path] + rest[1:]
            if os.path.basename(path).lower() == "ep.py":
                import ep.cli  # the bundled engine, whatever folder the caller named
                return int(ep.cli.main(rest[1:]) or 0)
            if os.path.basename(path).lower() == "cp.py":
                import cp.cli  # the bundled contest engine
                return int(cp.cli.main(rest[1:]) or 0)
            d = os.path.dirname(os.path.abspath(path))
            if d not in sys.path:
                sys.path.insert(0, d)
            runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0
