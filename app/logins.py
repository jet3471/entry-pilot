"""AI logins. The official CLIs do the login and keep their own tokens; the app only points them at its own folders
(CLAUDE_CONFIG_DIR, CODEX_HOME) so the login is made once and reused by every engine run:

  Claude   `claude auth login` in a console window -> browser sign-in -> saved in logins\\claude
  Codex    `codex login` in a console window       -> browser sign-in -> logins\\codex\\auth.json
  Gemini   API key from Google AI Studio (saved encrypted), or the Gemini CLI's own Google sign-in

The app never reads or copies those token files; status is asked from the CLIs (`auth status`, `login status`).
"""
import json
import os
import re
import subprocess
import urllib.request

from app import deps, paths, settings

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)


MAX_ACCOUNTS = 5
QUEUE_AGENTS = ("claude", "codex")  # Gemini uses an API key typed in the window, not a login window


def account_dir(agent: str, i: int = 1) -> str:
    """logins\\claude, logins\\claude-2, ... (account 1 keeps the old folder name)."""
    return paths.sub("logins", agent if i <= 1 else f"{agent}-{i}")


def claude_dir(i: int = 1) -> str:
    return account_dir("claude", i)


def codex_dir(i: int = 1) -> str:
    return account_dir("codex", i)


def account_count(agent: str, s: dict | None = None) -> int:
    s = s or settings.load()
    if not (s.get("agents") or {}).get(agent):
        return 0
    if agent not in QUEUE_AGENTS:
        return 1
    try:
        n = int((s.get("accounts") or {}).get(agent) or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(MAX_ACCOUNTS, n))


def agent_env(agent: str, i: int = 1, base: dict | None = None) -> dict:
    env = deps.runtime_env(base=base, agent=agent)
    for k in list(env):
        if k.upper().startswith(("CLAUDE", "CODEX_", "ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_API", "LCH_")) \
                and k != "CLAUDE_CODE_GIT_BASH_PATH":
            env.pop(k)
    env["CLAUDE_CONFIG_DIR"] = claude_dir(i if agent == "claude" else 1)
    env["CODEX_HOME"] = codex_dir(i if agent == "codex" else 1)
    if agent == "gemini":
        key = settings.get_secret("gemini_api_key")
        if key:
            env["GEMINI_API_KEY"] = key
    return env


def open_login(agent: str, i: int = 1, n: int = 1):
    """Open a console window running the CLI's login for account i of n directly (no cmd.exe or .cmd batch shim)."""
    if agent not in ("claude", "codex"):
        return {"ok": False, "error": f"지원하지 않는 로그인 대상: {agent}"}
    cmd = deps.resolve_cli_cmd(deps.cli_command(agent), agent)
    if not cmd:
        return {"ok": False, "error": "AI 프로그램을 먼저 설치하세요."}
    i, n = int(i), int(n)
    args = {"claude": ["auth", "login"], "codex": ["login"]}[agent]
    if agent == "codex" and not port_free(1455):
        # Codex signs in through a local page on port 1455. Windows reserves that range on many PCs (Hyper-V, WSL):
        # then the device code flow (code shown in the window, typed on a web page) is used instead.
        args = ["login", "--device-auth"]
    full = list(cmd) + list(args)
    if any(not isinstance(p, str) or not p or "\x00" in p for p in full):
        return {"ok": False, "error": "실행 인자가 올바르지 않습니다."}
    os.makedirs(account_dir(agent, i), exist_ok=True)
    work_dir = paths.sub("data", "work")
    os.makedirs(work_dir, exist_ok=True)
    env = agent_env(agent, i)
    return subprocess.Popen(full, env=env, creationflags=NEW_CONSOLE, cwd=work_dir)


def start_login(agent: str, i: int = 1) -> dict:
    p = open_login(agent, i, max(i, account_count(agent)))
    return p if isinstance(p, dict) else {"ok": True}


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEVICE_CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
URL = re.compile(r"https://\S+")


