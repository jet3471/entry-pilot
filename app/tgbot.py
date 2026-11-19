"""The AI makes the user's Telegram bot (only after the user agreed in the window).

1. open_web(): Telegram Web opens in the app's browser window; the user logs in there with the QR code.
2. run(report): one AI run (Codex or Claude, whichever is logged in) follows prompts/telegram-bot.md: talks to
   @BotFather, gets the token, sends "/start <code>" to the new bot, logs out of Telegram Web, writes a result file.
3. The app checks the token with Telegram, keeps it encrypted, finds the chat by the start code, and finally clears
   what Telegram Web left in that browser profile (the AI's logout is not trusted alone).
"""
import json
import os
import re
import secrets
import subprocess
import threading
import time

from app import deps, engine, logins, notify, paths, secure, settings, telegram

WEB = "https://web.telegram.org/k/"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RUN_TIMEOUT = 15 * 60
TASKKILL = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")


def open_web() -> dict:
    from app import server
    return server.open_url_in_app_browser(WEB)


DEFAULT_NAME = "Entry Pilot 알림"
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,30}[Bb][Oo][Tt]$")
LAST = {"retry": ""}  # "name" or "username" when the last run stopped at that question (the window asks again)


def agents() -> list:
    """AIs that can do it: Claude and Codex with a logged-in account (Gemini cannot drive the browser)."""
    return [a for a in ("claude", "codex") if settings.load().get("agents", {}).get(a) and logins.logged_in_dirs(a)]


def check_inputs(name: str, username: str) -> str:
    """Error text for a bot name or username BotFather would refuse by its rules ("" when fine or empty)."""
    if name and len(name) > 64:
        return "봇 이름은 64자까지 됩니다."
    if username and not USERNAME_RE.match(username):
        return "봇 아이디는 영문으로 시작하고 영문, 숫자, _ 만 써서 5자에서 32자, 끝이 bot 이어야 합니다."
    return ""


def usernames(username: str) -> list:
    """The user's username alone, or three random ones when the user left it empty."""
    if username:
        return [username]
    tag = secrets.token_hex(3)
    return [f"entrypilot_{tag}_bot", f"ep_alert_{tag}_bot", f"entry_pilot_{secrets.token_hex(4)}_bot"]


def _prompt(code: str, result_file: str, name: str = "", username: str = "") -> str:
    with open(os.path.join(paths.bundle_dir(), "prompts", "telegram-bot.md"), encoding="utf-8") as f:
        text = f.read()
    vals = {"EP_PY": engine.ep_py(), "BOT_NAME": name or DEFAULT_NAME, "START_CODE": code, "RESULT_FILE": result_file,
            "USERNAMES": ", ".join(f"`{u}`" for u in usernames(username))}
    for k, v in vals.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _command(agent: str, task_file: str, workdir: str) -> list:
    engine.configure()
    from ep import config  # noqa: E402  (after configure)
    from ep.agents.claude import DENY_SETTINGS  # noqa: E402
    line = f"Read the task file {task_file} and do exactly what it says."
    cmd = deps.cli_command(agent)
    if agent == "claude":
        return cmd + ["-p", line, "--model", config.MODELS["claude"], "--dangerously-skip-permissions",
                      "--strict-mcp-config", "--disable-slash-commands", "--settings", DENY_SETTINGS]
    return cmd + ["exec", "-m", config.MODELS["codex"], "-c", "model_reasoning_effort=medium",
                  "-c", "features.hooks=false", "--ignore-user-config", "--disable", "apps", "--disable", "plugins",
                  "-c", "project_doc_max_bytes=0", "--skip-git-repo-check", "-s", "danger-full-access", "-C", workdir, line]


