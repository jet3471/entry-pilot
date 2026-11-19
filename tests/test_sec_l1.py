"""Security regression tests for items 3 (UI/CSP/file serving), 6 (browser path), and 14 (Telegram link & poller).
No network, no real Chrome, no external processes."""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="ep-sec-l1-")
os.environ["EP_APP_DIR"] = _TMP
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import engine, paths, server, settings, telegram  # noqa: E402
from cp import review as cp_review  # noqa: E402


class Item3PageAndFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        cls.srv = server.serve()
        cls.port = cls.srv.server_address[1]
        cls.draft_dir = tempfile.mkdtemp(prefix="ep-sec-draft-")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.draft_dir, ignore_errors=True)

    def raw_get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, dict(e.headers.items()), e.read()
            finally:
                e.close()

    def test_headers_csp_and_referrer_policy_on_all_responses(self):
        # App HTML page
        st, hdrs, body = self.raw_get("/")
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("Referrer-Policy"), "no-referrer")
        csp = hdrs.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("'unsafe-inline'", csp.split("script-src")[1].split(";")[0])

        # Static JS
        st, hdrs, _ = self.raw_get("/app.js")
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("Referrer-Policy"), "no-referrer")

        # 403 error response also carries Referrer-Policy: no-referrer
        st, hdrs, _ = self.raw_get("/api/state", headers={"X-EP-Token": "wrong"})
        self.assertEqual(st, 403)
        self.assertEqual(hdrs.get("Referrer-Policy"), "no-referrer")

        # 200 JSON API response also carries Referrer-Policy: no-referrer
        st, hdrs, _ = self.raw_get("/api/state", headers={"X-EP-Token": server.TOKEN})
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("Referrer-Policy"), "no-referrer")

        # index.html has no inline <script> blocks or inline event handlers
        html = body.decode("utf-8")
        self.assertIn('<script src="/app.js"></script>', html)
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.I))
        self.assertIsNone(re.search(r"\bon[a-z]+\s*=", html, re.I))

    def test_contest_file_serves_only_raster_inline_and_sandboxes(self):
        files = {
            "pic.png": (b"\x89PNG\r\n\x1a\n", "image/png", True),
            "photo.JPG": (b"\xff\xd8\xff\xe0", "image/jpeg", True),
            "photo2.jpeg": (b"\xff\xd8\xff\xe0", "image/jpeg", True),
            "anim.gif": (b"GIF89a", "image/gif", True),
            "modern.WEBP": (b"RIFF0000WEBP", "image/webp", True),
            "xss.svg": (b"<svg onload=alert(1)></svg>", "application/octet-stream", False),
            "page.html": (b"<script>alert(1)</script>", "application/octet-stream", False),
            "doc.pdf": (b"%PDF-1.4", "application/octet-stream", False),
            "note.md": (b"# title", "application/octet-stream", False),
        }
        for name, (content, _, _) in files.items():
            with open(os.path.join(self.draft_dir, name), "wb") as f:
                f.write(content)

        def fake_file(iid, name):
            p = os.path.join(self.draft_dir, name)
            return p if iid == "C1" and os.path.basename(name) == name and os.path.isfile(p) else ""

        with mock.patch.object(engine, "contest_file", side_effect=fake_file):
            for name, (content, want_mime, inline) in files.items():
                q = urllib.parse.urlencode({"id": "C1", "name": name, "t": server.TOKEN})
                st, hdrs, data = self.raw_get(f"/api/contest/file?{q}")
                self.assertEqual(st, 200, name)
                self.assertEqual(data, content, name)
                self.assertEqual(hdrs.get("Content-Type"), want_mime, name)
                self.assertEqual(hdrs.get("Content-Security-Policy"), "default-src 'none'; sandbox", name)
                self.assertEqual(hdrs.get("Referrer-Policy"), "no-referrer", name)
                if inline:
                    self.assertNotIn("attachment", hdrs.get("Content-Disposition", ""), name)
                else:
                    self.assertEqual(hdrs.get("Content-Disposition"), "attachment", name)

            # Missing file still gets sandbox CSP and no-referrer
            st, hdrs, _ = self.raw_get(f"/api/contest/file?id=C1&name=missing.svg&t={server.TOKEN}")
            self.assertEqual(st, 404)
            self.assertEqual(hdrs.get("Content-Security-Policy"), "default-src 'none'; sandbox")
            self.assertEqual(hdrs.get("Referrer-Policy"), "no-referrer")

    def test_review_units_encodes_query_values(self):
        bad_img = 'a&b=c#1" onerror="alert(1) 한글+.png'
        bad_pdf = 'form&v=2#p"x.pdf'
        bad_id = "C01&x=1"
        it = {"id": bad_id, "work": {"draft_dir": self.draft_dir, "draft_files": [bad_pdf, bad_img]}}
        send = {"how": "이메일", "to": "a@example.com", "files": [bad_pdf, bad_img]}
        with mock.patch.object(cp_review.preview, "page_count", return_value=1):
            us = cp_review.units(it, send)
        urls = [u["url"] for u in us if u["type"] == "image"]
        self.assertEqual(len(urls), 2)
        for u in urls:
            self.assertNotIn('"', u)
            self.assertNotIn("#", u)
            self.assertNotIn(" ", u)
            route, qs = u.split("?", 1)
            parsed = urllib.parse.parse_qs(qs)
            self.assertEqual(parsed["id"], [bad_id])
            if route == "preview":
                self.assertEqual(parsed["pdf"], [bad_pdf])
            else:
                self.assertEqual(parsed["name"], [bad_img])

    def test_app_js_uses_safe_url_and_escapes_cx_unit_src(self):
        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("const safeUrl =", js)
        self.assertIn('u.protocol === "http:" || u.protocol === "https:"', js)
        # No raw esc(it.url) in href
        self.assertNotIn('href="${esc(it.url)}"', js)
        # Every dynamic href in app.js goes through safeUrl + esc
        hrefs = re.findall(r'href="([^"]+)"', js)
        for h in hrefs:
            self.assertTrue(h == "#" or h.startswith("${esc("), f"unsafe href pattern: {h}")
        # cxUnit escapes src attribute
        self.assertIn('src="${esc(src(u.url))}"', js)
        self.assertNotIn('src="${src(u.url)}"', js)


