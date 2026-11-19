"""Round 3 group C security tests (C1-C6). Pure unittest; no network, no real Chrome, no AI CLI."""
import http.client
import json
import os
import re
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

_BOOT_TMP = tempfile.mkdtemp(prefix="ep-r3c-")
os.environ["EP_APP_DIR"] = _BOOT_TMP
os.environ["EP_DATA_DIR"] = os.path.join(_BOOT_TMP, "data")
os.environ["EP_OWNER_DIR"] = os.path.join(_BOOT_TMP, "data", "board")
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import (  # noqa: E402
    deps,
    engine,
    logins,
    mailbot,
    mailer,
    main as app_main,
    paths,
    secure,
    server,
    settings,
    telegram,
    tgbot,
)
from ep import cli as ep_cli, config as ep_config, model as ep_model, store as ep_store  # noqa: E402


def tearDownModule():
    shutil.rmtree(_BOOT_TMP, ignore_errors=True)


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
                return r.status, dict(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, dict(e.headers.items()), e.read()
            finally:
                e.close()


class TestC1LaunchTtlSessionAndPerInstallRunFile(unittest.TestCase, HttpHelperMixin):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        cls.srv = server.serve()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_launch_code_ttl_is_five_minutes(self):
        self.assertEqual(server.LAUNCH_TTL, 300)
        # Code issued 240 s ago (> old 60 s, < 300 s) must still succeed
        code_ok = server.new_launch_code()
        server._LAUNCH_CODES[code_ok] = time.time() - 240
        st, _, raw = self.http_req(self.port, "POST", "/api/session", {"code": code_ok})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw)["token"], server.TOKEN)

        # Code issued 301 s ago (> 300 s) must fail
        code_exp = server.new_launch_code()
        server._LAUNCH_CODES[code_exp] = time.time() - 301
        st2, _, _ = self.http_req(self.port, "POST", "/api/session", {"code": code_exp})
        self.assertEqual(st2, 403)

    def test_per_installation_run_json_isolates_installed_and_portable_builds(self):
        local_tmp = tempfile.mkdtemp(prefix="ep-r3c-local-")
        port_a = tempfile.mkdtemp(prefix="ep-r3c-porta-")
        port_b = tempfile.mkdtemp(prefix="ep-r3c-portb-")
        self.addCleanup(shutil.rmtree, local_tmp, True)
        self.addCleanup(shutil.rmtree, port_a, True)
        self.addCleanup(shutil.rmtree, port_b, True)

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": local_tmp}, clear=False):
            env_no_override = {k: v for k, v in os.environ.items() if k != "EP_APP_DIR"}
            with mock.patch.dict(os.environ, env_no_override, clear=True):
                with mock.patch.object(paths, "portable", return_value=False):
                    rf_installed = app_main.run_file_path()
                with mock.patch.object(paths, "portable", return_value=True), \
                        mock.patch.object(sys, "executable", os.path.join(port_a, "EntryPilot.exe")):
                    rf_portable_a = app_main.run_file_path()
                with mock.patch.object(paths, "portable", return_value=True), \
                        mock.patch.object(sys, "executable", os.path.join(port_b, "EntryPilot.exe")):
                    rf_portable_b = app_main.run_file_path()

        want_dir = os.path.normcase(os.path.join(local_tmp, "EntryPilot"))
        for rf in (rf_installed, rf_portable_a, rf_portable_b):
            self.assertEqual(os.path.normcase(os.path.dirname(rf)), want_dir)
            self.assertRegex(os.path.basename(rf), r"^run-[0-9a-f]{8}\.json$")
        self.assertEqual(len({rf_installed, rf_portable_a, rf_portable_b}), 3)

        # Case / trailing slash differences for the same folder map to the exact same file
        same_upper = app_main.run_file_path(port_a.upper() + "\\")
        same_lower = app_main.run_file_path(port_a.lower())
        self.assertEqual(os.path.basename(same_upper), os.path.basename(same_lower))

    def test_ui_session_exchange_retry_storage_and_reopen_guidance(self):
        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()

        # 30 s retry window with backoff
        self.assertIn("SESSION_RETRY_MS = 30000", js)
        # Token persisted per-window in sessionStorage
        self.assertIn("sessionStorage.getItem", js)
        self.assertIn("sessionStorage.setItem", js)
        # Rejected promise is never cached
        self.assertIn("sessionReady = null", js)
        # URL ?c= is stripped ONLY after TOKEN = j.token (not in a finally block before success)
        ensure_fn = js.split("async function ensureSession()", 1)[1].split("async function api(", 1)[0]
        self.assertLess(ensure_fn.index("TOKEN = j.token"), ensure_fn.index("history.replaceState"))
        finally_tail = ensure_fn.split(".finally(", 1)[1]
        self.assertNotIn("history.replaceState", finally_tail)
        # Clear message + "다시 열기" button + instruction to start Entry Pilot again (no /api/open-window call)
        self.assertIn('id="reopen-window"', js)
        self.assertIn("다시 열기", js)
        self.assertIn("Entry Pilot", js)
        self.assertNotIn("/api/open-window", js)


