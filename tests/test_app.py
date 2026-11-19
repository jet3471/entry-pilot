"""App layer tests (no network, no windows): settings encryption, python shim, local server guards, profile, notify."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="ep-app-test-")
os.environ["EP_APP_DIR"] = _TMP

from app import notify, paths, pyshim, secure, server, settings  # noqa: E402


class Secure(unittest.TestCase):
    def test_roundtrip_and_masking(self):
        v = secure.protect("123:plain_secret_value-토큰")
        self.assertTrue(v.startswith("dpapi:"))
        self.assertNotIn("plain_secret_value", v)
        self.assertEqual(secure.unprotect(v), "123:plain_secret_value-토큰")
        settings.set_secret("telegram_token", "123:telegram_plain_value")
        with open(paths.settings_path(), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("telegram_plain_value", raw)
        self.assertEqual(settings.get_secret("telegram_token"), "123:telegram_plain_value")
        self.assertIs(settings.public()["telegram_token"], True)


class Shim(unittest.TestCase):
    def test_detects_python_calls(self):
        for argv in (["-X", "utf8", "a.py"], ["-m", "ep.chrome"], ["-c", "1"], ["-"], ["x.py"], ["--version"]):
            self.assertTrue(pyshim.looks_like_python_call(argv), argv)
        for argv in ([], ["--uninstall"]):
            self.assertFalse(pyshim.looks_like_python_call(argv), argv)

    def _run(self, argv, stdin=b""):
        code = ("import sys; sys.path.insert(0, %r); from app import pyshim; sys.exit(pyshim.run(%r))" % (ROOT, argv))
        return subprocess.run([sys.executable, "-c", code], input=stdin, capture_output=True, timeout=120)

    def test_forms(self):
        r = self._run(["-X", "utf8", "-c", "import sys; print('ok', sys.argv)"])
        self.assertIn(b"ok ['-c']", r.stdout)
        r = self._run(["-X", "utf8", "-"], b"print(6*7)")
        self.assertIn(b"42", r.stdout)
        r = self._run(["-c", "raise SystemExit(3)"])
        self.assertEqual(r.returncode, 3)
        f = os.path.join(_TMP, "s.py")
        open(f, "w").write("import sys\nprint('args', sys.argv[1:])\n")
        r = self._run([f, "a", "b"])
        self.assertIn(b"args ['a', 'b']", r.stdout)


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        cls.srv = server.serve()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, body=None, token=None, host=None):
        h = {"X-EP-Token": server.TOKEN if token is None else token, "Content-Type": "application/json"}
        if host:
            h["Host"] = host
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_guards(self):
        self.assertEqual(self.call("/api/heartbeat", {}, token="wrong")[0], 403)
        self.assertEqual(self.call("/api/heartbeat", {}, host="evil.example")[0], 403)
        self.assertEqual(self.call("/api/heartbeat", {})[0], 200)

    def test_profile_and_age(self):
        code, r = self.call("/api/profile", {"name": "홍길동", "birth": "2000.03.04", "sns": {"instagram": "me"}})
        self.assertTrue(r["ok"])
        p = json.load(open(os.path.join(paths.data_dir(), "profile.json"), encoding="utf-8"))
        self.assertEqual(p["name"], "홍길동")
        self.assertEqual(p["birth"], "2000-03-04")
        self.assertTrue(p["adult"])
        self.assertEqual(p["sns_handles"]["instagram"]["handle"], "me")
        _, g = self.call("/api/profile")
        self.assertEqual(g["sns"]["instagram"], "me")

    def test_settings_whitelist(self):
        _, r = self.call("/api/settings", {"agents": {"claude": True, "codex": False, "gemini": False},
                                           "setup_done": True, "telegram_token": "x"})
        s = settings.load()
        self.assertTrue(s["agents"]["claude"])
        self.assertFalse(s.get("setup_done"))          # not settable through this route
        self.assertNotEqual(s.get("telegram_token"), "x")


class Notify(unittest.TestCase):
    def test_text_and_buttons(self):
        it = {"id": "E12", "state": "manual", "title": "설문 이벤트", "need": "identity", "url": "https://x.example/e"}
        t = notify.describe(it)
        self.assertIn("본인인증", t)
        self.assertIn("https://x.example/e", t)
        self.assertEqual([b["callback_data"] for b in notify.buttons(it)[0]], ["done:E12", "skip:E12"])
        it["state"] = "owner"
        self.assertEqual([b["callback_data"] for b in notify.buttons(it)[0]], ["retry:E12", "skip:E12"])
        self.assertEqual(notify.on_button("noop:0"), "확인함")
        self.assertEqual(notify.on_button("bad"), "알 수 없는 버튼")


class CliPlaceholder(unittest.TestCase):
    """Claude Code's npm package first writes a tiny bin\\claude.exe placeholder; running it pops a 16-bit error."""

    def test_placeholder_is_not_installed(self):
        from app import deps
        d = os.path.join(deps.cli_prefix(), "node_modules", "@anthropic-ai", "claude-code")
        os.makedirs(os.path.join(d, "bin"), exist_ok=True)
        json.dump({"bin": {"claude": "bin/claude.exe"}}, open(os.path.join(d, "package.json"), "w"))
        exe = os.path.join(d, "bin", "claude.exe")
        open(exe, "wb").write(b"placeholder stub\n")
        self.assertEqual(deps.cli_command("claude"), [])
        with open(exe, "wb") as f:
            f.write(b"MZ" + b"\0" * (2 << 20))
        self.assertEqual(deps.cli_command("claude"), [os.path.normpath(exe)])

    def test_no_cli_runs_while_a_task_runs(self):
        from app import logins
        calls = []
        orig = logins.status
        logins.status = lambda a, i=1: calls.append(a) or {"installed": True, "logged_in": False}
        server.TASKS["install"] = {"state": "running"}
        try:
            server._login_cache.update(at=0.0, data={})
            server.login_states(force=True)
            self.assertEqual(calls, [])
            server.TASKS["install"] = {"state": "done"}
            server.login_states(force=True)
            self.assertTrue(calls)  # read again once the install is over
        finally:
            logins.status = orig
            server.TASKS.pop("install", None)



