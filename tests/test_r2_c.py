"""Round 2 group C security tests (C1-C8). No network, no real Chrome, no AI CLI."""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from unittest import mock

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="ep-r2c-")
os.environ["EP_APP_DIR"] = _TMP
os.environ["EP_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["EP_OWNER_DIR"] = os.path.join(_TMP, "data", "board")
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import deps, engine, logins, mailbot, mailer, main as app_main, notify, paths, secure, server, settings, telegram, tgbot  # noqa: E402
from ep import cli as ep_cli, config as ep_config, model as ep_model, scheduler as ep_sched, store as ep_store  # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class HttpHelperMixin:
    def http_req(self, port: int, method: str, path: str, body: bytes | dict | None = None, headers: dict | None = None):
        hdrs = dict(headers or {})
        data = None
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            data = body
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, dict(r.headers.items()), raw
        except urllib.error.HTTPError as e:
            try:
                return e.code, dict(e.headers.items()), e.read()
            finally:
                e.close()


class TestC1SessionAndRunJson(unittest.TestCase, HttpHelperMixin):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        cls.srv = server.serve()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_launch_code_exchange_is_single_use_and_expires_in_60s(self):
        opened = []
        with mock.patch.object(app_main, "_open_window", side_effect=opened.append):
            r = server.open_window(self.port)
        self.assertTrue(r["ok"])
        self.assertEqual(len(opened), 1)
        url = opened[0]
        self.assertNotIn(server.TOKEN, url)
        self.assertNotIn(server.OPEN_CODE, url)
        parsed = urllib.parse.urlparse(url)
        code = urllib.parse.parse_qs(parsed.query).get("c", [""])[0]
        self.assertTrue(code)

        # Exchange once -> gets real token
        st, _, raw = self.http_req(self.port, "POST", "/api/session", {"code": code})
        self.assertEqual(st, 200)
        j = json.loads(raw)
        self.assertEqual((j["ok"], j["token"]), (True, server.TOKEN))

        # Second exchange with the same launch code -> refused
        st2, _, _ = self.http_req(self.port, "POST", "/api/session", {"code": code})
        self.assertEqual(st2, 403)

        # Expired launch code (> LAUNCH_TTL = 300 s) -> refused
        self.assertEqual(server.LAUNCH_TTL, 300)
        exp_code = server.new_launch_code()
        server._LAUNCH_CODES[exp_code] = time.time() - (server.LAUNCH_TTL + 1)
        st3, _, _ = self.http_req(self.port, "POST", "/api/session", {"code": exp_code})
        self.assertEqual(st3, 403)

        # Unknown / extra-parameter / GET requests -> refused
        self.assertEqual(self.http_req(self.port, "POST", "/api/session", {"code": "unknown"})[0], 403)
        fresh = server.new_launch_code()
        self.assertEqual(self.http_req(self.port, "POST", "/api/session", {"code": fresh, "extra": 1})[0], 403)
        self.assertEqual(self.http_req(self.port, "GET", f"/api/session?c={fresh}")[0], 403)

    def test_open_window_route_accepts_only_open_code(self):
        opened = []
        with mock.patch.object(app_main, "_open_window", side_effect=opened.append):
            # Rejects API token, launch code, wrong code, extra keys, query string, GET
            self.assertEqual(self.http_req(self.port, "POST", "/api/open-window",
                                           {"open_code": server.TOKEN},
                                           {"X-EP-Token": server.TOKEN})[0], 403)
            lc = server.new_launch_code()
            self.assertEqual(self.http_req(self.port, "POST", "/api/open-window", {"open_code": lc})[0], 403)
            self.assertEqual(self.http_req(self.port, "POST", "/api/open-window",
                                           {"open_code": server.OPEN_CODE, "url": "https://evil.example"})[0], 403)
            self.assertEqual(self.http_req(self.port, "POST", "/api/open-window?x=1",
                                           {"open_code": server.OPEN_CODE})[0], 403)
            self.assertEqual(self.http_req(self.port, "GET", "/api/open-window")[0], 403)
            self.assertEqual(opened, [])

            # Accepts valid open_code and opens a new window without returning secrets
            st, _, raw = self.http_req(self.port, "POST", "/api/open-window", {"open_code": server.OPEN_CODE})
            self.assertEqual(st, 200)
            resp = json.loads(raw)
            self.assertEqual(resp, {"ok": True})
            self.assertNotIn(server.TOKEN, raw.decode("utf-8"))
            self.assertEqual(len(opened), 1)
            self.assertIn("/?c=", opened[0])
            self.assertNotIn(server.TOKEN, opened[0])

            # _request_open_window helper used by second instance
            self.assertTrue(app_main._request_open_window({"port": self.port, "pid": os.getpid(), "open_code": server.OPEN_CODE}))
            self.assertEqual(len(opened), 2)

    def test_run_json_under_localappdata_even_for_portable_and_acl_restricted(self):
        local_tmp = tempfile.mkdtemp(prefix="ep-localapp-")
        self.addCleanup(shutil.rmtree, local_tmp, True)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": local_tmp}), \
                mock.patch.object(paths, "portable", return_value=True):
            rf = app_main.run_file_path()
            self.assertEqual(os.path.normcase(os.path.dirname(rf)),
                             os.path.normcase(os.path.join(local_tmp, "EntryPilot")))
            self.assertRegex(os.path.basename(rf), r"^run-[0-9a-f]{8}\.json$")
            with mock.patch.object(secure, "restrict_acl", wraps=secure.restrict_acl) as macl:
                app_main.write_run_file(rf, self.port, server.OPEN_CODE)
                self.assertTrue(any(os.path.normcase(c.args[0]) == os.path.normcase(rf) for c in macl.call_args_list))
            with open(rf, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(set(data.keys()), {"port", "pid", "open_code"})
            self.assertNotIn("token", data)
            self.assertNotIn(server.TOKEN, json.dumps(data))
            icacls = os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe")
            acl_out = subprocess.run([icacls, rf], capture_output=True, text=True,
                                     encoding="mbcs", errors="replace", check=True).stdout
            self.assertTrue(acl_out.strip())

    def test_ui_exchanges_session_and_replaces_url(self):
        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertNotIn('new URLSearchParams(location.search).get("t")', js)
        self.assertIn('fetch("/api/session"', js)
        self.assertIn("history.replaceState", js)


class TestC2SyncXlsxPending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2c-xlsx-")
        self.saved_env = {k: os.environ.get(k) for k in ("EP_APP_DIR", "EP_DATA_DIR", "EP_OWNER_DIR")}
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "data", "board")
        paths.ensure_dirs()
        engine.ensure_initialized()

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_items_and_xlsx(self):
        dl = (ep_model.today_kst() + timedelta(days=5)).isoformat()
        m1 = ep_cli.call(["add", "--agent", "gemini", "--json",
                          json.dumps({"title": "Manual Done", "org": "Org1", "url": "https://forms.gle/m1", "deadline": dl})])["results"][0]["id"]
        ep_cli.call(["manual", m1, "--agent", "codex", "--need", "login", "--reason", "login needed"])
        m2 = ep_cli.call(["add", "--agent", "gemini", "--json",
                          json.dumps({"title": "Manual Skip", "org": "Org2", "url": "https://forms.gle/m2", "deadline": dl})])["results"][0]["id"]
        ep_cli.call(["manual", m2, "--agent", "codex", "--need", "app", "--reason", "app needed"])
        o1 = ep_cli.call(["add", "--agent", "gemini", "--json",
                          json.dumps({"title": "Owner Ask", "org": "Org3", "url": "https://forms.gle/o1", "deadline": dl})])["results"][0]["id"]
        ep_cli.call(["verify", o1, "--agent", "codex", "--lane", "form"])
        ep_cli.call(["owner", o1, "--agent", "codex", "--question", "Which option?"])

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = ep_config.XLSX_SHEET_MANUAL
        ws.append([ep_config.XLSX_COL_ID, "제목", ep_config.XLSX_COL_DONE])
        ws.append([m1, "Manual Done", "했음"])
        ws.append([m2, "Manual Skip", "안함"])
        wo = wb.create_sheet(ep_config.XLSX_SHEET_OWNER)
        wo.append([ep_config.XLSX_COL_ID, "질문", ep_config.XLSX_COL_ANSWER])
        wo.append([o1, "Which option?", "2번"])
        os.makedirs(os.path.dirname(ep_config.owner_xlsx_path()), exist_ok=True)
        wb.save(ep_config.owner_xlsx_path())
        wb.close()
        return m1, m2, o1

    def test_sync_xlsx_records_pending_only_and_app_confirms(self):
        m1, m2, o1 = self._add_items_and_xlsx()

        # CLI sync-xlsx and Scheduler.housekeeping must NOT apply owner actions
        r = ep_cli.call(["sync-xlsx"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["changed"], 0)
        self.assertEqual(r["pending"], 3)

        db = ep_store.connect()
        try:
            self.assertEqual(ep_store.get_item(db, m1)["state"], "manual")
            self.assertEqual(ep_store.get_item(db, m2)["state"], "manual")
            self.assertEqual(ep_store.get_item(db, o1)["state"], "owner")
            sched = ep_sched.Scheduler(dry_run=False, db=db, log=lambda m: None)
            sched.tab_sweep = lambda s: None
            s_out = {"actions": [], "notes": []}
            sched.housekeeping({}, s_out)
            self.assertEqual(ep_store.get_item(db, m1)["state"], "manual")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM owner_answers WHERE applied=1").fetchone()[0], 0)
        finally:
            db.close()

        # CLI cannot confirm xlsx changes
        self.assertFalse(ep_cli.call(["confirm-xlsx"])["ok"])

        # App to-do API returns pending items (with version and hash)
        items_resp = server.handle("GET", "/api/items", {"state": ["owner"]}, {})
        pending_list = items_resp["xlsx_pending"]
        self.assertEqual(len(pending_list), 3)
        by_id = {p["id"]: p for p in pending_list}

        # Confirm one displayed item in-process through app server (id + version + hash)
        p_m1 = by_id[m1]
        c1 = server.handle("POST", "/api/xlsx/confirm", {},
                           {"id": m1, "version": p_m1["version"], "hash": p_m1["hash"]})
        self.assertTrue(c1["ok"], c1)
        self.assertEqual(c1["count"], 1)
        db = ep_store.connect()
        try:
            self.assertEqual(ep_store.get_item(db, m1)["state"], "submitted")
            self.assertEqual(ep_store.get_item(db, m2)["state"], "manual")
            self.assertEqual(ep_store.get_item(db, o1)["state"], "owner")
        finally:
            db.close()

        # Confirm remaining displayed items with "모두 확인" (displayed items list)
        rem_items = [{"id": by_id[k]["id"], "version": by_id[k]["version"], "hash": by_id[k]["hash"]}
                     for k in (m2, o1)]
        c_all = server.handle("POST", "/api/xlsx/confirm", {}, {"items": rem_items})
        self.assertTrue(c_all["ok"], c_all)
        self.assertEqual(c_all["count"], 2)
        db = ep_store.connect()
        try:
            self.assertEqual(ep_store.get_item(db, m2)["state"], "excluded")
            it_o = ep_store.get_item(db, o1)
            self.assertEqual(it_o["state"], "new")
            self.assertIn("owner answer: 2번", it_o["verify_notes"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM owner_answers WHERE applied=0").fetchone()[0], 0)
        finally:
            db.close()

        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("엑셀에서 바꾼 항목 ${pending.length}건", js)
        self.assertIn("모두 확인", js)
        self.assertIn("data-xlsx-confirm", js)


class TestC3TelegramSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2c-tg-")
        self.saved_env = {k: os.environ.get(k) for k in ("EP_APP_DIR", "EP_DATA_DIR", "EP_OWNER_DIR")}
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "data", "board")
        paths.ensure_dirs()
        engine.ensure_initialized()
        telegram._START_CODES.clear()
        telegram._USED_CODES.clear()
        settings.save(dict(settings.DEFAULTS))

    def tearDown(self):
        telegram._START_CODES.clear()
        telegram._USED_CODES.clear()
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tgbot_sets_start_at_via_new_start_code_and_server_find_works(self):
        tok = "111222333:AA" + "c" * 33

        def fake_cmd(agent, task_file, workdir):
            script = (
                "import json, re, sys; t = open(sys.argv[1], encoding='utf-8').read(); "
                "f = re.search(r'result file `([^`]+)`', t).group(1); "
                f"json.dump({{'ok': True, 'token': '{tok}', 'username': 'ai_made_bot', 'logout': True}}, open(f, 'w'))"
            )
            return [sys.executable, "-c", script, task_file]

        with mock.patch.object(tgbot, "agents", return_value=["codex"]), \
                mock.patch.object(tgbot, "_command", side_effect=fake_cmd), \
                mock.patch.object(tgbot, "clear_web_data", return_value=True), \
                mock.patch.object(logins, "logged_in_dirs", return_value=[logins.account_dir("codex", 1)]), \
                mock.patch.object(telegram, "verify", return_value={"ok": True, "id": "111222333", "username": "ai_made_bot"}), \
                mock.patch.object(telegram, "find_chat", return_value={"ok": False, "error": "not yet"}), \
                mock.patch.object(tgbot.time, "sleep"):
            tgbot.run(lambda *a: None, "codex", "내 봇", "")

        s = settings.load()
        code = s["telegram_start"]
        self.assertTrue(code)
        self.assertIn(code, telegram._START_CODES)
        self.assertGreater(s["telegram_start_at"], 0)

        # /api/telegram/find must NOT immediately expire the code; it must link when /start arrives
        ups = [{"update_id": 1, "message": {"chat": {"id": 777, "type": "private"}, "text": f"/start {code}", "date": time.time()}}]
        with mock.patch.object(telegram, "call", return_value=ups), \
                mock.patch.object(telegram, "send"):
            r = server.handle("POST", "/api/telegram/find", {}, {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(settings.load()["telegram_chat_id"], "777")

    def test_used_start_codes_persist_across_restart_and_never_clear_wholesale(self):
        tok = "111222333:AA" + "d" * 33
        first_code = telegram.new_start_code()
        with mock.patch.object(telegram, "call", return_value=[
            {"update_id": 1, "message": {"chat": {"id": 10, "type": "private"}, "text": f"/start {first_code}", "date": time.time()}}
        ]):
            self.assertTrue(telegram.find_chat(tok, first_code)["ok"])

        # Add > 1005 more used codes and simulate restart by clearing in-memory dicts
        bulk = [f"used{i:05d}" for i in range(1005)]
        settings.update(telegram_used_codes=[first_code] + bulk)
        telegram._USED_CODES.clear()
        telegram._START_CODES[first_code] = time.time()

        with mock.patch.object(telegram, "call", return_value=[
            {"update_id": 2, "message": {"chat": {"id": 10, "type": "private"}, "text": f"/start {first_code}", "date": time.time()}}
        ]):
            res = telegram.find_chat(tok, first_code)
            self.assertFalse(res["ok"])
            self.assertIn("사용된", res["error"])

    def test_find_chat_pages_through_foreign_updates_with_offset(self):
        tok = "111222333:AA" + "e" * 33
        code = telegram.new_start_code()
        page1 = [{"update_id": i, "message": {"chat": {"id": 900 + i, "type": "private"}, "text": "spam"}} for i in range(1, 101)]
        page2 = [{"update_id": i, "message": {"chat": {"id": 900 + i, "type": "private"}, "text": "more spam"}} for i in range(101, 201)]
        page3 = [{"update_id": 201, "message": {"chat": {"id": 4242, "type": "private"}, "text": f"/start {code}", "date": time.time()}}]
        offsets = []

        def fake_call(method, **kw):
            self.assertEqual(method, "getUpdates")
            off = kw.get("offset")
            offsets.append(off)
            if off is None:
                return page1
            if off == 101:
                return page2
            if off == 201:
                return page3
            return []

        with mock.patch.object(telegram, "call", side_effect=fake_call):
            r = telegram.find_chat(tok, code)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["chat_id"], "4242")
        self.assertEqual(offsets, [None, 101, 201])

    def test_poller_advances_offset_and_drops_updates_when_notify_not_telegram(self):
        tok = "111222333:AA" + "f" * 33
        settings.set_secret("telegram_token", tok)
        settings.update(telegram_chat_id="100", notify="email")
        seen_btn, seen_rep, offsets = [], [], []
        poller = telegram.Poller(
            on_button=lambda d: seen_btn.append(d) or "ok",
            log=lambda m: None,
            on_reply=lambda mid, txt: seen_rep.append((mid, txt)) or "ok",
        )
        queued = [
            {"update_id": 50, "callback_query": {"id": "c1", "from": {"id": 100},
                                                 "message": {"message_id": 1, "chat": {"id": 100}}, "data": "done:E1"}},
            {"update_id": 51, "message": {"chat": {"id": 100}, "from": {"id": 100},
                                          "reply_to_message": {"message_id": 2}, "text": "hi"}},
        ]

        def fake_call(method, **kw):
            offsets.append(kw.get("offset"))
            return queued if kw.get("offset") is None else []

        with mock.patch.object(telegram, "call", side_effect=fake_call):
            self.assertFalse(poller.poll_once())
            self.assertEqual(poller.offset, 52)
            self.assertEqual(seen_btn, [])
            self.assertEqual(seen_rep, [])
            settings.update(notify="telegram")
            self.assertTrue(poller.poll_once())
            self.assertEqual(offsets, [None, 52])

    def test_message_map_bound_to_bot_and_chat_and_checks_item_state(self):
        dl = (ep_model.today_kst() + timedelta(days=5)).isoformat()
        owner_id = ep_cli.call(["add", "--agent", "gemini", "--json",
                                json.dumps({"title": "Ask Item", "org": "O", "url": "https://forms.gle/a1", "deadline": dl})])["results"][0]["id"]
        ep_cli.call(["verify", owner_id, "--agent", "codex", "--lane", "form"])
        ep_cli.call(["owner", owner_id, "--agent", "codex", "--question", "Question?"])

        manual_id = ep_cli.call(["add", "--agent", "gemini", "--json",
                                 json.dumps({"title": "Manual Item", "org": "O", "url": "https://forms.gle/m9", "deadline": dl})])["results"][0]["id"]
        ep_cli.call(["manual", manual_id, "--agent", "codex", "--need", "login", "--reason", "login"])

        settings.set_secret("telegram_token", "11111111:AA" + "a" * 33)
        settings.update(telegram_bot="bot_one", telegram_bot_id="11111111", telegram_chat_id="500", notify="telegram")
        notify.remember(10, owner_id, "owner")
        notify.remember(11, manual_id, "manual")

        # Move owner_id out of 'owner' into 'manual': reply & retry/skip from old 'owner' message must fail!
        db = ep_store.connect()
        try:
            ep_store.update_item(db, owner_id, state="manual", need="other", reason="changed")
        finally:
            db.close()
        with self.assertRaises(RuntimeError):
            notify.on_reply(10, "my answer", chat_id="500")
        with self.assertRaises(RuntimeError):
            notify.on_button(f"retry:{owner_id}", message_id=10, chat_id="500")
        with self.assertRaises(RuntimeError):
            notify.on_button(f"skip:{owner_id}", message_id=10, chat_id="500")

        # Restore owner_id to 'owner' -> reply succeeds and transitions to 'new'
        db = ep_store.connect()
        try:
            ep_store.update_item(db, owner_id, state="owner", question="Question?")
        finally:
            db.close()
        self.assertIn("다시 시도", notify.on_reply(10, "answer text", chat_id="500"))

        # Move manual_id to 'excluded' -> pressing 'done' button on message 11 must fail!
        db = ep_store.connect()
        try:
            ep_store.update_item(db, manual_id, state="excluded", reason="expired")
        finally:
            db.close()
        with self.assertRaises(RuntimeError):
            notify.on_button(f"done:{manual_id}", message_id=11, chat_id="500")

        # Restore manual_id to 'manual', then change chat_id -> map must be dropped on disk!
        db = ep_store.connect()
        try:
            ep_store.update_item(db, manual_id, state="manual", need="login")
        finally:
            db.close()
        settings.update(telegram_chat_id="999")
        with self.assertRaises(RuntimeError):
            notify.on_button(f"done:{manual_id}", message_id=11, chat_id="999")
        self.assertFalse(os.path.exists(notify._map_path()))

        # Re-remember under chat 999, then change bot -> map must be dropped!
        notify.remember(12, manual_id, "manual")
        self.assertTrue(os.path.exists(notify._map_path()))
        settings.set_secret("telegram_token", "22222222:BB" + "b" * 33)
        settings.update(telegram_bot="bot_two", telegram_bot_id="22222222")
        with self.assertRaises(RuntimeError):
            notify.on_button(f"done:{manual_id}", message_id=12, chat_id="999")
        self.assertFalse(os.path.exists(notify._map_path()))


class TestC4AndC8SetupRunsAndScrubbing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2c-setup-")
        self.saved_env = {k: os.environ.get(k) for k in ("EP_APP_DIR", "EP_DATA_DIR")}
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        paths.ensure_dirs()
        server.LOG_LINES.clear()

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_codex_setup_command_has_all_hardening_flags(self):
        with mock.patch.object(deps, "cli_command", return_value=["codex.exe"]):
            for mod in (tgbot, mailbot):
                cmd = mod._command("codex", r"C:\work\task.md", r"C:\work")
                self.assertIn("features.hooks=false", cmd)
                self.assertIn("--ignore-user-config", cmd)
                self.assertIn("project_doc_max_bytes=0", cmd)
                disabled = [cmd[i + 1] for i, x in enumerate(cmd[:-1]) if x == "--disable"]
                self.assertIn("apps", disabled)
                self.assertIn("plugins", disabled)

    def test_streaming_scrub_and_failure_reason_scrub_and_16_letter_words_kept(self):
        tok = "1234567890:AAH" + "z" * 32
        spaced_pw = "abcd efgh ijkl mnop"
        known_pw = "qrstuvwxyzabcdef"
        word16_a = "acknowledgements"
        word16_b = "characterization"
        log_path = os.path.join(paths.logs_dir(), "email-app-password.log")
        mid_run_snapshots = []

        def fake_mail_cmd(agent, task_file, workdir):
            code = (
                "import json, re, sys, time; "
                f"print('word={word16_a} tok={tok} spaced={spaced_pw} raw={known_pw} other={word16_b}', flush=True); "
                "time.sleep(0.35); "
                "t = open(sys.argv[1], encoding='utf-8').read(); "
                "f = re.search(r'[Rr]esult file `([^`]+)`', t).group(1); "
                f"json.dump({{'ok': False, 'password': '{known_pw}', 'reason': 'failed after {tok} and {spaced_pw} and {known_pw} ({word16_a})'}}, open(f, 'w'))"
            )
            return [sys.executable, "-c", code, task_file]

        def spy_report(pct, text):
            if pct > 10 and os.path.isfile(log_path):
                with open(log_path, encoding="utf-8") as f:
                    mid_run_snapshots.append(f.read())

        real_sleep = time.sleep
        with mock.patch.object(mailbot, "agents", return_value=["codex"]), \
                mock.patch.object(mailbot, "_command", side_effect=fake_mail_cmd), \
                mock.patch.object(mailbot, "close_google_tabs", return_value=True), \
                mock.patch.object(logins, "logged_in_dirs", return_value=[logins.account_dir("codex", 1)]), \
                mock.patch.object(mailbot.time, "sleep", side_effect=lambda _s: real_sleep(0.05)):
            server.run_task("mailbot", lambda r: mailbot.run(lambda pct, txt: (r(pct, txt), spy_report(pct, txt)), "codex", "me@gmail.com"))
            for _ in range(80):
                if (server.TASKS.get("mailbot") or {}).get("state") != "running":
                    break
                real_sleep(0.05)

        # Mid-run snapshot (while child was still running!) already had token and spaced password scrubbed
        self.assertTrue(mid_run_snapshots)
        for snap in mid_run_snapshots:
            self.assertNotIn(tok, snap)
            self.assertNotIn(spaced_pw, snap)
            self.assertIn(word16_a, snap)
            self.assertIn(word16_b, snap)

        # Final log has actual known 16-char password scrubbed too, while keeping normal 16-letter English words!
        with open(log_path, encoding="utf-8") as f:
            final_log = f.read()
        self.assertNotIn(tok, final_log)
        self.assertNotIn(spaced_pw, final_log)
        self.assertNotIn(known_pw, final_log)
        self.assertIn(word16_a, final_log)
        self.assertIn(word16_b, final_log)

        # app.log and task error also scrubbed free-text failure reason without blanking 16-letter words
        task_err = server.TASKS["mailbot"]["error"]
        with open(os.path.join(paths.logs_dir(), "app.log"), encoding="utf-8") as f:
            app_log = f.read()
        for target in (task_err, app_log):
            self.assertNotIn(tok, target)
            self.assertNotIn(spaced_pw, target)
            self.assertNotIn(known_pw, target)
        self.assertIn(word16_a, app_log)

        # Also verify tgbot failure reason scrubbing in app.log and 16-letter words preserved in telegram-bot.log
        tg_log_path = os.path.join(paths.logs_dir(), "telegram-bot.log")

        def fake_tg_cmd(agent, task_file, workdir):
            code = (
                "import json, re, sys; "
                f"print('tglog {word16_a} {tok} {spaced_pw}', flush=True); "
                "t = open(sys.argv[1], encoding='utf-8').read(); "
                "f = re.search(r'result file `([^`]+)`', t).group(1); "
                f"json.dump({{'ok': False, 'reason': 'botfather failed {tok} {spaced_pw} {word16_b}'}}, open(f, 'w'))"
            )
            return [sys.executable, "-c", code, task_file]

        with mock.patch.object(tgbot, "agents", return_value=["codex"]), \
                mock.patch.object(tgbot, "_command", side_effect=fake_tg_cmd), \
                mock.patch.object(tgbot, "clear_web_data", return_value=True), \
                mock.patch.object(logins, "logged_in_dirs", return_value=[logins.account_dir("codex", 1)]):
            server.run_task("tgbot", lambda r: tgbot.run(r, "codex", "내 봇", ""))
            for _ in range(80):
                if (server.TASKS.get("tgbot") or {}).get("state") != "running":
                    break
                real_sleep(0.05)

        with open(tg_log_path, encoding="utf-8") as f:
            tg_log = f.read()
        self.assertNotIn(tok, tg_log)
        self.assertNotIn(spaced_pw, tg_log)
        self.assertIn(word16_a, tg_log)
        with open(os.path.join(paths.logs_dir(), "app.log"), encoding="utf-8") as f:
            app_log2 = f.read()
        self.assertNotIn(tok, app_log2)
        self.assertNotIn(spaced_pw, app_log2)
        self.assertIn(word16_b, app_log2)
        self.assertEqual(
            tgbot.scrub_text("nbsp aaaa\u00a0bbbb\u00a0cccc\u00a0dddd and responsibilities"),
            "nbsp [hidden] and responsibilities",
        )

    def test_tgbot_cancel_returns_cleanup_status_and_ui_checks_it(self):
        with mock.patch.object(tgbot, "clear_web_data", return_value=False):
            r_fail = server.handle("POST", "/api/tgbot/cancel", {}, {})
        self.assertFalse(r_fail["ok"])
        self.assertIn("지우지 못했습니다", r_fail["error"])

        with mock.patch.object(tgbot, "clear_web_data", return_value=True):
            r_ok = server.handle("POST", "/api/tgbot/cancel", {}, {})
        self.assertTrue(r_ok["ok"])
        self.assertIn("지웠습니다", r_ok["message"])

        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        cancel_block = js.split('t.id === "tg-ai-cancel"', 1)[1].split("if (t.id ===", 1)[0]
        self.assertIn("r.ok", cancel_block)
        self.assertIn("toast(", cancel_block)


class TestC5MailClipboardConfirm(unittest.TestCase):
    def test_mail_clip_masks_and_waits_for_confirm(self):
        pw = "mnopqrstuvwxyzab"
        calls = []
        server._CLIP_TRIED[0] = ""
        server._MAIL_CLIP.update(pw="", masked="")
        with mock.patch.object(mailer, "password_in_clipboard", return_value=pw), \
                mock.patch.object(mailer, "connect", side_effect=lambda a, p: calls.append((a, p)) or {"ok": True}):
            r = server.handle("GET", "/api/email/clip", {"address": ["user@gmail.com"]}, {})
            self.assertTrue(r["ok"] and r["found"] and r["pending"])
            self.assertEqual(r["masked"], "mn** **** **** **ab")
            self.assertNotIn(pw, json.dumps(r))
            self.assertEqual(calls, [])

            # Wrong masked value rejected
            bad = server.handle("POST", "/api/email/confirm", {}, {"address": "user@gmail.com", "masked": "xx** **** **** **yy"})
            self.assertFalse(bad["ok"])
            self.assertEqual(calls, [])

            # Correct confirm connects
            ok = server.handle("POST", "/api/email/confirm", {}, {"address": "user@gmail.com", "masked": r["masked"]})
            self.assertTrue(ok["ok"])
            self.assertEqual(calls, [("user@gmail.com", pw)])

        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("/api/email/confirm", js)
        self.assertIn('id="em-confirm"', js)


class TestC6UninstallErrorUI(unittest.TestCase):
    def test_uninstall_api_refusal_and_ui_error_toast(self):
        with mock.patch.object(paths, "portable", return_value=True):
            r = server.handle("POST", "/api/uninstall", {}, {})
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])

        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        block = js.split('t.id === "uninstall"', 1)[1].split('if (t.id === "open-folder"', 1)[0]
        self.assertIn("const r = await api(\"/api/uninstall\"", block)
        self.assertIn("if (!r || !r.ok)", block)
        self.assertLess(block.index("toast("), block.index("삭제했습니다"))


class TestC7ServerLimits(unittest.TestCase, HttpHelperMixin):
    def test_socket_timeout_body_size_cap_and_chunked_file_streaming(self):
        self.assertEqual(server.Handler.timeout, 15)
        srv = server.serve()
        port = srv.server_address[1]
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))

        # 1) Oversized JSON body (> 2 MB) rejected with 413
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.putrequest("POST", "/api/profile")
            conn.putheader("Host", f"127.0.0.1:{port}")
            conn.putheader("X-EP-Token", server.TOKEN)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(server.MAX_JSON_BYTES + 100))
            conn.endheaders()
            resp = conn.getresponse()
            self.assertEqual(resp.status, 413)
        finally:
            conn.close()

        # Upload route keeps its own larger cap (30 MB > 2 MB -> not 413 for a 10-byte body, and > 30 MB -> 413)
        self.assertGreater(server.UPLOAD_ROUTES["/api/upload"], server.MAX_JSON_BYTES)
        conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn2.putrequest("POST", "/api/upload")
            conn2.putheader("Host", f"127.0.0.1:{port}")
            conn2.putheader("X-EP-Token", server.TOKEN)
            conn2.putheader("Content-Length", str(server.UPLOAD_ROUTES["/api/upload"] + 1))
            conn2.endheaders()
            self.assertEqual(conn2.getresponse().status, 413)
        finally:
            conn2.close()

        # 2) File response streamed in chunks instead of whole-file read()
        big_dir = tempfile.mkdtemp(prefix="ep-r2c-stream-")
        self.addCleanup(shutil.rmtree, big_dir, True)
        big_file = os.path.join(big_dir, "work.png")
        payload = b"\x89PNG\r\n\x1a\n" + (b"ABCD" * (server.STREAM_CHUNK))  # > 4 chunks (~256 KB)
        with open(big_file, "wb") as f:
            f.write(payload)

        read_sizes = []
        real_open = open

        class SpyFile:
            def __init__(self, fobj):
                self._f = fobj

            def read(self, n=-1):
                read_sizes.append(n)
                return self._f.read(n)

            def fileno(self):
                return self._f.fileno()

            def close(self):
                return self._f.close()

        def spy_open(file, mode="r", *a, **kw):
            fobj = real_open(file, mode, *a, **kw)
            if file == big_file and "b" in mode:
                return SpyFile(fobj)
            return fobj

        with mock.patch.object(engine, "contest_file", return_value=big_file), \
                mock.patch("builtins.open", side_effect=spy_open):
            st, hdrs, got = self.http_req(port, "GET", f"/api/contest/file?id=C1&name=work.png&t={server.TOKEN}")
        self.assertEqual(st, 200)
        self.assertEqual(got, payload)
        self.assertGreaterEqual(len(read_sizes), 4)
        self.assertTrue(all(0 < n <= server.STREAM_CHUNK for n in read_sizes), read_sizes)

    def test_socket_read_timeout_and_thread_cap(self):
        # Verify short socket timeout closes stalled connection cleanly
        with mock.patch.object(server.Handler, "timeout", 0.3):
            srv = server.serve()
            port = srv.server_address[1]
            self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                s.sendall(b"POST /api/heartbeat HTTP/1.1\r\nHost: 127.0.0.1\r\n")
                time.sleep(0.55)
                # Server remains responsive after stalled client times out
                st, _, _ = self.http_req(port, "POST", "/api/heartbeat", {}, {"X-EP-Token": server.TOKEN})
                self.assertEqual(st, 200)
            finally:
                s.close()

        # Verify concurrent handler threads are capped
        active = 0
        peak = 0
        lock = threading.Lock()
        release = threading.Event()
        four_in = threading.Event()

        class CappedServer(server.BoundedHTTPServer):
            max_threads = 4

        orig_handle = server.handle

        def slow_handle(method, path, q, body):
            nonlocal active, peak
            if path == "/api/heartbeat":
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active >= 4:
                        four_in.set()
                release.wait(timeout=5)
                with lock:
                    active -= 1
                return {"ok": True}
            return orig_handle(method, path, q, body)

        srv2 = CappedServer(("127.0.0.1", 0), server.Handler)
        port2 = srv2.server_address[1]
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv2.shutdown(), srv2.server_close()))

        results = []
        with mock.patch.object(server, "handle", side_effect=slow_handle):
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        self.http_req(port2, "POST", "/api/heartbeat", {}, {"X-EP-Token": server.TOKEN})[0]
                    )
                )
                for _ in range(10)
            ]
            for t in threads:
                t.start()
            self.assertTrue(four_in.wait(timeout=5))
            time.sleep(0.15)  # give excess connections time to pile up if uncapped
            self.assertLessEqual(peak, 4)
            release.set()
            for t in threads:
                t.join(timeout=10)
        self.assertEqual(results, [200] * 10)
        self.assertEqual(peak, 4)


if __name__ == "__main__":
    unittest.main()