def codex_login(i: int, show) -> int:
    """Codex login without a console window: the CLI's output is read here and shown in the app in Korean.
    Device code flow (port 1455 blocked): the sign-in page opens in the browser and the one-time code is put on the
    clipboard; the app shows the code big. Browser flow: the CLI opens the browser itself. Returns the exit code."""
    cmd = deps.resolve_cli_cmd(deps.cli_command("codex"), "codex")
    if not cmd:
        raise deps.StepError("AI 프로그램을 먼저 설치하세요.")
    os.makedirs(account_dir("codex", i), exist_ok=True)
    work_dir = paths.sub("data", "work")
    os.makedirs(work_dir, exist_ok=True)
    device = not port_free(1455)
    p = subprocess.Popen(cmd + (["login", "--device-auth"] if device else ["login"]), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=agent_env("codex", i),
                         creationflags=NO_WINDOW, cwd=work_dir)
    url = code = ""
    opened = False
    for raw in iter(p.stdout.readline, b""):
        line = ANSI.sub("", raw.decode("utf-8", "replace")).strip()
        m = URL.search(line)
        if m and not url:
            url = m.group(0)
        m = DEVICE_CODE.search(line) if device else None
        if m and not code:
            code = m.group(1)
            copy_text(code)
        if url and (code or not device) and not opened:
            opened = True
            if device:
                open_url(url)  # the browser flow opens the browser by itself
            show({"agent": "codex", "i": i, "device": device, "url": url, "code": code})
    rc = p.wait()
    show(None)
    return rc


def open_url(url: str):
    if url.startswith("https://"):
        os.startfile(url)