class Accounts(unittest.TestCase):
    """Several accounts per AI: folders, the count setting, and the one-after-another login windows."""

    def setUp(self):
        settings.save(dict(settings.DEFAULTS, agents={"claude": True, "codex": True, "gemini": False},
                           accounts={"claude": 2, "codex": 1}))

    def test_folders_and_counts(self):
        from app import logins
        self.assertTrue(logins.account_dir("claude", 1).endswith(os.path.join("logins", "claude")))
        self.assertTrue(logins.account_dir("claude", 2).endswith(os.path.join("logins", "claude-2")))
        self.assertEqual(logins.account_count("claude"), 2)
        self.assertEqual(logins.account_count("codex"), 1)
        self.assertEqual(logins.account_count("gemini"), 0)
        self.assertEqual(logins.agent_env("claude", 2)["CLAUDE_CONFIG_DIR"], logins.account_dir("claude", 2))

    def test_queue_opens_windows_in_order_and_retries_same_account(self):
        from app import logins
        state = {("claude", 1): False, ("claude", 2): False, ("codex", 1): True}
        emails = {1: "a@x", 2: "a@x"}  # account 2 first logs in with the same e-mail
        opened, reports = [], []

        class P:
            def wait(self):
                return 0

        def fake_open(agent, i, n):
            opened.append((agent, i, n))
            state[(agent, i)] = True
            if agent == "claude" and i == 2 and opened.count(("claude", 2, 2)) == 2:
                emails[2] = "b@x"  # second try: a different account
            return P()

        def fake_status(agent, i=1):
            ok = state[(agent, i)]
            return {"logged_in": ok, "email": emails.get(i, "") if agent == "claude" and ok else ""}

        def fake_logout(agent, i=1):
            state[(agent, i)] = False
            return {"ok": True}

        saved = logins.open_login, logins.status, logins.logout
        logins.open_login, logins.status, logins.logout = fake_open, fake_status, fake_logout
        try:
            logins.login_queue(lambda pct, text: reports.append(text))
        finally:
            logins.open_login, logins.status, logins.logout = saved
        self.assertEqual(opened, [("claude", 1, 2), ("claude", 2, 2), ("claude", 2, 2)])  # codex already logged in
        self.assertTrue(any("같은 계정" in r for r in reports))

    def test_settings_route_clamps_counts(self):
        r = server.handle("POST", "/api/settings", {}, {"accounts": {"claude": 9, "codex": 0, "gemini": 3, "x": 2}})
        self.assertEqual(r["settings"]["accounts"], {"claude": 5, "codex": 1})

    def test_engine_gets_every_account_folder(self):
        from app import engine, logins
        env = engine.engine_env()
        self.assertEqual(env["EP_CLAUDE_CONFIG_DIRS"].split(os.pathsep),
                         [logins.account_dir("claude", 1), logins.account_dir("claude", 2)])
        code = ("import sys, json; sys.path.insert(0, %r); from ep import config; "
                "print(json.dumps([config.CLAUDE_ACCOUNTS, config.CLAUDE_CONFIG_DIRS, config.CODEX_HOMES]))" % ROOT)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
        accts, cdirs, homes = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(accts, ["1", "2"])
        self.assertEqual(cdirs[1], logins.account_dir("claude", 2))
        self.assertEqual(homes, [logins.account_dir("codex", 1)])