class Item6BrowserPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-sec-browser-")
        settings.save(dict(settings.DEFAULTS))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        settings.save(dict(settings.DEFAULTS))

    def _touch(self, *parts: str) -> str:
        p = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"MZ")
        return p

    def test_allows_only_signed_chrome_or_msedge_in_standard_location(self):
        from ep import browser_check as ep_bcheck

        pf = os.path.join(self.tmp, "pf")
        c_exe = self._touch("pf", "Google", "Chrome", "Application", "chrome.exe")
        e_exe = self._touch("pf", "Microsoft", "Edge", "Application", "msedge.exe")
        loose_chrome = self._touch("Chrome.EXE")  # right name, non-standard location
        bad_exe = self._touch("calc.exe")
        fake_dir = os.path.join(self.tmp, "msedge_dir", "chrome.exe")
        os.makedirs(fake_dir)
        signers = {
            os.path.normcase(os.path.realpath(c_exe)): (True, "CN=Google LLC, O=Google LLC"),
            os.path.normcase(os.path.realpath(e_exe)): (True, "CN=Microsoft Corporation, O=Microsoft Corporation"),
            os.path.normcase(os.path.realpath(loose_chrome)): (True, "CN=Google LLC, O=Google LLC"),
        }

        def fake_sig(path):
            return signers.get(os.path.normcase(os.path.realpath(path)), (False, ""))

        env = {"ProgramFiles": pf, "ProgramFiles(x86)": pf, "LOCALAPPDATA": pf}
        ep_bcheck._SIG_CACHE.clear()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(ep_bcheck, "_read_authenticode_subject", side_effect=fake_sig):
            r = server.handle("POST", "/api/settings", {}, {"chrome": c_exe})
            self.assertTrue(r["ok"])
            self.assertEqual(settings.load()["chrome"], os.path.realpath(c_exe))

            r = server.handle("POST", "/api/settings", {}, {"chrome": e_exe})
            self.assertTrue(r["ok"])
            self.assertEqual(settings.load()["chrome"], os.path.realpath(e_exe))

            # Arbitrary executable, non-standard location or bad signature rejected; previous setting stays
            for invalid in (
                bad_exe,
                loose_chrome,
                os.path.join(self.tmp, "missing", "chrome.exe"),
                fake_dir,
                c_exe + ".",
                "   ",
                123,
                None,
            ):
                r = server.handle("POST", "/api/settings", {}, {"chrome": invalid})
                self.assertFalse(r["ok"], f"should reject {invalid!r}")
                self.assertEqual(settings.load()["chrome"], os.path.realpath(e_exe))

            # Standard location but unsigned / wrong signer -> rejected
            signers[os.path.normcase(os.path.realpath(c_exe))] = (True, "CN=Microsoft Corporation")
            ep_bcheck._SIG_CACHE.clear()
            r = server.handle("POST", "/api/settings", {}, {"chrome": c_exe})
            self.assertFalse(r["ok"])
            self.assertEqual(settings.load()["chrome"], os.path.realpath(e_exe))

            # Empty string clears back to auto-detect default
            r = server.handle("POST", "/api/settings", {}, {"chrome": ""})
            self.assertTrue(r["ok"])
            self.assertEqual(settings.load()["chrome"], "")
        ep_bcheck._SIG_CACHE.clear()