class TestC2TelegramDeferredStartCodeAndWaiting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3c-tg-")
        self.saved_env = {k: os.environ.get(k) for k in ("EP_APP_DIR", "EP_DATA_DIR", "EP_OWNER_DIR")}
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "data", "board")
        paths.ensure_dirs()
        engine.ensure_initialized()
        telegram._START_CODES.clear()
        telegram._USED_CODES.clear()
        server.TASKS.clear()
        settings.save(dict(settings.DEFAULTS))

    def tearDown(self):
        telegram._START_CODES.clear()
        telegram._USED_CODES.clear()
        server.TASKS.clear()
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_code_issued_after_bot_created_waiting_state_and_expiry_renewal(self):
        tok = "777888999:AA" + "m" * 33
        codes_during_agent = []
        saved_start_during_agent = []

        def fake_cmd(agent, task_file, workdir):
            # Capture start-code state at the moment the agent command is built/launched
            codes_during_agent.append(dict(telegram._START_CODES))
            saved_start_during_agent.append(settings.load().get("telegram_start") or "")
            script = (
                "import json, re, sys; t = open(sys.argv[1], encoding='utf-8').read(); "
                "f = re.search(r'result file `([^`]+)`', t).group(1); "
                f"json.dump({{'ok': True, 'token': '{tok}', 'username': 'r3_wait_bot', 'logout': True}}, open(f, 'w'))"
            )
            return [sys.executable, "-c", script, task_file]

        reports = []
        with mock.patch.object(tgbot, "agents", return_value=["codex"]), \
                mock.patch.object(tgbot, "_command", side_effect=fake_cmd), \
                mock.patch.object(tgbot, "clear_web_data", return_value=True), \
                mock.patch.object(logins, "logged_in_dirs", return_value=[logins.account_dir("codex", 1)]), \
                mock.patch.object(telegram, "verify", return_value={"ok": True, "id": "777888999", "username": "r3_wait_bot"}), \
                mock.patch.object(telegram, "find_chat", return_value={"ok": False, "error": "no msg"}):
            out = tgbot.run(lambda pct, txt: reports.append((pct, txt)), "codex", "내 봇", "")

        # 1) Start code was NOT issued before the agent run
        self.assertEqual(codes_during_agent, [{}])
        self.assertEqual(saved_start_during_agent, [""])

        # 2) Issued right after bot creation; reports "연결 대기" (not success) when no chat found yet
        self.assertTrue(out["ok"])
        self.assertTrue(out["waiting"])
        self.assertFalse(out["connected"])
        code1 = settings.load()["telegram_start"]
        self.assertTrue(code1)
        self.assertIn(code1, telegram._START_CODES)
        self.assertEqual(out["bot_url"], f"https://t.me/r3_wait_bot?start={code1}")
        self.assertIn("연결 대기", reports[-1][1])
        self.assertNotIn("봇 연결됨", reports[-1][1])

        # Server task records state == "waiting" rather than "done"
        server.run_task("tgbot", lambda r: out)
        for _ in range(40):
            if (server.TASKS.get("tgbot") or {}).get("state") != "running":
                break
            time.sleep(0.02)
        self.assertEqual(server.TASKS["tgbot"]["state"], "waiting")

        # 3) Polling from UI while code is valid returns current bot_url
        with mock.patch.object(telegram, "call", return_value=[]):
            p1 = server.handle("POST", "/api/telegram/find", {}, {"renew": True})
        self.assertFalse(p1["ok"])
        self.assertTrue(p1["waiting"])
        self.assertFalse(p1.get("renewed"))
        self.assertEqual(p1["bot_url"], f"https://t.me/r3_wait_bot?start={code1}")

        # 4) Expiry while waiting -> issues a new start code and updates bot_url
        exp_at = time.time() - (telegram.START_TTL + 5)
        settings.update(telegram_start_at=exp_at)
        telegram._START_CODES[code1] = exp_at

        with mock.patch.object(telegram, "call", return_value=[
            {"update_id": 1, "message": {"chat": {"id": 9191, "type": "private"}, "text": f"/start {code1}", "date": time.time()}}
        ]):
            p_exp = server.handle("POST", "/api/telegram/find", {}, {"renew": True})
        self.assertFalse(p_exp["ok"])
        self.assertTrue(p_exp["expired"])
        self.assertTrue(p_exp["renewed"])
        code2 = settings.load()["telegram_start"]
        self.assertTrue(code2)
        self.assertNotEqual(code2, code1)
        self.assertEqual(p_exp["bot_url"], f"https://t.me/r3_wait_bot?start={code2}")
        self.assertEqual(settings.load()["telegram_chat_id"], "")

        # 5) Pressing Start on the renewed link connects and completes the waiting task
        with mock.patch.object(telegram, "call", return_value=[
            {"update_id": 2, "message": {"chat": {"id": 9191, "type": "private"}, "text": f"/start {code2}", "date": time.time()}}
        ]), mock.patch.object(telegram, "send"):
            p_ok = server.handle("POST", "/api/telegram/find", {}, {"renew": True})
        self.assertTrue(p_ok["ok"])
        self.assertEqual(settings.load()["telegram_chat_id"], "9191")
        self.assertEqual(settings.load()["telegram_start"], "")
        self.assertEqual(server.TASKS["tgbot"]["state"], "done")

        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("연결 대기", js)
        self.assertIn('id="tg-bot-link"', js)
        self.assertIn('api("/api/telegram/find", { renew: true })', js)


