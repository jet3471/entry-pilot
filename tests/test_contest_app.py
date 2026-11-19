"""Contest mode in the app layer: settings keys, the contest environment, the review routes, the notice outbox, the
python shim for cp.py, the external-tool hooks that are reserved but not supported, and a scan that keeps the author's
private setup out of the contest engine. No network, no windows, no real scheduler."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="ep-contest-app-test-")
os.environ["EP_APP_DIR"] = _TMP
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import engine, notify, paths, pyshim, server, settings  # noqa: E402

DOSSIER = "\n\n".join(f"## {h}\n" + ("x" * 90 if not h.startswith("Summary") else "한국어 요약입니다.")
                      for h in ("Contest", "Organizer", "Stated intent", "Hidden intent", "Rules and format",
                                "Recommended approach", "Needs from the user", "Summary (Korean)"))


def cpcall(*argv):
    return engine._contest_call([str(x) for x in argv])


class Settings(unittest.TestCase):
    def test_integrations_are_reserved_and_off(self):
        s = settings.load()
        self.assertEqual(set(s["integrations"]), {"lch_wiki", "llm_relay", "llm_console"})
        for v in s["integrations"].values():
            self.assertEqual(v, {"enabled": False, "url": ""})
        self.assertFalse(settings.DEFAULTS["modes"]["contests"])  # off until the user picks it

    def test_integrations_cannot_be_switched_on(self):
        r = server.handle("POST", "/api/settings", {}, {"integrations": {"lch_wiki": {"enabled": True, "url": "x"}}})
        self.assertFalse(r["settings"]["integrations"]["lch_wiki"]["enabled"])
        self.assertEqual(engine.integrations_file(), "")
        self.assertNotIn("EP_INTEGRATIONS_FILE", engine.engine_env())
        self.assertNotIn("CP_INTEGRATIONS_FILE", engine.contest_env())
        settings.update(integrations={n: {"enabled": False, "url": ""} for n in settings.INTEGRATIONS})


class Env(unittest.TestCase):
    def test_contest_env_is_separate(self):
        e = engine.contest_env()
        self.assertEqual(e["CP_DATA_DIR"], os.path.join(paths.data_dir(), "contest"))
        self.assertEqual(e["CP_PROFILE_PATH"], os.path.join(paths.data_dir(), "profile.json"))
        self.assertFalse([k for k in e if k.startswith("EP_")])
        self.assertIn("CP_AGENTS", e)
        self.assertNotEqual(e["CP_DATA_DIR"], engine.engine_env()["EP_DATA_DIR"])

    def test_start_without_a_login_is_refused(self):
        settings.update(agents={"claude": True, "codex": False, "gemini": False})
        with mock.patch.object(engine.logins, "logged_in_dirs", return_value=[]):
            r = engine.start_contest()
        self.assertFalse(r["ok"])
        self.assertIn("로그인", r["error"])

    def test_modes_decide_what_starts(self):
        settings.update(modes={"events": False, "contests": True})
        with mock.patch.object(engine, "start_contest", return_value={"ok": True, "pid": 1}) as sc, \
                mock.patch.object(engine, "start") as se:
            r = engine.start_modes()
        self.assertTrue(r["ok"])
        sc.assert_called_once()
        se.assert_not_called()
        settings.update(modes={"events": True, "contests": False})


class Shim(unittest.TestCase):
    def test_cp_py_runs_the_contest_engine(self):
        env = dict(os.environ, CP_DATA_DIR=os.path.join(_TMP, "shimdata"), CP_APP_DIR=_TMP)
        code = ("import sys; sys.path.insert(0, %r); from app import pyshim; sys.exit(pyshim.run(['-X', 'utf8', %r, 'status']))"
                % (ROOT, os.path.join(ROOT, "cp.py")))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")[-500:])
        self.assertTrue(json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])["ok"])


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        settings.update(modes={"events": True, "contests": True})
        engine.ensure_contest_initialized()
        cls.srv = server.serve()
        cls.port = cls.srv.server_address[1]
        row = {"title": "에세이 공모", "org": "기관", "deadline": "2099-01-01", "region": "KR", "url": "https://example.com/a"}
        r = cpcall("add", "--agent", "gemini", "--json", json.dumps(row))
        cls.iid = r["results"][0]["id"]
        os.makedirs(os.path.join(engine.contest_dir(), "analysis"), exist_ok=True)
        path = os.path.join(engine.contest_dir(), "analysis", cls.iid + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(DOSSIER)
        v = cpcall("verify", cls.iid, "--agent", "codex", "--lane", "browser", "--work-type", "essay", "--theme", "t",
                   "--format", "x", "--submit-method", "email", "--analysis-file", path, "--top-prize-krw", 100000)
        assert v["ok"], v

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, body=None, token=None, raw=False):
        h = {"Content-Type": "application/json"}
        if token != "":
            h["X-EP-Token"] = server.TOKEN if token is None else token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_a_overview_and_state(self):
        st, ov = self.call("/api/contest/overview")
        self.assertEqual(st, 200)
        self.assertEqual([x["id"] for x in ov["answer"]], [self.iid])
        self.assertEqual(ov["answer"][0]["state_ko"], "분석서 확인")
        st, s = self.call("/api/state")
        self.assertIn("contest_engine", s)
        self.assertFalse(s["integrations_supported"])
        self.assertEqual(self.call("/api/contest/overview", token="bad")[0], 403)

    def test_b_item_and_answer(self):
        st, it = self.call(f"/api/contest/item?id={self.iid}")
        self.assertTrue(it["answerable"])
        self.assertIn("한국어 요약", it["dossier"]["text"])
        st, r = self.call("/api/contest/decide", {"id": self.iid, "action": "revise", "note": ""})
        self.assertFalse(r["ok"])  # a revise needs a memo
        st, r = self.call("/api/contest/decide", {"id": self.iid, "action": "yes"})
        self.assertTrue(r["ok"], r)
        st, ov = self.call("/api/contest/overview")
        self.assertEqual([x["id"] for x in ov["running"]], [self.iid])
        self.assertEqual(ov["running"][0]["state"], "draft")
        st, r = self.call("/api/contest/decide", {"id": self.iid, "action": "yes"})
        self.assertFalse(r["ok"])  # already answered

    def test_c_files_need_the_token_and_stay_in_the_folder(self):
        dd = os.path.join(engine.contest_dir(), "drafts", self.iid)
        os.makedirs(dd, exist_ok=True)
        with open(os.path.join(dd, "work.md"), "w", encoding="utf-8") as f:
            f.write("본문")
        from cp import store
        engine.configure_contest()
        db = store.connect()
        try:
            it = store.get_item(db, self.iid)
            store.update_item(db, self.iid, work={**it["work"], "draft_dir": dd, "draft_files": ["work.md"]})
        finally:
            db.close()
        tok = server.TOKEN
        st, data = self.call(f"/api/contest/file?id={self.iid}&name=work.md&t={tok}", token="", raw=True)
        self.assertEqual((st, data.decode("utf-8")), (200, "본문"))
        self.assertEqual(self.call(f"/api/contest/file?id={self.iid}&name=work.md", token="")[0], 403)
        self.assertEqual(self.call(f"/api/contest/file?id={self.iid}&name=..%5C..%5Csettings.json&t={tok}", token="")[0], 404)
        self.assertEqual(self.call(f"/api/contest/preview?id={self.iid}&pdf=work.md&n=0&t={tok}", token="")[0], 404)
        st, r = self.call("/api/contest/open-folder", {"id": self.iid})
        self.assertTrue(r["ok"])

    def test_d_filters_reach_both_engines(self):
        st, r = self.call("/api/contest", {"contest_fields": ["photo"], "contest_ceremony": "exclude"})
        self.assertTrue(r["ok"], r)
        for p in (os.path.join(paths.data_dir(), "policy.json"), os.path.join(engine.contest_dir(), "policy.json")):
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["contest_fields"], ["photo"])

    def test_e_contest_item_routes_in_notify(self):
        self.assertTrue(notify.is_contest("C001"))
        self.assertFalse(notify.is_contest("E001"))


class Outbox(unittest.TestCase):
    def setUp(self):
        self.d = notify.outbox_dir()
        os.makedirs(self.d, exist_ok=True)
        for n in os.listdir(self.d):
            os.remove(os.path.join(self.d, n))
        with open(os.path.join(self.d, "20260101-000000-000001.json"), "w", encoding="utf-8") as f:
            json.dump({"subject": "[공모전] 시험", "text": "답을 기다리는 항목입니다."}, f, ensure_ascii=False)

    def test_sent_notices_are_deleted(self):
        with mock.patch.object(notify.telegram, "send") as send:
            self.assertEqual(notify.send_outbox("telegram"), 1)
        self.assertIn("답을 기다리는", send.call_args[0][0])
        self.assertEqual(os.listdir(self.d), [])

    def test_failed_send_keeps_the_file_and_is_visible(self):
        with mock.patch.object(notify.telegram, "send", side_effect=OSError("down")):
            with self.assertRaises(RuntimeError):
                notify.send_outbox("telegram")
        self.assertEqual(len(os.listdir(self.d)), 1)

    def test_no_channel_keeps_notices(self):
        self.assertEqual(notify.send_outbox("none"), 0)
        self.assertEqual(len(os.listdir(self.d)), 1)


class Private(unittest.TestCase):
    """The contest engine, its prompts and the app code are free of the author's private setup."""
    BAD = frozenset({
        "4748d4c802a775e8db9a23ec58f0986cacdc5d2d3356d22c490a7d22331ff133",
        "795b104abe3e4134960ca245ded0e617f162c751209347b7f003bc35e062f43e",
        "ec6f049478046f72becb006a2339202eae93911281dd610a7c869adec1ba3297",
        "082c23067d409e6450057266b6edda11548cb88aa69cc79e6bdd59a6a3051fcd",
        "7605002388c0be9171f1b6de11d4502ad975273eef07f9069411f0a3b80f1f49",
        "42231fb21487bcaec70dcc22f723f08fe702ec4f238caf6da4d384acdd18523d",
        "6350859c36b140e773b508cef1c0e5d61ec218649e10ba30ca868c6aa108b495",
        "8218218de4ab3885e3d89c94e82002a3fc31de94b34283581df36ab72f0b4680",
        "0df6b224a0366cd8ef1f7794aa1e177c9abe4aa04dc7dcfbc1ab602c04f5a7e2",
        "00682a302ba159cd7ceeae69a4888a16ff66e9f0d9fe5bd885eb6eb71b7541f2",
        "2899b5488744896396afc7310e42b49df1f7bab669f39a0f4f0b095e2e3df4f8",
        "eeda9c8d5884b58cb81f0e0478eccb9b656d07c25db802c8febad3bd46cbad7c",
        "ddae8811c0aada12d04ed4cac9fcd77eaf61fa8691e78a87400c6e5287097f96",
        "70ac1d541cba81b8b0fdb93b705bcb94df28cf588e57f6947ebf6f6e7dbd2f41",
        "249215fed826e1cff9368c2c7490760a16f7bd49f875d39a9f23eaf1505d076d",
        "b932587274ce76e339652701d3555b53a403a72ea4e7fd543f8baee1383cbda0",
        "a789e4581797b6aeef004a0059e24bb28a55518b070cebc67ae4b215905fa2a1",
        "f4c34ff16e04db295a6b91c6ef3c8b01cb42eec00f4e76341fbb7ff2c6049900",
        "df3dd87b64f57c1bdffb808731521c71d58a368b8cd150abd69e0567aceecbe4",
        "df8fb33d1f45146593070b03c053e07ba5a2c30f2b74c94360ee2ec657ec6f45",
    })
    PLACEHOLDER = ("lch-wiki", "llm-relay", "llm-console", "lch_wiki", "llm_relay", "llm_console")

    @staticmethod
    def tokens(text: str) -> set[str]:
        s = text.lower()
        out = set(re.findall(r"[a-z0-9가-힣]+", s))
        for m in re.findall(r"[가-힣]{2,}", s):
            for i in range(len(m)):
                for n in (2, 3, 4):
                    if i + n <= len(m):
                        out.add(m[i:i + n])
        for m in re.findall(r"[a-z0-9_.-]+", s):
            out.add(m)
            for part in m.split("."):
                if part:
                    out.add(part)
            for i, ch in enumerate(m):
                if ch == ".":
                    out.add(m[i:])
        for m in re.findall(r"[a-z0-9_.:-]+(?:\\+[a-z0-9_.:-]+)+", s):
            segs = [x for x in m.split("\\") if x]
            for i in range(len(segs) - 1):
                out.add(segs[i] + "\\" + segs[i + 1])
        return out

    def files(self):
        out = []
        for base in ("cp", "prompts_contest"):
            for dp, _, fs in os.walk(os.path.join(ROOT, base)):
                if "__pycache__" in dp:
                    continue
                out += [os.path.join(dp, f) for f in fs if f.endswith((".py", ".md"))]
        out.append(os.path.join(ROOT, "cp.py"))
        return out

    def test_no_private_words(self):
        hits = []
        for p in self.files() + [os.path.abspath(__file__)]:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for tok in sorted(self.tokens(text)):
                digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()
                if digest in self.BAD:
                    hits.append((os.path.relpath(p, ROOT), digest[:12]))
        self.assertEqual(hits, [])

    def test_external_tools_appear_only_as_the_reserved_hook(self):
        for p in self.files():
            text = open(p, encoding="utf-8", errors="replace").read().lower()
            if any(w in text for w in self.PLACEHOLDER):
                self.assertTrue(p.endswith(os.path.join("cp", "config.py")), p)  # one comment at the hook point

    def test_no_dated_owner_notes(self):
        rx = re.compile(r"owner[^\n]{0,40}20\d\d-\d\d-\d\d|\(owner[^)]*\d\d-\d\d", re.I)
        hits = [os.path.relpath(p, ROOT) for p in self.files() if rx.search(open(p, encoding="utf-8", errors="replace").read())]
        self.assertEqual(hits, [])

    def test_removed_modules_stay_removed(self):
        for n in ("owner_page", "owner_views", "remote_login", "publish", "actuator", "runner_hidden", "render", "owner_xlsx"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, "cp", n + ".py")), n)

    def test_hwp_and_generation_are_marked_unsupported(self):
        draft = open(os.path.join(ROOT, "prompts_contest", "draft.md"), encoding="utf-8").read()
        common = open(os.path.join(ROOT, "prompts_contest", "common.md"), encoding="utf-8").read()
        self.assertIn("--need unsupported", draft)
        self.assertIn("HWP and HWPX are not supported", draft)
        self.assertIn("image or video generation", common)


class Bundle(unittest.TestCase):
    def test_build_script_checks_the_contest_bundle(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "build", "build.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bundle ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
