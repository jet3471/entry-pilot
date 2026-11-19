"""The AI makes the user's Google app password (only after the user agreed in the window). Gmail only.

1. open_page(): Google's app password page opens in the app's browser window; the user logs in there (password and
   2-step prompts are done by the user, never by the AI).
2. run(report): one AI run (Codex or Claude, whichever is logged in) follows prompts/email-app-password.md: creates
   an app password named "Entry Pilot" on that page only and writes it into a result file.
3. The app reads the result file, deletes it, removes the password from the run log, sends the test mail with it and
   keeps it encrypted (DPAPI) like a password typed by hand.
"""
import json
import os
import re
import subprocess
import time

from app import deps, engine, logins, mailer, paths, secure, settings, tgbot

PAGE = "https://myaccount.google.com/apppasswords"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RUN_TIMEOUT = 10 * 60
LAST = {"need": ""}  # "twosv" when the last run stopped because 2-Step Verification is off (the window shows a button)


def open_page() -> dict:
    from app import server
    return server.open_url_in_app_browser(PAGE)


def agents() -> list:
    return tgbot.agents()


def _prompt(result_file: str) -> str:
    with open(os.path.join(paths.bundle_dir(), "prompts", "email-app-password.md"), encoding="utf-8") as f:
        text = f.read()
    for k, v in {"EP_PY": engine.ep_py(), "RESULT_FILE": result_file}.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _command(agent: str, task_file: str, workdir: str) -> list:
    return tgbot._command(agent, task_file, workdir)


def _env(agent: str) -> dict:
    return tgbot._env(agent)


def close_google_tabs() -> bool:
    """Close the app password tab(s) in the app's browser. The Google login itself stays (the user may want it)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=10000)
            for pg in list(b.contexts[0].pages):
                if pg.url.startswith("https://myaccount.google.com/apppasswords"):
                    pg.close()
        return True
    except Exception:
        return False


LOG_PW_RE = tgbot.SPACED_PW_RE


def _scrub(path: str, secret: str = ""):
    """Replace the spaced 4x4 Google app password form and the actual secret (once known) with [hidden]."""
    tgbot._scrub(path, secret)


def run(report, agent: str = "", address: str = ""):
    LAST["need"] = ""
    address = (address or "").strip()
    if mailer.provider(address) != "google":
        raise deps.StepError("AI가 만들기는 Gmail만 됩니다. 직접 만들기를 쓰세요.")
    if agent not in agents():
        raise deps.StepError("고른 AI에 로그인되어 있지 않습니다. AI 로그인 단계를 먼저 끝내세요.")
    work, task_file, result_file = tgbot.prepare_work_files("email-app-password")
    os.makedirs(paths.logs_dir(), exist_ok=True)
    log_path = os.path.join(paths.logs_dir(), "email-app-password.log")
    res = {"ok": False, "reason": "no result file"}
    try:
        with open(task_file, "w", encoding="utf-8") as f:
            f.write(_prompt(result_file))
        secure.restrict_acl(task_file)
        env = _env(agent)
        who = {"claude": "Claude", "codex": "Codex"}[agent]
        msg = f"{who}가 Google 앱 비밀번호 화면에서 만드는 중입니다 (1분에서 3분)"
        report(10, msg)
        pct = [10]

        def _tick():
            pct[0] = min(80, pct[0] + 2)
            report(pct[0], msg)

        tgbot.run_scrubbed_process(_command(agent, task_file, work), log_path, env, work, RUN_TIMEOUT,
                                   _tick, popen_fn=subprocess.Popen, sleep_fn=time.sleep)
        res = tgbot.read_result_file(result_file)
    finally:
        for f in (result_file, task_file):
            try:
                os.remove(f)
            except OSError:
                pass
        _scrub(log_path, mailer.app_password_in(str(res.get("password") or "")))
    known_pw = mailer.app_password_in(str(res.get("password") or ""))
    pw = known_pw if res.get("ok") else ""
    report(85, "Google 앱 비밀번호 화면 닫는 중")
    close_google_tabs()
    reason = str(res.get("reason") or "")
    if not pw:
        if reason == "two_step_off":
            LAST["need"] = "twosv"
            raise deps.StepError("Google 2단계 인증이 꺼져 있어 앱 비밀번호를 만들 수 없습니다. 2단계 인증을 켠 뒤 다시 시도하세요.")
        if reason == "not_logged_in":
            raise deps.StepError("Google에 로그인되어 있지 않았습니다. 열린 창에서 로그인한 뒤 다시 시도하세요.")
        clean_reason = tgbot.scrub_text(reason, known_pw)[:80]
        raise deps.StepError(f"AI가 앱 비밀번호를 만들지 못했습니다 ({clean_reason}). 기록: {log_path}")
    report(92, "확인 메일 보내는 중")
    r = mailer.connect(address, pw)
    if not r.get("ok"):
        raise deps.StepError(tgbot.scrub_text(r.get("error") or "확인 메일을 보내지 못했습니다.", pw))
    settings.update(notify="email")
    report(100, "이메일 연결됨. 확인 메일을 보냈습니다.")