def _env(agent: str) -> dict:
    env = engine.engine_env()
    env.update(logins.agent_env(agent, 1))
    for k in list(env):
        if k.upper().startswith(("GEMINI_", "GOOGLE_API", "LCH_")) or k.upper() in ("GEMINI_API_KEY", "TERM"):
            env.pop(k, None)
    first = logins.logged_in_dirs(agent)[0]
    if agent == "claude":
        for k in list(env):
            if k.upper().startswith("CLAUDE"):
                env.pop(k, None)
        env["CLAUDE_CONFIG_DIR"] = first
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    else:
        env["CODEX_HOME"] = first
    return env


_RAW_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
SPACED_PW_RE = re.compile(
    r"(?<![A-Za-z0-9_])[a-z]{4}([ \u00a0]+)[a-z]{4}\1[a-z]{4}\1[a-z]{4}(?![A-Za-z0-9_])"
)
PW_CONTEXT_RE = re.compile(r"(?:app[ \t]+passwords?|apppasswords|앱[ \t]*비밀번호)", re.IGNORECASE)
UNSPACED_PW_RE = re.compile(r"(?<![A-Za-z0-9_])[a-z]{16}(?![A-Za-z0-9_])")


def scrub_text(text: str, secret: str = "") -> str:
    """Scrub Telegram bot tokens, spaced 4x4 Google app passwords, context-tagged unspaced 16-letter app passwords,
    and the exact secret once known."""
    if not text:
        return text
    clean = telegram.TOKEN_RE.sub("[hidden]", text)
    clean = _RAW_TOKEN_RE.sub("[hidden]", clean)
    clean = SPACED_PW_RE.sub("[hidden]", clean)
    if PW_CONTEXT_RE.search(clean):
        clean = "".join(
            UNSPACED_PW_RE.sub("[hidden]", ln) if PW_CONTEXT_RE.search(ln) else ln
            for ln in clean.splitlines(keepends=True)
        )
    if secret:
        clean = clean.replace(secret, "[hidden]")
        if len(secret) == 16 and secret.isalpha() and secret.islower():
            spaced = " ".join(secret[i:i + 4] for i in range(0, 16, 4))
            nbsp = "\u00a0".join(secret[i:i + 4] for i in range(0, 16, 4))
            clean = clean.replace(spaced, "[hidden]").replace(nbsp, "[hidden]")
    return clean


