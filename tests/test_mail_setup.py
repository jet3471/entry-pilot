"""E-mail setup tests (no network, no windows): provider by domain, clipboard app password, the AI run with a faked CLI."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="ep-mail-test-")
os.environ.setdefault("EP_APP_DIR", _TMP)

from app import deps, logins, mailbot, mailer, paths, server, settings  # noqa: E402

PW = "abcdefghijklmnop"


class Provider(unittest.TestCase):
    def test_provider_by_domain(self):
        self.assertEqual(mailer.provider(""), "")
        self.assertEqual(mailer.provider("nobody"), "")
        self.assertEqual(mailer.provider("a@gmail.com"), "google")
        self.assertEqual(mailer.provider("a@GoogleMail.com"), "google")
        self.assertEqual(mailer.provider("a@naver.com"), "naver")
        self.assertEqual(mailer.provider("a@daum.net"), "daum")
        self.assertEqual(mailer.provider("a@hanmail.net"), "daum")
        self.assertEqual(mailer.provider("a@example.org"), "other")

    def test_open_urls(self):
        self.assertEqual(mailer.OPEN_URLS["apppw"], "https://myaccount.google.com/apppasswords")
        self.assertEqual(mailer.OPEN_URLS["twosv"], "https://myaccount.google.com/signinoptions/twosv")


class Clipboard(unittest.TestCase):
    def test_pattern(self):
        self.assertEqual(mailer.app_password_in("abcd efgh ijkl mnop"), PW)
        self.assertEqual(mailer.app_password_in(PW), PW)
        self.assertEqual(mailer.app_password_in("  abcd efgh ijkl mnop\r\n"), PW)
        for bad in ("", "abcd efgh ijkl", "abcdefghijklmnopq", "ABCD EFGH IJKL MNOP", "abcd-efgh-ijkl-mnop",
                    "abcd efgh ijkl mno1", "my password is abcd efgh ijkl mnop", "123456789:AAExampleTokenxxxxxxxxxxxxxxxxxx"):
            self.assertEqual(mailer.app_password_in(bad), "", bad)

    def test_route_never_returns_the_password(self):
        sent = []
        saved = (mailer.password_in_clipboard, mailer.connect)
        mailer.password_in_clipboard = lambda: PW
        mailer.connect = lambda a, p: sent.append((a, p)) or {"ok": True}
        server._CLIP_TRIED[0] = ""
        server._MAIL_CLIP.update(pw="", masked="")
        try:
            r = server.handle("GET", "/api/email/clip", {"address": ["me@gmail.com"]}, {})
            self.assertTrue(r["ok"] and r["found"] and r["pending"])
            self.assertTrue(r["masked"])
            self.assertNotIn(PW, json.dumps(r))
            self.assertEqual(sent, [])  # must not connect before user confirms
            c = server.handle("POST", "/api/email/confirm", {}, {"address": "me@gmail.com", "masked": r["masked"]})
            self.assertEqual(c, {"ok": True, "error": ""})
            self.assertEqual(sent, [("me@gmail.com", PW)])
            self.assertEqual(settings.load()["notify"], "email")
            # the same clipboard text is not offered again after confirm
            self.assertEqual(server.handle("GET", "/api/email/clip", {"address": ["me@gmail.com"]}, {}), {"ok": True, "found": False})
            self.assertEqual(len(sent), 1)
            # other providers and empty address: the clipboard is not even read
            mailer.password_in_clipboard = lambda: self.fail("clipboard read")
            self.assertFalse(server.handle("GET", "/api/email/clip", {"address": ["me@naver.com"]}, {})["found"])
            self.assertFalse(server.handle("GET", "/api/email/clip", {}, {})["found"])
        finally:
            (mailer.password_in_clipboard, mailer.connect) = saved
            server._CLIP_TRIED[0] = ""
            server._MAIL_CLIP.update(pw="", masked="")
            settings.update(notify="")

    def test_failed_connect_returns_error_only(self):
        saved = (mailer.password_in_clipboard, mailer.connect)
        mailer.password_in_clipboard = lambda: "wxyz wxyz wxyz wxyz"
        mailer.connect = lambda a, p: {"ok": False, "error": "로그인 거절."}
        server._CLIP_TRIED[0] = ""
        server._MAIL_CLIP.update(pw="", masked="")
        try:
            clip = server.handle("GET", "/api/email/clip", {"address": ["me@gmail.com"]}, {})
            self.assertTrue(clip["ok"] and clip["found"] and clip["pending"])
            self.assertNotIn("wxyz", json.dumps(clip))
            r = server.handle("POST", "/api/email/confirm", {}, {"address": "me@gmail.com", "masked": clip["masked"]})
            # refused clipboard text is not offered again until clipboard changes
            self.assertEqual(server.handle("GET", "/api/email/clip", {"address": ["me@gmail.com"]}, {}), {"ok": True, "found": False})
        finally:
            (mailer.password_in_clipboard, mailer.connect) = saved
            server._CLIP_TRIED[0] = ""
            server._MAIL_CLIP.update(pw="", masked="")
        self.assertFalse(r["ok"])
        self.assertNotIn("wxyz", json.dumps(r))


class MailBot(unittest.TestCase):
    def _run(self, result: dict, address="me@gmail.com", log_text=""):
        def fake_command(agent, task_file, workdir):
            code = ("import json, re, sys; t = open(sys.argv[1], encoding='utf-8').read(); "
                    "f = re.search(r'[Rr]esult file `([^`]+)`', t).group(1); "
                    "json.dump(json.loads(sys.argv[2]), open(f, 'w')); print(sys.argv[3])")
            return [sys.executable, "-c", code, task_file, json.dumps(result), log_text]
        sent = []
        saved = (mailbot.agents, mailbot._command, mailbot.close_google_tabs, logins.logged_in_dirs, mailer.connect)
        mailbot.agents = lambda: ["claude"]
        mailbot._command = fake_command
        mailbot.close_google_tabs = lambda: True
        logins.logged_in_dirs = lambda a: [logins.account_dir(a, 1)]
        mailer.connect = lambda a, p: sent.append((a, p)) or {"ok": True}
        try:
            mailbot.run(lambda *a: None, "claude", address)
        finally:
            (mailbot.agents, mailbot._command, mailbot.close_google_tabs, logins.logged_in_dirs, mailer.connect) = saved
        return sent

    def test_password_is_handed_to_connect_and_removed_from_files(self):
        sent = self._run({"ok": True, "password": "abcd efgh ijkl mnop"}, log_text="abcdefghijklmnop and abcd efgh ijkl mnop")
        self.assertEqual(sent, [("me@gmail.com", PW)])
        self.assertEqual(settings.load()["notify"], "email")
        work = paths.sub("data", "work", "email-app-password")
        self.assertFalse(os.path.exists(os.path.join(work, "result.json")))
        self.assertFalse(os.path.exists(os.path.join(work, "task.md")))
        log = open(os.path.join(paths.logs_dir(), "email-app-password.log"), encoding="utf-8").read()
        self.assertNotIn("abcdefghijklmnop", log)
        self.assertNotIn("abcd efgh", log)
        settings.update(notify="")

    def test_two_step_off_asks_for_twosv(self):
        with self.assertRaises(deps.StepError) as cm:
            self._run({"ok": False, "reason": "two_step_off"})
        self.assertIn("2단계 인증", str(cm.exception))
        self.assertEqual(mailbot.LAST["need"], "twosv")

    def test_bad_password_text_is_refused(self):
        with self.assertRaises(deps.StepError):
            self._run({"ok": True, "password": "not a password"})

    def test_not_logged_in_and_non_gmail(self):
        with self.assertRaises(deps.StepError) as cm:
            self._run({"ok": False, "reason": "not_logged_in"})
        self.assertIn("로그인", str(cm.exception))
        with self.assertRaises(deps.StepError) as cm:
            self._run({"ok": True, "password": PW}, address="me@naver.com")
        self.assertIn("Gmail", str(cm.exception))
        r = server.handle("POST", "/api/email/start", {}, {"agent": "claude", "address": "me@naver.com"})
        self.assertFalse(r["ok"])

    def test_prompt_values_and_restrictions(self):
        text = mailbot._prompt(r"C:\x\result.json")
        self.assertNotIn("{{", text)
        self.assertIn(r"C:\x\result.json", text)
        self.assertIn("https://myaccount.google.com/apppasswords", text)
        for must in ("Do not open Gmail", "Never type the user's Google password", "2-Step Verification",
                     "two_step_off", "Do not print it", "Entry Pilot"):
            self.assertIn(must, text.replace("\n", " "), must)


class SenderAndInbox(unittest.TestCase):
    def test_gmail_sends_any_inbox_receives(self):
        self.assertFalse(mailer.connect("me@naver.com", "abcdefghijklmnop")["ok"])
        self.assertFalse(server.handle("POST", "/api/email/to", {}, {"to": "not an address"})["ok"])
        self.assertTrue(server.handle("POST", "/api/email/to", {}, {"to": "me@naver.com"})["ok"])
        sent = []

        class Fake:
            def send_message(self, m):
                sent.append((m["From"], m["To"]))

            def quit(self):
                pass
        old = mailer._connect
        mailer._connect = lambda a, p: Fake()
        try:
            self.assertTrue(mailer.connect("sender@gmail.com", "abcd efgh ijkl mnop")["ok"])
        finally:
            mailer._connect = old
            settings.update(email_to="", email_address="", notify="")
            settings.set_secret("email_password", "")
        self.assertEqual(sent, [("sender@gmail.com", "me@naver.com")])


if __name__ == "__main__":
    unittest.main()