class Item14TelegramSecurity(unittest.TestCase):
    def setUp(self):
        server._TG_CLIP.update(tok="", info=None)
        telegram._START_CODES.clear()
        telegram._USED_CODES.clear()
        settings.save(dict(settings.DEFAULTS))
        settings.set_secret("telegram_token", "")

    def tearDown(self):
        server._TG_CLIP.update(tok="", info=None)
        telegram._START_CODES.clear()
        telegram._USED_CODES.clear()
        settings.save(dict(settings.DEFAULTS))
        settings.set_secret("telegram_token", "")

    def test_clipboard_poll_requires_user_confirmation_before_saving(self):
        tok = "123456789:AA" + "z" * 33
        verify_calls = []

        def fake_verify(t):
            verify_calls.append(t)
            return {"ok": True, "username": "my_entry_bot", "name": "내 응모 봇"}

        with mock.patch.object(telegram, "token_in_clipboard", return_value=tok), \
                mock.patch.object(telegram, "verify", side_effect=fake_verify):
            r1 = server.handle("GET", "/api/telegram/clip", {}, {})
            self.assertTrue(r1["ok"] and r1["found"])
            self.assertEqual(r1["username"], "my_entry_bot")
            self.assertEqual(r1["name"], "내 응모 봇")
            # Must NOT be saved yet!
            self.assertEqual(settings.get_secret("telegram_token"), "")
            self.assertEqual(settings.load().get("telegram_bot", ""), "")

            # Polling again with same clipboard text does not re-call Telegram API or save
            r2 = server.handle("GET", "/api/telegram/clip", {}, {})
            self.assertTrue(r2["ok"] and r2["found"])
            self.assertEqual(len(verify_calls), 1)
            self.assertEqual(settings.get_secret("telegram_token"), "")

            # Confirm with mismatched username is rejected
            bad = server.handle("POST", "/api/telegram/confirm", {}, {"username": "other_bot"})
            self.assertFalse(bad["ok"])
            self.assertEqual(settings.get_secret("telegram_token"), "")

            # Confirm saves token and generates start code
            ok = server.handle("POST", "/api/telegram/confirm", {}, {"username": "my_entry_bot"})
            self.assertTrue(ok["ok"])
            self.assertEqual(settings.get_secret("telegram_token"), tok)
            s = settings.load()
            self.assertEqual(s["telegram_bot"], "my_entry_bot")
            self.assertTrue(s["telegram_start"])
            self.assertGreater(s["telegram_start_at"], 0)

    def test_start_code_rejects_empty_is_single_use_and_expires_after_10_minutes(self):
        tok = "123456789:AA" + "k" * 33
        ups = [
            {"message": {"chat": {"id": 11, "type": "private"}, "text": "hello"}},
            {"message": {"chat": {"id": 22, "type": "private"}, "text": "/start"}},
        ]
        with mock.patch.object(telegram, "call", return_value=ups) as mcall:
            self.assertFalse(telegram.find_chat(tok, "")["ok"])
            self.assertFalse(telegram.find_chat(tok, "   ")["ok"])
            mcall.assert_not_called()

        # Server route rejects empty start code
        settings.set_secret("telegram_token", tok)
        settings.update(telegram_bot="b_bot", telegram_start="", telegram_start_at=time.time())
        self.assertFalse(server.handle("POST", "/api/telegram/find", {}, {})["ok"])

        # Fresh start code via save_bot -> single use
        server.save_bot(tok, {"username": "b_bot", "name": "Bot"})
        code = settings.load()["telegram_start"]
        self.assertTrue(code)
        valid_ups = [
            {"message": {"chat": {"id": 55, "type": "private"}, "text": f"/start {code}", "date": time.time()}},
            {"message": {"chat": {"id": 66, "type": "private"}, "text": f"/start {code}", "date": time.time()}},
        ]
        with mock.patch.object(telegram, "call", return_value=valid_ups), \
                mock.patch.object(telegram, "send"):
            r1 = server.handle("POST", "/api/telegram/find", {}, {})
            self.assertTrue(r1["ok"])
            self.assertEqual(settings.load()["telegram_chat_id"], "66")
            self.assertEqual(settings.load()["telegram_start"], "")

            # Second attempt with same start code fails (both server route and direct find_chat)
            r2 = server.handle("POST", "/api/telegram/find", {}, {})
            self.assertFalse(r2["ok"])
            self.assertFalse(telegram.find_chat(tok, code)["ok"])

        # 10-minute expiration (> 600s)
        server.save_bot(tok, {"username": "b_bot", "name": "Bot"})
        exp_code = settings.load()["telegram_start"]
        expired_at = time.time() - 601
        settings.update(telegram_start_at=expired_at)
        telegram._START_CODES[exp_code] = expired_at
        with mock.patch.object(telegram, "call", return_value=[
            {"message": {"chat": {"id": 77, "type": "private"}, "text": f"/start {exp_code}"}}
        ]):
            self.assertFalse(telegram.find_chat(tok, exp_code)["ok"])
            r_exp = server.handle("POST", "/api/telegram/find", {}, {})
            self.assertFalse(r_exp["ok"])
            self.assertEqual(settings.load()["telegram_start"], "")

    def test_poller_checks_reply_from_id_and_stops_when_notify_not_telegram(self):
        tok = "123456789:AA" + "p" * 33
        settings.set_secret("telegram_token", tok)
        settings.update(telegram_chat_id="100", notify="telegram")

        replies, buttons, sent = [], [], []
        poller = telegram.Poller(
            on_button=lambda d: buttons.append(d) or "ok-btn",
            log=lambda m: None,
            on_reply=lambda mid, txt: replies.append((mid, txt)) or "ok-reply",
        )

        updates = [
            # Reply in chat 100, but from.id is 999 -> must be ignored
            {"update_id": 1, "message": {"chat": {"id": 100}, "from": {"id": 999},
                                         "reply_to_message": {"message_id": 7}, "text": "spoof"}},
            # Reply in chat 100 with missing from -> must be ignored
            {"update_id": 2, "message": {"chat": {"id": 100},
                                         "reply_to_message": {"message_id": 7}, "text": "nofrom"}},
            # Legitimate reply from linked user 100 -> handled
            {"update_id": 3, "message": {"chat": {"id": 100}, "from": {"id": 100},
                                         "reply_to_message": {"message_id": 7}, "text": "real"}},
        ]
        with mock.patch.object(telegram, "call", return_value=updates), \
                mock.patch.object(telegram, "send", side_effect=lambda txt, **kw: sent.append(txt)):
            self.assertTrue(poller.poll_once())
        self.assertEqual(replies, [(7, "real")])
        self.assertEqual(sent, ["ok-reply"])

        # Switch notify away from telegram (email or none): the poller may still call getUpdates
        # to advance the offset and drop pending updates, but it must not act on them.
        replies.clear()
        sent.clear()
        for n, mode in enumerate(("none", "email")):
            settings.update(notify=mode)
            uid = 20 + 2 * n
            dropped = [
                {"update_id": uid, "message": {"chat": {"id": 100}, "from": {"id": 100},
                                               "reply_to_message": {"message_id": 7}, "text": "drop"}},
                {"update_id": uid + 1, "callback_query": {"id": "cq0", "from": {"id": 100},
                                                          "message": {"message_id": 9, "chat": {"id": 100}},
                                                          "data": "done:E1"}},
            ]
            with mock.patch.object(telegram, "call", return_value=dropped) as mcall, \
                    mock.patch.object(telegram, "send", side_effect=lambda txt, **kw: sent.append(txt)):
                self.assertFalse(poller.poll_once())
            for c in mcall.call_args_list:
                self.assertEqual(c.args[0], "getUpdates")
            self.assertEqual(poller.offset, uid + 2)
            self.assertEqual(replies, [])
            self.assertEqual(buttons, [])
            self.assertEqual(sent, [])

        # Even if notify changes mid-poll while getUpdates returns button/reply updates, nothing is handled
        settings.update(notify="telegram")
        mid_updates = [
            {"update_id": 4, "message": {"chat": {"id": 100}, "from": {"id": 100},
                                         "reply_to_message": {"message_id": 8}, "text": "late"}},
            {"update_id": 5, "callback_query": {"id": "cq1", "from": {"id": 100},
                                                "message": {"message_id": 9, "chat": {"id": 100}}, "data": "done:E1"}},
        ]

        def switch_during_getupdates(method, **kw):
            if method == "getUpdates":
                settings.update(notify="none")
                return mid_updates
            return {}

        replies.clear()
        buttons.clear()
        with mock.patch.object(telegram, "call", side_effect=switch_during_getupdates):
            poller.poll_once()
        self.assertEqual(replies, [])
        self.assertEqual(buttons, [])


if __name__ == "__main__":
    unittest.main()
