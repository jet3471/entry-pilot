"""Security tests for items 4-5 (no network, no Chrome, no AI CLI):
- Item 4: mailer allows only gmail.com / googlemail.com on smtp.gmail.com; send() refuses any other sender address.
- Item 5: tgbot / mailbot Claude runs enforce DENY_SETTINGS + CLAUDE_CODE_DISABLE_AUTO_MEMORY=1, scrub secrets from
  logs in finally whether or not the run succeeded, and warn visibly if clear_web_data() fails."""
import json
import os
import smtplib
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="ep-sec-l2-")
os.environ.setdefault("EP_APP_DIR", _TMP)

from app import deps, logins, mailbot, mailer, paths, settings, telegram, tgbot  # noqa: E402
from ep.agents import claude as claude_agent  # noqa: E402


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class MailerGmailOnly(unittest.TestCase):
    def tearDown(self):
        settings.update(email_address="", email_to="", notify="")
        settings.set_secret("email_password", "")

    def test_host_for_allows_only_gmail_and_googlemail(self):
        self.assertEqual(mailer.host_for("user@gmail.com"), ("smtp.gmail.com", 465, True))
        self.assertEqual(mailer.host_for("user@googlemail.com"), ("smtp.gmail.com", 465, True))
        self.assertEqual(mailer.host_for("  User.Name+1@GoogleMail.COM "), ("smtp.gmail.com", 465, True))
        for bad in (
            "",
            "gmail.com",
            "@gmail.com",
            "a@b@gmail.com",
            "user@naver.com",
            "user@daum.net",
            "user@hotmail.com",
            "user@evil.example",
            "user@gmail.com.evil.example",
            "user@sub.gmail.com",
            "user name@gmail.com",
            "user@gmail.com\r\nbcc:evil@example.com",
        ):
            with self.assertRaises(mailer.MailError, msg=bad):
                mailer.host_for(bad)

    def test_send_refuses_non_gmail_before_connecting(self):
        saved_connect = mailer._connect
        saved_ssl = smtplib.SMTP_SSL
        mailer._connect = lambda *a, **k: self.fail("_connect must not be called for non-Gmail")
        smtplib.SMTP_SSL = lambda *a, **k: self.fail("SMTP_SSL must not be called for non-Gmail")
        try:
            for bad in ("user@naver.com", "user@hotmail.com", "user@evil.example", "a@evil.example@gmail.com"):
                with self.assertRaises(mailer.MailError, msg=bad):
                    mailer.send("제목", "본문", address=bad, password="abcdefghijklmnop")
            settings.update(email_address="victim@evil.example")
            settings.set_secret("email_password", "abcdefghijklmnop")
            with self.assertRaises(mailer.MailError):
                mailer.send("제목", "본문")
        finally:
            mailer._connect = saved_connect
            smtplib.SMTP_SSL = saved_ssl

    def test_send_connects_only_to_smtp_gmail_com(self):
        calls = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None, context=None):
                calls.append(("connect", host, port))

            def login(self, addr, pw):
                calls.append(("login", addr, pw))

            def send_message(self, msg):
                calls.append(("send", msg["From"], msg["To"]))

            def quit(self):
                pass

        saved_ssl = smtplib.SMTP_SSL
        smtplib.SMTP_SSL = FakeSMTP
        try:
            settings.update(email_to="inbox@naver.com")
            mailer.send("알림", "내용", address="sender@googlemail.com", password="abcdefghijklmnop")
        finally:
            smtplib.SMTP_SSL = saved_ssl
        self.assertEqual(calls[0], ("connect", "smtp.gmail.com", 465))
        self.assertEqual(calls[1], ("login", "sender@googlemail.com", "abcdefghijklmnop"))
        self.assertEqual(calls[2], ("send", "sender@googlemail.com", "inbox@naver.com"))


class SetupAgentRestrictions(unittest.TestCase):
    def test_claude_command_has_deny_settings_in_tgbot_and_mailbot(self):
        saved_cli = deps.cli_command
        deps.cli_command = lambda a: ["claude.exe"] if a == "claude" else ["codex.exe"]
        try:
            for mod in (tgbot, mailbot):
                cmd = mod._command("claude", r"C:\work\task.md", r"C:\work")
                self.assertIn("--settings", cmd)
                raw = cmd[cmd.index("--settings") + 1]
                self.assertEqual(raw, claude_agent.DENY_SETTINGS)
                deny = json.loads(raw)["permissions"]["deny"]
                for rule in ("Read(**/secrets*.env)", "Read(**/profile.json)", "Bash(*secrets*.env*)"):
                    self.assertIn(rule, deny)
        finally:
            deps.cli_command = saved_cli

    def test_claude_env_disables_auto_memory_in_tgbot_and_mailbot(self):
        saved_dirs = logins.logged_in_dirs
        logins.logged_in_dirs = lambda a: [logins.account_dir(a, 1)]
        try:
            for mod in (tgbot, mailbot):
                env = mod._env("claude")
                self.assertEqual(env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")
                self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), logins.account_dir("claude", 1))
        finally:
            logins.logged_in_dirs = saved_dirs