class TestC3SettingsSubKeyMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3c-set-")
        self.prev = os.environ.get("EP_APP_DIR")
        os.environ["EP_APP_DIR"] = self.tmp
        paths.ensure_dirs()
        settings.save(dict(settings.DEFAULTS))

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("EP_APP_DIR", None)
        else:
            os.environ["EP_APP_DIR"] = self.prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_windows_merge_subkeys_of_agents_modes_and_interests(self):
        barrier = threading.Barrier(3)
        errors = []

        def worker(payload):
            try:
                barrier.wait(timeout=5)
                r = server.handle("POST", "/api/settings", {}, payload)
                if not r.get("ok"):
                    errors.append(r)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=({"agents": {"claude": True}, "interests": {"ai": True}},)),
            threading.Thread(target=worker, args=({"agents": {"codex": True}, "modes": {"contests": True}},)),
            threading.Thread(target=worker, args=({"agents": {"gemini": True}, "interests": {"design": True}},)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        s = settings.load()
        self.assertEqual(s["agents"], {"claude": True, "codex": True, "gemini": True})
        self.assertEqual(s["modes"], {"events": True, "contests": True})
        self.assertEqual(s["interests"], {"ai": True, "design": True})

        # Toggling off a single agent sub-key leaves the others untouched
        server.handle("POST", "/api/settings", {}, {"agents": {"codex": False}})
        self.assertEqual(settings.load()["agents"], {"claude": True, "codex": False, "gemini": True})

        # Plain string interests continues to work
        server.handle("POST", "/api/settings", {}, {"interests": "공모전, 경품"})
        self.assertEqual(settings.load()["interests"], "공모전, 경품")

    def test_ui_sends_only_changed_subkeys(self):
        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertNotIn("{ ...S.settings.agents", js)
        self.assertIn("agents: { [d.agent]: !S.settings.agents[d.agent] }", js)
        self.assertIn("modes: { [d.mode]: nextVal }", js)
        self.assertIn("agents: { [t.dataset.agentSw]: t.checked }", js)


class TestC4ExcelConfirmVersionHashAndThrottle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3c-xlsx-")
        self.saved_env = {k: os.environ.get(k) for k in ("EP_APP_DIR", "EP_DATA_DIR", "EP_OWNER_DIR")}
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "data", "board")
        server._XLSX_SYNC_STATE.clear()
        paths.ensure_dirs()
        engine.ensure_initialized()

    def tearDown(self):
        server._XLSX_SYNC_STATE.clear()
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_xlsx(self, manual_rows, owner_rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = ep_config.XLSX_SHEET_MANUAL
        ws.append([ep_config.XLSX_COL_ID, "제목", ep_config.XLSX_COL_DONE])
        for iid, val in manual_rows:
            ws.append([iid, f"Title {iid}", val])
        wo = wb.create_sheet(ep_config.XLSX_SHEET_OWNER)
        wo.append([ep_config.XLSX_COL_ID, "질문", ep_config.XLSX_COL_ANSWER])
        for iid, val in owner_rows:
            wo.append([iid, "Question?", val])
        path = ep_config.owner_xlsx_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wb.save(path)
        wb.close()
        return path

    def _seed_items(self):
        dl = (ep_model.today_kst() + timedelta(days=7)).isoformat()
        ids = []
        for idx in range(3):
            iid = ep_cli.call([
                "add", "--agent", "gemini", "--json",
                json.dumps({"title": f"Item {idx}", "org": "Org", "url": f"https://forms.gle/r3x{idx}", "deadline": dl}),
            ])["results"][0]["id"]
            ids.append(iid)
        ep_cli.call(["manual", ids[0], "--agent", "codex", "--need", "login", "--reason", "login"])
        ep_cli.call(["manual", ids[1], "--agent", "codex", "--need", "app", "--reason", "app"])
        ep_cli.call(["verify", ids[2], "--agent", "codex", "--lane", "form"])
        ep_cli.call(["owner", ids[2], "--agent", "codex", "--question", "Pick option?"])
        return ids[0], ids[1], ids[2]

    def test_version_hash_displayed_only_confirm_and_stale_refusal(self):
        m1, m2, o1 = self._seed_items()
        self._write_xlsx([(m1, "했음"), (m2, "안함")], [])

        # Initial /api/items syncs and returns m1, m2 with version=1 and hash
        resp = server.handle("GET", "/api/items", {"state": ["owner"]}, {})
        displayed = resp["xlsx_pending"]
        self.assertEqual([p["id"] for p in displayed], [m1, m2])
        for p in displayed:
            self.assertEqual(p["version"], 1)
            self.assertRegex(p["hash"], r"^[0-9a-f]{64}$")

        # Blind {"all": True} and missing version/hash are refused
        self.assertFalse(server.handle("POST", "/api/xlsx/confirm", {}, {"all": True})["ok"])
        self.assertFalse(server.handle("POST", "/api/xlsx/confirm", {}, {"id": m1})["ok"])

        # Queue a 3rd row (o1) AND modify m2 ("안함" -> "했음") AFTER the UI displayed [m1, m2]!
        self._write_xlsx([(m1, "했음"), (m2, "했음")], [(o1, "B안")])
        ep_cli.call(["sync-xlsx"])

        # "모두 확인" sends the displayed list [m1(v1,h1), m2(v1,h1_old)] — NOT o1!
        # m1 matches and applies; m2 changed after display -> refused with stale/refresh; o1 untouched!
        batch_req = [{"id": p["id"], "version": p["version"], "hash": p["hash"]} for p in displayed]
        r_batch = server.handle("POST", "/api/xlsx/confirm", {}, {"items": batch_req})
        self.assertFalse(r_batch["ok"])
        self.assertTrue(r_batch.get("stale"))
        self.assertTrue(r_batch.get("refresh"))
        self.assertEqual([x["id"] for x in r_batch["refused"]], [m2])

        db = ep_store.connect()
        try:
            self.assertEqual(ep_store.get_item(db, m1)["state"], "submitted")
            self.assertEqual(ep_store.get_item(db, m2)["state"], "manual")  # changed row was NOT applied!
            self.assertEqual(ep_store.get_item(db, o1)["state"], "owner")   # undisplayed row was NOT applied!
        finally:
            db.close()

        # Refreshed list now shows updated m2 (version 2) and o1 (version 1)
        refreshed = r_batch["items"]
        by_id = {p["id"]: p for p in refreshed}
        self.assertEqual(set(by_id.keys()), {m2, o1})
        self.assertEqual(by_id[m2]["version"], 2)
        self.assertNotEqual(by_id[m2]["hash"], displayed[1]["hash"])

        # Confirming ONLY displayed [m2(v2, new_hash)] leaves o1 in the queue
        r_m2 = server.handle("POST", "/api/xlsx/confirm", {}, {
            "items": [{"id": m2, "version": by_id[m2]["version"], "hash": by_id[m2]["hash"]}]
        })
        self.assertTrue(r_m2["ok"], r_m2)
        self.assertEqual(r_m2["count"], 1)
        db = ep_store.connect()
        try:
            self.assertEqual(ep_store.get_item(db, m2)["state"], "submitted")
            self.assertEqual(ep_store.get_item(db, o1)["state"], "owner")
        finally:
            db.close()

    def test_sync_xlsx_runs_at_most_once_per_60s_and_only_when_mtime_changed(self):
        m1, _, _ = self._seed_items()
        xlsx_path = self._write_xlsx([(m1, "했음")], [])
        st0 = os.stat(xlsx_path)

        orig_call = ep_cli.call
        sync_calls = []

        def spy_call(argv, *a, **kw):
            if argv == ["sync-xlsx"]:
                sync_calls.append(1)
            return orig_call(argv, *a, **kw)

        clock = [1000.0]
        with mock.patch.object(ep_cli, "call", side_effect=spy_call), \
                mock.patch.object(server.time, "monotonic", side_effect=lambda: clock[0]):
            # 1) First /api/items request -> runs sync-xlsx once
            server.handle("GET", "/api/items", {"state": ["owner"]}, {})
            self.assertEqual(len(sync_calls), 1)

            # 2) Repeated /api/items with unchanged mtime (both within 60 s and after 120 s) -> 0 new syncs
            clock[0] = 1010.0
            server.handle("GET", "/api/items", {"state": ["owner"]}, {})
            clock[0] = 1130.0
            server.handle("GET", "/api/items", {"state": ["owner"]}, {})
            self.assertEqual(len(sync_calls), 1)

            # 3) mtime changes after > 60 s since last sync -> runs immediately once
            os.utime(xlsx_path, (st0.st_atime, st0.st_mtime + 20.0))
            clock[0] = 1135.0
            server.handle("GET", "/api/items", {"state": ["owner"]}, {})
            self.assertEqual(len(sync_calls), 2)

            # 4) mtime changes again only 15 s later (< 60 s) -> throttled!
            os.utime(xlsx_path, (st0.st_atime, st0.st_mtime + 40.0))
            clock[0] = 1150.0
            server.handle("GET", "/api/items", {"state": ["owner"]}, {})
            self.assertEqual(len(sync_calls), 2)

            # 5) Once 60 s has elapsed since the 1135.0 sync -> runs once for the pending mtime change
            clock[0] = 1196.0
            server.handle("GET", "/api/items", {"state": ["owner"]}, {})
            self.assertEqual(len(sync_calls), 3)


class TestC5TotalRequestDeadline(unittest.TestCase, HttpHelperMixin):
    def test_slow_loris_headers_and_body_cut_off_by_total_request_deadline(self):
        self.assertEqual(server.REQUEST_DEADLINE, 30)
        self.assertEqual(server.Handler.request_deadline, 30)

        # Idle timeout (5.0 s) is much larger than per-chunk gap (0.08 s),
        # while total per-request deadline is 0.45 s.
        with mock.patch.object(server.Handler, "timeout", 5.0), \
                mock.patch.object(server.Handler, "request_deadline", 0.45):
            srv = server.serve()
            port = srv.server_address[1]
            self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))

            # 1) Trickle headers past total deadline -> disconnected within ~1 s (not 5 s!)
            t0 = time.monotonic()
            s1 = socket.create_connection(("127.0.0.1", port), timeout=3)
            try:
                s1.sendall(b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1\r\n")
                for i in range(15):
                    time.sleep(0.08)
                    try:
                        s1.sendall(f"X-Pad-{i}: a\r\n".encode("ascii"))
                    except OSError:
                        break
                s1.settimeout(1.0)
                try:
                    rem = s1.recv(1024)
                except OSError:
                    rem = b""
                elapsed1 = time.monotonic() - t0
                self.assertLess(elapsed1, 2.0)
                # Either empty EOF or 408 timeout response
                self.assertTrue(rem == b"" or b"408" in rem, rem)
            finally:
                s1.close()

            # 2) Send headers fast, then trickle JSON body past total deadline -> 408 / closed within ~1 s
            t1 = time.monotonic()
            s2 = socket.create_connection(("127.0.0.1", port), timeout=3)
            try:
                hdr = (
                    f"POST /api/heartbeat HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"X-EP-Token: {server.TOKEN}\r\nContent-Type: application/json\r\n"
                    "Content-Length: 500\r\n\r\n{"
                ).encode("utf-8")
                s2.sendall(hdr)
                got_resp = b""
                for _ in range(15):
                    time.sleep(0.08)
                    try:
                        s2.sendall(b" ")
                    except OSError:
                        break
                s2.settimeout(1.0)
                try:
                    got_resp = s2.recv(4096)
                except OSError:
                    got_resp = b""
                elapsed2 = time.monotonic() - t1
                self.assertLess(elapsed2, 2.0)
                self.assertTrue(b"408" in got_resp or got_resp == b"", got_resp)
            finally:
                s2.close()

            # 3) Normal request and HTTP/1.1 keep-alive still succeed cleanly
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request("POST", "/api/heartbeat", body=b"{}",
                             headers={"Host": f"127.0.0.1:{port}", "X-EP-Token": server.TOKEN, "Content-Type": "application/json"})
                r1 = conn.getresponse()
                r1.read()
                self.assertEqual(r1.status, 200)

                time.sleep(0.25)
                conn.request("POST", "/api/heartbeat", body=b"{}",
                             headers={"Host": f"127.0.0.1:{port}", "X-EP-Token": server.TOKEN, "Content-Type": "application/json"})
                r2 = conn.getresponse()
                r2.read()
                self.assertEqual(r2.status, 200)
            finally:
                conn.close()


class TestC6RandomAclResultFileAndStreamScrubbing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3c-c6-")
        self.saved_env = {k: os.environ.get(k) for k in ("EP_APP_DIR", "EP_DATA_DIR", "EP_OWNER_DIR")}
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "data", "board")
        paths.ensure_dirs()
        server.LOG_LINES.clear()

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_user_only_acl(self, folder: str):
        icacls = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "icacls.exe")
        r = subprocess.run([icacls, folder], capture_output=True, text=True,
                           encoding="mbcs", errors="replace", check=True)
        aces = secure._parse_icacls_aces(r.stdout, folder)
        self.assertEqual(len(aces), 1, r.stdout)
        self.assertTrue(secure._ace_is_sole_user_full(aces, secure.current_sid()), r.stdout)
        self.assertNotIn("everyone", r.stdout.lower())

    def test_random_result_filename_acl_protected_folder_and_finally_cleanup(self):
        seen_files = []
        tok = "555666777:AA" + "q" * 33
        pw = "zxcvbnmasdfghjkl"

        def fake_tg_cmd(agent, task_file, workdir):
            with open(task_file, encoding="utf-8") as f:
                txt = f.read()
            rf = re.search(r"result file `([^`]+)`", txt).group(1)
            seen_files.append(("tg", workdir, task_file, rf))
            code = f"import json; json.dump({{'ok': True, 'token': '{tok}', 'username': 'c6_bot', 'logout': True}}, open({rf!r}, 'w'))"
            return [sys.executable, "-c", code]

        with mock.patch.object(tgbot, "agents", return_value=["codex"]), \
                mock.patch.object(tgbot, "_command", side_effect=fake_tg_cmd), \
                mock.patch.object(tgbot, "clear_web_data", return_value=True), \
                mock.patch.object(logins, "logged_in_dirs", return_value=[logins.account_dir("codex", 1)]), \
                mock.patch.object(telegram, "verify", return_value={"ok": True, "id": "555666777", "username": "c6_bot"}), \
                mock.patch.object(telegram, "find_chat", return_value={"ok": False, "error": "wait"}):
            tgbot.run(lambda *a: None, "codex", "봇", "")

        def fake_mail_cmd(agent, task_file, workdir):
            with open(task_file, encoding="utf-8") as f:
                txt = f.read()
            rf = re.search(r"[Rr]esult file `([^`]+)`", txt).group(1)
            seen_files.append(("mail", workdir, task_file, rf))
            code = f"import json; json.dump({{'ok': True, 'password': '{pw}'}}, open({rf!r}, 'w'))"
            return [sys.executable, "-c", code]

        with mock.patch.object(mailbot, "agents", return_value=["codex"]), \
                mock.patch.object(mailbot, "_command", side_effect=fake_mail_cmd), \
                mock.patch.object(mailbot, "close_google_tabs", return_value=True), \
                mock.patch.object(logins, "logged_in_dirs", return_value=[logins.account_dir("codex", 1)]), \
                mock.patch.object(mailer, "connect", return_value={"ok": True}):
            mailbot.run(lambda *a: None, "codex", "me@gmail.com")

        self.assertEqual(len(seen_files), 2)
        for kind, workdir, task_file, rf in seen_files:
            self.assertNotEqual(os.path.basename(rf).lower(), "result.json", kind)
            self.assertRegex(os.path.basename(rf), r"^result-[0-9a-f]{32}\.json$", kind)
            self.assertRegex(os.path.basename(task_file), r"^task-[0-9a-f]{32}\.md$", kind)
            self._assert_user_only_acl(workdir)
            # Deleted in finally
            self.assertFalse(os.path.exists(rf))
            self.assertFalse(os.path.exists(task_file))
            self.assertFalse(os.path.exists(os.path.join(workdir, "result.json")))

    def test_stream_scrubs_unspaced_16_letter_mail_password_on_keyword_lines(self):
        unspaced1 = "qwertyuiopasdfgh"
        unspaced2 = "lkjhgfdsapoiuytr"
        unspaced3 = "mnbvcxzlkjhgfdsa"
        word16_ok = "acknowledgements"
        log_path = os.path.join(paths.logs_dir(), "email-app-password.log")
        mid_snapshots = []

        def fake_mail_cmd(agent, task_file, workdir):
            code = (
                "import sys, time; "
                f"print('Created App Password: {unspaced1}', flush=True); "
                f"print('앱 비밀번호는 {unspaced2}입니다', flush=True); "
                f"print('from https://myaccount.google.com/apppasswords got {unspaced3}', flush=True); "
                f"print('status line with {word16_ok} only', flush=True); "
                "time.sleep(0.3)"
            )
            return [sys.executable, "-c", code]

        real_sleep = time.sleep

        def spy_report(pct, text):
            if pct > 10 and os.path.isfile(log_path):
                with open(log_path, encoding="utf-8") as f:
                    mid_snapshots.append(f.read())

        with mock.patch.object(mailbot, "agents", return_value=["codex"]), \
                mock.patch.object(mailbot, "_command", side_effect=fake_mail_cmd), \
                mock.patch.object(mailbot, "close_google_tabs", return_value=True), \
                mock.patch.object(logins, "logged_in_dirs", return_value=[logins.account_dir("codex", 1)]), \
                mock.patch.object(mailbot.time, "sleep", side_effect=lambda _s: real_sleep(0.05)):
            with self.assertRaises(deps.StepError):
                mailbot.run(spy_report, "codex", "me@gmail.com")

        # Even while child process was still running (no result file ever written!),
        # none of the 3 unspaced passwords ever touched disk!
        self.assertTrue(mid_snapshots)
        for snap in mid_snapshots:
            self.assertNotIn(unspaced1, snap)
            self.assertNotIn(unspaced2, snap)
            self.assertNotIn(unspaced3, snap)

        with open(log_path, encoding="utf-8") as f:
            final_log = f.read()
        for secret_pw in (unspaced1, unspaced2, unspaced3):
            self.assertNotIn(secret_pw, final_log)
        self.assertIn(word16_ok, final_log)

        # server.log() scrubs unspaced 16-letter passwords on keyword lines while keeping 16-letter words elsewhere
        server.log(f"mailbot error on apppasswords: {unspaced1}")
        self.assertNotIn(unspaced1, server.LOG_LINES[-1])
        server.log(f"mailbot note: {word16_ok}")
        self.assertIn(word16_ok, server.LOG_LINES[-1])


if __name__ == "__main__":
    unittest.main()