def copy_text(text: str):
    try:
        subprocess.run([paths.system32_exe("clip.exe")], input=text.encode("utf-16-le"),
                       creationflags=NO_WINDOW, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass  # the code is also shown big in the app


def login_queue(report, s: dict | None = None, show=lambda d: None):
    """Log in every account the user asked for, one window after another: a window opens, the user signs in, the
    window closes, the next one opens. Accounts already logged in are skipped. A Claude account that turns out to be
    the same e-mail as an earlier one is logged out again and its window reopened (3 tries in all)."""
    s = s or settings.load()
    todo = [(a, i, account_count(a, s)) for a in QUEUE_AGENTS for i in range(1, account_count(a, s) + 1)]
    total = len(todo) or 1
    for k, (agent, i, n) in enumerate(todo):
        name = {"claude": "Claude", "codex": "Codex"}[agent]
        label = f"{name} 계정 {i}/{n}" if n > 1 else name
        for _ in range(3):
            st = status(agent, i)
            if st.get("logged_in"):
                dup = _same_as_earlier(agent, i, st)
                if not dup:
                    break
                logout(agent, i)
                report(100 * k // total, f"{label}: 계정 {dup}과 같은 계정이라 다시 엽니다. 다른 계정으로 로그인하세요.")
            else:
                report(100 * k // total, f"{label} 로그인 중" if agent == "codex" else
                       f"{label} 로그인 창이 열렸습니다. 브라우저에서 로그인하세요.")
            if agent == "codex":
                codex_login(i, show)
                continue
            p = open_login(agent, i, n)
            if isinstance(p, dict):
                raise deps.StepError(p["error"])
            p.wait()
        st = status(agent, i)
        if not st.get("logged_in"):
            raise deps.StepError(f"{label} 로그인이 끝나지 않았습니다. 다시 시도를 누르세요.")
        if _same_as_earlier(agent, i, st):
            raise deps.StepError(f"{label}: 앞 계정과 같은 계정입니다. 다시 시도를 누르고 다른 계정으로 로그인하세요.")
    report(100, "모든 계정 로그인됨")


def _same_as_earlier(agent: str, i: int, st: dict) -> int:
    """Number of an earlier account with the same e-mail (Claude reports it; Codex does not, so 0 there)."""
    who = (st.get("email") or "").lower()
    if agent != "claude" or not who:
        return 0
    for j in range(1, i):
        if (status(agent, j).get("email") or "").lower() == who:
            return j
    return 0


def port_free(port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _run(cmd, env, timeout=40):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          env=env, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)


def status(agent: str, i: int = 1) -> dict:
    cmd = deps.resolve_cli_cmd(deps.cli_command(agent), agent)
    if not cmd:
        return {"installed": False, "logged_in": False}
    try:
        if agent == "claude":
            r = _run(cmd + ["auth", "status", "--json"], agent_env(agent, i))
            try:
                j = json.loads(r.stdout.strip() or "{}")
            except ValueError:
                j = {}
            ok = bool(j.get("loggedIn") or j.get("logged_in"))
            return {"installed": True, "logged_in": ok, "who": j.get("email") or j.get("account") or "",
                    "email": j.get("email") or "",
                    "plan": j.get("subscriptionType") or j.get("authMethod") or ""}
        if agent == "codex":
            r = _run(cmd + ["login", "status"], agent_env(agent, i))
            txt = (r.stdout + r.stderr).strip()
            ok = r.returncode == 0 and "not logged in" not in txt.lower()
            return {"installed": True, "logged_in": ok, "who": txt.splitlines()[0][:80] if ok and txt else ""}
        if agent == "gemini":
            if settings.get_secret("gemini_api_key"):
                return {"installed": True, "logged_in": True, "who": "API 키"}
            creds = os.path.join(os.path.expanduser("~"), ".gemini", "oauth_creds.json")
            return {"installed": True, "logged_in": os.path.isfile(creds), "who": "Google 계정" if os.path.isfile(creds) else ""}
    except subprocess.TimeoutExpired:
        return {"installed": True, "logged_in": False, "error": "응답 없음"}
    except OSError as e:
        return {"installed": True, "logged_in": False, "error": str(e)[:120]}
    return {"installed": False, "logged_in": False}


def accounts_status(agent: str, s: dict | None = None) -> dict:
    """Status of every account of one AI: logged_in when at least one is, all_in when every one is, plus the list."""
    n = account_count(agent, s)
    if agent not in QUEUE_AGENTS or n <= 1:
        st = status(agent, 1)
        st["all_in"] = bool(st.get("logged_in"))
        st["accounts"] = [dict(st, i=1)] if n else []
        return st
    rows = [dict(status(agent, i), i=i) for i in range(1, n + 1)]
    return {"installed": any(r.get("installed") for r in rows), "logged_in": any(r.get("logged_in") for r in rows),
            "all_in": all(r.get("logged_in") for r in rows), "who": rows[0].get("who", ""), "accounts": rows}


def logged_in_dirs(agent: str) -> list:
    """Login folders of the accounts logged in now, account 1 first (what the engine rotates through)."""
    return [account_dir(agent, i) for i in range(1, max(1, account_count(agent)) + 1)
            if status(agent, i).get("logged_in")]


def logout(agent: str, i: int = 1) -> dict:
    cmd = deps.resolve_cli_cmd(deps.cli_command(agent), agent)
    if agent == "gemini":
        settings.set_secret("gemini_api_key", "")
        return {"ok": True}
    if not cmd:
        return {"ok": False}
    args = {"claude": ["auth", "logout"], "codex": ["logout"]}[agent]
    r = _run(cmd + args, agent_env(agent, i))
    return {"ok": r.returncode == 0}


def check_gemini_key(key: str) -> dict:
    """Ask Google whether the key works (lists models). The key goes only to Google."""
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                                     headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"키가 거절됨 (HTTP {e.code})"}
    except Exception as e:
        return {"ok": False, "error": f"연결 실패 ({type(e).__name__})"}
    return {"ok": ok}