class LogScrubbingAndLogoutWarning(unittest.TestCase):
    def setUp(self):
        self.tok = "1234567890:AAH" + "z" * 32
        self.pw_raw = "qrstuvwxyzabcdef"
        self.pw_spaced = "qrst uvwx yzab cdef"

    def tearDown(self):
        settings.set_secret("telegram_token", "")
        settings.set_secret("email_password", "")
        settings.update(telegram_chat_id="", telegram_bot="", telegram_start="", notify="", email_address="")

    def _run_tgbot(self, payload, log_line, clear_ok=True, report_cb=None):
        seen_envs = []
        orig_popen = tgbot.subprocess.Popen

        def fake_command(agent, task_file, workdir):
            code = (
                "import json, re, sys; "
                "print(sys.argv[3], flush=True); "
                "t = open(sys.argv[1], encoding='utf-8').read(); "
                "f = re.search(r'result file `([^`]+)`', t).group(1); "
                "p = sys.argv[2]; "
                "p and json.dump(json.loads(p), open(f, 'w', encoding='utf-8'))"
            )
            return [sys.executable, "-c", code, task_file, "" if payload is None else json.dumps(payload), log_line]

        def spy_popen(*args, **kwargs):
            if kwargs.get("env") is not None:
                seen_envs.append(dict(kwargs["env"]))
            return orig_popen(*args, **kwargs)

        saved = (
            tgbot.agents, tgbot._command, tgbot.clear_web_data, logins.logged_in_dirs,
            telegram.verify, telegram.find_chat, telegram.send, tgbot.subprocess.Popen,
        )
        sent = []
        reports = []
        tgbot.agents = lambda: ["claude"]
        tgbot._command = fake_command
        tgbot.clear_web_data = lambda: clear_ok
        logins.logged_in_dirs = lambda a: [logins.account_dir(a, 1)]
        telegram.verify = lambda t: {"ok": True, "username": "sec_test_bot"}
        telegram.find_chat = lambda t, code="": {"ok": True, "chat_id": "99"}
        telegram.send = lambda text, *a, **k: sent.append(text)
        tgbot.subprocess.Popen = spy_popen
        try:
            out = tgbot.run(report_cb or (lambda pct, text: reports.append((pct, text))), "claude", "테스트", "")
        finally:
            (
                tgbot.agents, tgbot._command, tgbot.clear_web_data, logins.logged_in_dirs,
                telegram.verify, telegram.find_chat, telegram.send, tgbot.subprocess.Popen,
            ) = saved
        return out, reports, sent, seen_envs

    def test_tgbot_scrubs_token_on_success_failure_and_exception(self):
        log_path = os.path.join(paths.logs_dir(), "telegram-bot.log")
        for payload in ({"ok": False, "reason": "not_logged_in"}, None):
            with self.assertRaises(deps.StepError):
                self._run_tgbot(payload, f"leaked {self.tok} and https://api.telegram.org/bot{self.tok}/getMe")
            log_text = _read_text(log_path)
            self.assertNotIn(self.tok, log_text)
            self.assertIn("[hidden]", log_text)

        def boom_after_start(pct, text):
            if pct > 10:
                raise RuntimeError("boom")

        def slow_command(agent, task_file, workdir):
            code = "import sys, time; print(sys.argv[1], flush=True); time.sleep(0.4)"
            return [sys.executable, "-c", code, f"mid-run token {self.tok}"]

        saved = (tgbot.agents, tgbot._command, logins.logged_in_dirs, tgbot.time.sleep)
        tgbot.agents = lambda: ["claude"]
        tgbot._command = slow_command
        logins.logged_in_dirs = lambda a: [logins.account_dir(a, 1)]
        tgbot.time.sleep = lambda _s: __import__("time").sleep(0.05)
        try:
            with self.assertRaises(RuntimeError):
                tgbot.run(boom_after_start, "claude", "테스트", "")
        finally:
            (tgbot.agents, tgbot._command, logins.logged_in_dirs, tgbot.time.sleep) = saved
        log_text = _read_text(log_path)
        self.assertNotIn(self.tok, log_text)
        self.assertIn("[hidden]", log_text)

        out, _, _, seen_envs = self._run_tgbot(
            {"ok": True, "token": self.tok, "username": "sec_test_bot", "logout": True},
            f"Done! token is {self.tok}",
            clear_ok=True,
        )
        log_text = _read_text(log_path)
        self.assertNotIn(self.tok, log_text)
        self.assertEqual(seen_envs[0].get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")
        self.assertTrue(out["logged_out"])
        self.assertEqual(out["warning"], "")

    def test_tgbot_clear_web_data_failure_warns_and_never_reports_logout_done(self):
        out, reports, sent, _ = self._run_tgbot(
            {"ok": True, "token": self.tok, "username": "sec_test_bot", "logout": True},
            "ok",
            clear_ok=False,
        )
        final_pct, final_text = reports[-1]
        self.assertEqual(final_pct, 100)
        self.assertNotIn("로그아웃했습니다", final_text)
        self.assertIn("경고", final_text)
        self.assertFalse(out["cleared"])
        self.assertFalse(out["logged_out"])
        self.assertTrue(out["warning"])
        self.assertIn("경고", out["warning"])
        self.assertTrue(sent and "경고" in sent[0])

    def test_mailbot_scrubs_app_password_pattern_in_finally_on_failure_and_exception(self):
        log_path = os.path.join(paths.logs_dir(), "email-app-password.log")
        seen_envs = []
        orig_popen = mailbot.subprocess.Popen

        def run_case(payload, log_line):
            def fake_command(agent, task_file, workdir):
                code = (
                    "import json, re, sys; "
                    "print(sys.argv[3], flush=True); "
                    "t = open(sys.argv[1], encoding='utf-8').read(); "
                    "f = re.search(r'[Rr]esult file `([^`]+)`', t).group(1); "
                    "p = sys.argv[2]; "
                    "p and json.dump(json.loads(p), open(f, 'w', encoding='utf-8'))"
                )
                return [sys.executable, "-c", code, task_file, "" if payload is None else json.dumps(payload), log_line]

            def spy_popen(*args, **kwargs):
                if kwargs.get("env") is not None:
                    seen_envs.append(dict(kwargs["env"]))
                return orig_popen(*args, **kwargs)

            saved = (
                mailbot.agents, mailbot._command, mailbot.close_google_tabs,
                logins.logged_in_dirs, mailer.connect, mailbot.subprocess.Popen,
            )
            mailbot.agents = lambda: ["claude"]
            mailbot._command = fake_command
            mailbot.close_google_tabs = lambda: True
            logins.logged_in_dirs = lambda a: [logins.account_dir(a, 1)]
            mailer.connect = lambda a, p: {"ok": True}
            mailbot.subprocess.Popen = spy_popen
            try:
                mailbot.run(lambda *a: None, "claude", "me@gmail.com")
            finally:
                (
                    mailbot.agents, mailbot._command, mailbot.close_google_tabs,
                    logins.logged_in_dirs, mailer.connect, mailbot.subprocess.Popen,
                ) = saved

        for payload in ({"ok": False, "reason": "no_password"}, None):
            with self.assertRaises(deps.StepError):
                run_case(payload, f"dialog showed {self.pw_spaced} and raw {self.pw_raw} and nbsp aaaa\u00a0bbbb\u00a0cccc\u00a0dddd")
            log_text = _read_text(log_path)
            self.assertNotIn(self.pw_spaced, log_text)
            self.assertNotIn("qrst uvwx", log_text)
            # Round 2: no known password in these cases, so a bare 16-letter word stays;
            # only the spaced 4x4 form (and the exact known value) is hidden.
            self.assertIn(self.pw_raw, log_text)
            self.assertNotIn("aaaa\u00a0bbbb", log_text)
            self.assertIn("[hidden]", log_text)
        self.assertEqual(seen_envs[0].get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")

        # Exception raised mid-run via report callback:
        def boom_after_start(pct, text):
            if pct > 10:
                raise RuntimeError("boom")

        def slow_command(agent, task_file, workdir):
            code = "import sys, time; print(sys.argv[1], flush=True); time.sleep(0.4)"
            return [sys.executable, "-c", code, f"mid-run pw {self.pw_spaced} and {self.pw_raw}"]

        saved = (mailbot.agents, mailbot._command, logins.logged_in_dirs, mailbot.time.sleep)
        mailbot.agents = lambda: ["claude"]
        mailbot._command = slow_command
        logins.logged_in_dirs = lambda a: [logins.account_dir(a, 1)]
        mailbot.time.sleep = lambda _s: __import__("time").sleep(0.05)
        try:
            with self.assertRaises(RuntimeError):
                mailbot.run(boom_after_start, "claude", "me@gmail.com")
        finally:
            (mailbot.agents, mailbot._command, logins.logged_in_dirs, mailbot.time.sleep) = saved
        log_text = _read_text(log_path)
        self.assertNotIn(self.pw_spaced, log_text)
        self.assertIn(self.pw_raw, log_text)
        self.assertIn("[hidden]", log_text)


if __name__ == "__main__":
    unittest.main()