def _scrub(path: str, secret: str = ""):
    """Replace any leaked token or known secret with [hidden] in the run log whether or not the run succeeded."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        clean = scrub_text(text, secret)
        if clean != text:
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(clean)
            secure.restrict_acl(tmp)
            os.replace(tmp, path)
    except OSError:
        pass


def prepare_work_files(subdir: str) -> tuple[str, str, str]:
    """Create ACL-protected work folder and return (work_dir, task_file, random_result_file)."""
    work = paths.sub("data", "work", subdir)
    os.makedirs(work, exist_ok=True)
    secure.restrict_acl(work)
    for legacy in ("result.json", "task.md"):
        lp = os.path.join(work, legacy)
        if os.path.lexists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass
    nonce = secrets.token_hex(16)
    task_file = os.path.join(work, f"task-{nonce}.md")
    result_file = os.path.join(work, f"result-{nonce}.json")
    return work, task_file, result_file


def read_result_file(result_file: str) -> dict:
    try:
        if not os.path.isfile(result_file) or os.path.islink(result_file):
            return {"ok": False, "reason": "no result file"}
        with open(result_file, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except (OSError, ValueError):
        pass
    return {"ok": False, "reason": "no result file"}


def run_scrubbed_process(cmd: list, log_path: str, env: dict, cwd: str, timeout: float,
                         on_tick, popen_fn=None, sleep_fn=None):
    """Run child CLI while streaming its output through scrub_text before writing to disk."""
    popen_fn = popen_fn or subprocess.Popen
    sleep_fn = sleep_fn or time.sleep
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as lf:
        p = popen_fn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, env=env, cwd=cwd, creationflags=NO_WINDOW)
        lock = threading.Lock()

        def _pump():
            if p.stdout is None:
                return
            try:
                for raw in iter(p.stdout.readline, b""):
                    line = scrub_text(raw.decode("utf-8", errors="replace"))
                    with lock:
                        lf.write(line)
                        lf.flush()
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=_pump, daemon=True, name="setup-log-scrub")
        reader.start()
        try:
            t0 = time.time()
            while p.poll() is None:
                if time.time() - t0 > timeout:
                    subprocess.run([TASKKILL, "/T", "/F", "/PID", str(p.pid)],
                                   capture_output=True, creationflags=NO_WINDOW)
                    break
                sleep_fn(3)
                on_tick()
        finally:
            if p.poll() is None:
                subprocess.run([TASKKILL, "/T", "/F", "/PID", str(p.pid)],
                               capture_output=True, creationflags=NO_WINDOW)
            try:
                p.wait(timeout=5)
            except Exception:
                pass
            reader.join(timeout=5)
            if p.stdout is not None:
                try:
                    p.stdout.close()
                except Exception:
                    pass


def run(report, agent: str = "", name: str = "", username: str = ""):
    LAST["retry"] = ""
    name = " ".join((name or "").replace("`", "").split())  # one line, no backticks (it goes into the task file)
    username = (username or "").strip().lstrip("@")
    if agent not in agents():
        raise deps.StepError("고른 AI에 로그인되어 있지 않습니다. AI 로그인 단계를 먼저 끝내세요.")
    bad = check_inputs(name, username)
    if bad:
        LAST["retry"] = "name" if "이름" in bad else "username"
        raise deps.StepError(bad)
    work, task_file, result_file = prepare_work_files("telegram-bot")
    os.makedirs(paths.logs_dir(), exist_ok=True)
    log_path = os.path.join(paths.logs_dir(), "telegram-bot.log")
    res = {"ok": False, "reason": "no result file"}
    try:
        with open(task_file, "w", encoding="utf-8") as f:
            # Start code is issued ONLY after the bot is created and verified, never before the agent run.
            f.write(_prompt("pending", result_file, name, username))
        secure.restrict_acl(task_file)
        env = _env(agent)
        who = {"claude": "Claude", "codex": "Codex"}[agent]
        msg = f"{who}가 텔레그램 웹에서 봇을 만드는 중입니다 (2분에서 5분)"
        report(10, msg)
        pct = [10]

        def _tick():
            pct[0] = min(80, pct[0] + 1)
            report(pct[0], msg)

        run_scrubbed_process(_command(agent, task_file, work), log_path, env, work, RUN_TIMEOUT,
                             _tick, popen_fn=subprocess.Popen, sleep_fn=time.sleep)
        res = read_result_file(result_file)
    finally:
        for f in (result_file, task_file):
            try:
                os.remove(f)
            except OSError:
                pass
        _scrub(log_path, str(res.get("token") or ""))
    raw_tok = str(res.get("token") or "")
    reason = str(res.get("reason") or "")
    if not res.get("ok") and reason in ("name", "username"):
        # still logged in to Telegram Web on purpose: the window asks for another name or username and runs again
        LAST["retry"] = reason
        raise deps.StepError({"name": "BotFather가 이 봇 이름을 받지 않았습니다. 다른 이름을 넣으세요.",
                              "username": ("이 아이디는 이미 있거나 쓸 수 없습니다. 다른 아이디를 넣거나, 비워 두면 무작위로 만듭니다."
                                           if username else "무작위 아이디 세 개가 모두 안 됐습니다. 다시 시도하세요.")}[reason])
    report(85, "텔레그램 웹 기록 지우는 중")
    cleared = clear_web_data()
    clear_warn = ("경고: 브라우저의 텔레그램 웹 기록을 지우지 못했습니다. 열린 브라우저에서 직접 로그아웃하고 "
                  "휴대폰 텔레그램의 설정 > 기기에서 Telegram Web 세션을 직접 종료하세요.") if not cleared else ""
    if not res.get("ok") or not raw_tok:
        why = {"not_logged_in": "텔레그램 웹에 로그인되어 있지 않았습니다. 텔레그램 웹 열기부터 다시 하세요."}.get(reason, "")
        clean_reason = scrub_text(reason, raw_tok)[:80]
        base = why or f"AI가 봇을 만들지 못했습니다 ({clean_reason}). 기록: {log_path}"
        raise deps.StepError(f"{base} {clear_warn}".strip())
    try:
        me = telegram.verify(raw_tok)
    except telegram.TgError as e:
        raise deps.StepError(f"받은 토큰을 텔레그램이 거절했습니다: {scrub_text(str(e), raw_tok)}" + (f" ({clear_warn})" if clear_warn else ""))
    # Issue the start code right before showing the user the bot link (after the bot is created).
    code = telegram.new_start_code()
    issued_at = telegram._START_CODES.get(code) or time.time()
    bot_user = (me.get("username") or str(res.get("username") or "")).strip().lstrip("@")
    notify.clear_map()
    settings.update(
        telegram_token=raw_tok,
        telegram_bot=bot_user,
        telegram_bot_id=str(me.get("id") or ""),
        telegram_chat_id="",
        telegram_start=code,
        telegram_start_at=issued_at,
    )
    report(92, "봇 대화 확인 중")
    r = telegram.find_chat(raw_tok, code)
    connected = bool(r.get("ok"))
    if connected:
        settings.update(telegram_chat_id=r["chat_id"], notify="telegram", telegram_start="", telegram_start_at=0)
        welcome = "Entry Pilot 연결됨. 직접 해야 하는 일과 AI가 묻는 것을 여기로 보냅니다."
        if clear_warn:
            welcome = f"{welcome}\n\n{clear_warn}"
        telegram.send(welcome)
    if not cleared:
        warn = (clear_warn if res.get("logout") else
                "경고: AI의 텔레그램 웹 로그아웃을 확인하지 못했고 브라우저의 텔레그램 웹 기록도 지우지 못했습니다. "
                "열린 브라우저에서 직접 로그아웃하고 휴대폰 텔레그램의 설정 > 기기에서 Telegram Web 세션을 직접 종료하세요.")
        logged_out = False
    elif not res.get("logout"):
        warn = ("다만 AI의 텔레그램 웹 로그아웃을 확인하지 못했습니다. 휴대폰 텔레그램의 설정 > 기기에서 "
                "Telegram Web 세션을 직접 종료하세요.")
        logged_out = False
    else:
        warn = ""
        logged_out = True
    if not connected:
        bot_url = f"https://t.me/{bot_user}?start={code}" if bot_user else ""
        if bot_url and not os.environ.get("EP_TEST_NO_SHELL"):
            try:
                os.startfile(bot_url)
            except OSError:
                pass
        wait_msg = (f"연결 대기: 텔레그램에서 @{bot_user} 봇의 시작(START)을 누르세요."
                    if bot_user else "연결 대기: 텔레그램에서 봇의 시작(START)을 누르세요.")
        if warn:
            wait_msg = f"{wait_msg} {warn}"
        report(95, wait_msg)
        return {"ok": True, "connected": False, "waiting": True, "bot_url": bot_url, "username": bot_user,
                "logged_out": logged_out, "cleared": cleared, "warning": warn}
    done_msg = f"봇 연결됨. {warn}" if warn else "봇 연결됨. 텔레그램 웹에서 로그아웃했습니다."
    report(100, done_msg)
    return {"ok": True, "connected": True, "waiting": False, "logged_out": logged_out, "cleared": cleared, "warning": warn}


def clear_web_data() -> bool:
    """Remove what Telegram Web stored in the app's browser profile (session, cache) through the browser's own
    DevTools command, and close its tabs. True when the browser confirmed it."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=10000)
            ctx = b.contexts[0]
            page = ctx.new_page()
            s = ctx.new_cdp_session(page)
            s.send("Storage.clearDataForOrigin", {"origin": "https://web.telegram.org", "storageTypes": "all"})
            for pg in list(ctx.pages):
                if pg.url.startswith("https://web.telegram.org"):
                    pg.close()
            page.close()
        return True
    except Exception:
        return False