class Notices(unittest.TestCase):
    def test_token_pattern_and_start_code(self):
        from app import telegram
        self.assertTrue(telegram.TOKEN_RE.search("Use this token: 1234567890:AAH" + "x" * 32 + " keep it"))
        self.assertFalse(telegram.TOKEN_RE.search("010-1234-5678"))
        code = telegram.new_start_code()
        ups = [{"message": {"chat": {"id": 1, "type": "private"}, "text": "/start other"}},
               {"message": {"chat": {"id": 2, "type": "private"}, "text": f"/start {code}"}},
               {"message": {"chat": {"id": 3, "type": "private"}, "text": "hello"}}]
        saved = telegram.call
        telegram.call = lambda *a, **k: ups
        try:
            self.assertFalse(telegram.find_chat("t", "unissued123")["ok"])
            self.assertEqual(telegram.find_chat("t", code)["chat_id"], "2")
            self.assertFalse(telegram.find_chat("t", "zzz")["ok"])
            self.assertFalse(telegram.find_chat("t")["ok"])
        finally:
            telegram.call = saved

    def test_codex_output_parsing(self):
        from app import logins
        raw = "Follow these steps\n   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n   \x1b[94mLULT-58DML\x1b[0m\n"
        clean = logins.ANSI.sub("", raw)
        self.assertEqual(logins.URL.search(clean).group(0), "https://auth.openai.com/codex/device")
        self.assertEqual(logins.DEVICE_CODE.search(clean).group(1), "LULT-58DML")

    def test_channel_and_email_round(self):
        from app import mailer
        self.assertEqual(notify.channel({"notify": "telegram"}), "none")       # not connected yet
        self.assertEqual(notify.channel({"notify": "email", "email_address": "a@b.c", "email_password": "x"}), "email")
        self.assertEqual(notify.channel({"telegram_token": "t", "telegram_chat_id": "1"}), "telegram")  # old settings
        self.assertEqual(mailer.host_for("me@gmail.com"), ("smtp.gmail.com", 465, True))
        self.assertEqual(mailer.host_for("me@googlemail.com"), ("smtp.gmail.com", 465, True))
        settings.update(notify="email", email_address="a@b.c")
        settings.set_secret("email_password", "pw")
        sent = []
        saved = mailer.send, notify.engine.items, notify._load_seen, notify._save_seen
        mailer.send = lambda subject, text, *a: sent.append((subject, text))
        notify.engine.items = lambda state, n: [{"id": f"{state}1", "title": "이벤트 " + state, "need": "identity"}]
        notify._load_seen, notify._save_seen = (lambda: set()), (lambda seen: None)
        try:
            self.assertEqual(notify.Watcher(lambda m: None).once(), 2)
        finally:
            mailer.send, notify.engine.items, notify._load_seen, notify._save_seen = saved
            settings.update(notify="", email_address="")
        self.assertEqual(len(sent), 1)                # one mail per round
        self.assertIn("2건", sent[0][0])
        self.assertIn("이벤트 owner", sent[0][1])


class TelegramBotByAI(unittest.TestCase):
    """The AI run itself is faked: a child that writes the result file like the AI would."""

    def test_prompt_has_every_value(self):
        from app import tgbot
        text = tgbot._prompt("abc123", r"C:\x\result.json")
        self.assertNotIn("{{", text)
        self.assertIn("/start abc123", text)
        self.assertIn("web.telegram.org/k/#@BotFather", text)

    def test_inputs_and_usernames(self):
        from app import tgbot
        self.assertEqual(tgbot.check_inputs("", ""), "")
        self.assertEqual(tgbot.check_inputs("내 알림", "my_alert_bot"), "")
        self.assertTrue(tgbot.check_inputs("", "my_alert"))        # must end with bot
        self.assertTrue(tgbot.check_inputs("", "1abc_bot"))        # must start with a letter
        self.assertTrue(tgbot.check_inputs("x" * 65, ""))
        self.assertEqual(tgbot.usernames("my_alert_bot"), ["my_alert_bot"])
        rnd = tgbot.usernames("")
        self.assertEqual(len(rnd), 3)
        self.assertTrue(all(tgbot.USERNAME_RE.match(u) for u in rnd))
        text = tgbot._prompt("c0de", "r.json", "내 알림", "my_alert_bot")
        self.assertIn("`my_alert_bot`", text)
        self.assertIn("내 알림", text)

    def test_refused_username_asks_again_without_logout(self):
        from app import deps, logins, tgbot
        def fake_command(agent, task_file, workdir):
            code = ("import json, re, sys; t = open(sys.argv[1], encoding='utf-8').read(); "
                    "f = re.search(r'result file `([^`]+)`', t).group(1); "
                    "json.dump({'ok': False, 'reason': 'username'}, open(f, 'w'))")
            return [sys.executable, "-c", code, task_file]
        cleared = []
        saved = (tgbot.agents, tgbot._command, tgbot.clear_web_data, logins.logged_in_dirs)
        tgbot.agents = lambda: ["claude"]
        tgbot._command = fake_command
        tgbot.clear_web_data = lambda: cleared.append(1) or True
        logins.logged_in_dirs = lambda a: [logins.account_dir(a, 1)]
        try:
            with self.assertRaises(deps.StepError) as cm:
                tgbot.run(lambda *a: None, "claude", "", "taken_name_bot")
        finally:
            (tgbot.agents, tgbot._command, tgbot.clear_web_data, logins.logged_in_dirs) = saved
        self.assertIn("다른 아이디", str(cm.exception))
        self.assertEqual(tgbot.LAST["retry"], "username")
        self.assertEqual(cleared, [])                       # still logged in, so the user can try another one
        with self.assertRaises(deps.StepError):
            tgbot.run(lambda *a: None, "gemini")            # not an AI that can do it

    def test_run_keeps_token_finds_chat_and_cleans_up(self):
        from app import logins, telegram, tgbot
        tok = "1234567890:AA" + "b" * 33
        sent = []

        def fake_command(agent, task_file, workdir):
            code = ("import json, re, sys; t = open(sys.argv[1], encoding='utf-8').read(); "
                    "f = re.search(r'result file `([^`]+)`', t).group(1); "
                    "json.dump({'ok': True, 'token': '%s', 'username': 'x_bot', 'logout': True}, open(f, 'w'))" % tok)
            return [sys.executable, "-c", code, task_file]

        saved = (tgbot.agents, tgbot._command, tgbot.clear_web_data, logins.logged_in_dirs,
                 telegram.verify, telegram.find_chat, telegram.send)
        tgbot.agents = lambda: ["codex"]
        tgbot._command = fake_command
        tgbot.clear_web_data = lambda: True
        logins.logged_in_dirs = lambda a: [logins.account_dir(a, 1)]
        telegram.verify = lambda t: {"ok": True, "username": "x_bot"}
        telegram.find_chat = lambda t, code="": {"ok": True, "chat_id": "77"}
        telegram.send = lambda text, *a, **k: sent.append(text)
        reports = []
        try:
            tgbot.run(lambda pct, text: reports.append(text), "codex", "내 알림", "")
        finally:
            (tgbot.agents, tgbot._command, tgbot.clear_web_data, logins.logged_in_dirs,
             telegram.verify, telegram.find_chat, telegram.send) = saved
        s = settings.load()
        self.assertEqual(settings.get_secret("telegram_token"), tok)
        self.assertEqual((s["telegram_chat_id"], s["notify"], s["telegram_bot"]), ("77", "telegram", "x_bot"))
        self.assertTrue(sent)
        self.assertIn("로그아웃했습니다", reports[-1])
        work = paths.sub("data", "work", "telegram-bot")
        self.assertFalse(os.path.exists(os.path.join(work, "result.json")))  # the token file is gone
        self.assertFalse(os.path.exists(os.path.join(work, "task.md")))
        settings.set_secret("telegram_token", "")
        settings.update(telegram_chat_id="", notify="", telegram_bot="")


class Contest(unittest.TestCase):
    def test_save_merges_policy_file(self):
        path = os.path.join(paths.data_dir(), "policy.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"x_actions": "none"}, f)
        g = server.contest_get()
        self.assertEqual(g["values"]["contest_fields"], ["write", "ai"])
        self.assertIn("photo", [f["id"] for f in g["fields"]])
        self.assertFalse(server.contest_set({"contest_fields": ["bad"]})["ok"])
        self.assertFalse(server.contest_set({"contest_ceremony": "maybe"})["ok"])
        self.assertFalse(server.contest_set({"contest_prize_ask_krw": -1})["ok"])
        r = server.contest_set({"contest_fields": ["photo"], "contest_ceremony": "exclude", "contest_prize_ask_krw": 1000000})
        self.assertTrue(r["ok"])
        self.assertEqual(r["values"]["contest_fields"], ["photo"])
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["x_actions"], "none")
        self.assertEqual(data["contest_ceremony"], "exclude")
        self.assertTrue(settings.load()["contest_chosen"])


if __name__ == "__main__":
    unittest.main()
