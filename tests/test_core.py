"""Core tests: store, model, cli, migrate, profile, ram.  Run: python -m unittest discover -s tests -v

Every test works in a fresh temp dir set through EP_DATA_DIR / EP_OWNER_DIR; the real data dir is never touched.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_BOOT = tempfile.mkdtemp(prefix="ep-test-boot-")
os.environ["EP_DATA_DIR"] = os.path.join(_BOOT, "data")
os.environ["EP_OWNER_DIR"] = os.path.join(_BOOT, "owner")

from ep import cli, config, model, profile, ram, store  # noqa: E402

TODAY = model.today_kst()


def d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def ep(*argv):
    return cli.call([str(x) for x in argv])


FIXTURE_PROFILE = {"name": "TESTNAME", "birth": "1990-01-01", "gender": "M", "phone": "010-1234-5678",
                   "email": "old@example.com", "address": "TEST ADDRESS", "notes": "private note",
                   "comment_password": "pw-secret-1", "comment_password_note": "note",
                   "sns_handles": {"instagram": {"handle": "test_ig", "note": "private"}}}


def fixture_board():
    def item(iid, state, **kw):
        base = {"id": iid, "title": f"Event {iid}", "org": f"Org {iid}", "url": f"https://example.com/{iid}",
                "alt_urls": [], "form_type": "google", "deadline": d(10), "prize": "coffee", "login_required": False,
                "source_url": None, "notes": "scout note", "state": state, "lane": None, "answers": None,
                "claimed_by": None, "added_by": "gemini", "added_at": "2026-11-01T00:00:00+09:00",
                "history": [{"at": "2026-11-01T00:00:00+09:00", "id": iid, "agent": "gemini", "action": "add",
                             "detail": ""}]}
        base.update(kw)
        return base

    items = [
        item("E001", "submitted", deadline=None, submitted_by="codex", submitted_at="2026-11-01T01:00:00+09:00",
             evidence="response recorded"),
        item("E002", "manual", manual_need="login", manual_reason="site account needed", min_prize_won="5,000",
             all_rewarded="예", winners="100", effort="3", url="https://www.shop.example.kr/event/1"),
        item("E003", "failed", deadline="2026-11-01", fail_reason="timeout", lane="codex",
             history=[{"at": "x", "id": "E003", "agent": "codex", "action": "fail", "detail": "t"}]),
        item("E004", "owner", owner_question="which answer?", deadline=d(20)),
        item("E005", "ready_codex", lane="codex", verify_notes="follow first", deadline=d(5),
             possible_dup_of="E004"),
        item("E006", "ready_gemini", lane="gemini", deadline="상시", all_rewarded="true"),
    ]
    pool = [{"id": "P001", "title": "Pool one", "org": "Org P1", "url": "https://forms.gle/p1", "source_url": None,
             "form_type": "google", "deadline": d(12), "prize": "gift", "login_required": False, "conditions": "c",
             "result_date": None, "added_by": "gemini", "added_at": "2026-11-01T00:00:00+09:00",
             "min_prize_won": "3000", "all_rewarded": "아니오"},
            {"id": "P002", "title": "Pool two", "org": "Org P2", "url": "https://forms.gle/p2", "source_url": None,
             "form_type": "google", "deadline": d(14), "prize": "gift", "login_required": False, "conditions": "c",
             "result_date": None, "added_by": "gemini", "added_at": "2026-11-01T00:00:00+09:00"}]
    errors = [{"at": "2026-11-02T00:00:00+09:00", "agent": "codex", "message": "old failure", "resolved": False}]
    return {"version": 1, "target": 100, "next_id": 7, "items": items, "errors": errors, "log": [], "pool": pool}


FIXTURE_RUNS = {"stop": False, "blocked": {}, "runs": [], "verify_tries": {"E004": 2},
                "codex_home": r"C:\fake\codex\acct-b", "claude_account": "acct-a"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-test-")
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "owner")
        os.makedirs(os.environ["EP_DATA_DIR"])
        os.makedirs(os.environ["EP_OWNER_DIR"])
        self.assertNotEqual(os.path.normcase(config.data_dir()), os.path.normcase(config.DEFAULT_DATA_DIR))
        self.assertNotEqual(os.path.normcase(config.owner_dir()), os.path.normcase(config.DEFAULT_OWNER_DIR))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_fixture(self):
        with open(config.legacy_board_path(), "w", encoding="utf-8") as f:
            json.dump(fixture_board(), f, ensure_ascii=False)
        with open(config.legacy_runs_path(), "w", encoding="utf-8") as f:
            json.dump(FIXTURE_RUNS, f)
        with open(config.profile_path(), "w", encoding="utf-8") as f:
            json.dump(FIXTURE_PROFILE, f, ensure_ascii=False)

    def item(self, iid):
        db = store.connect()
        try:
            return store.get_item(db, iid)
        finally:
            db.close()

    def add(self, **kw):
        row = {"title": "T " + kw.get("url", "x"), "org": "Org", "deadline": d(10)}
        row.update(kw)
        r = ep("add", "--agent", "gemini", "--json", json.dumps(row))
        self.assertTrue(r["ok"], r)
        return r["results"][0]

    def ready(self, lane="form", **kw):
        res = self.add(**kw)
        self.assertEqual(res["status"], "added", res)
        r = ep("verify", res["id"], "--agent", "codex", "--lane", lane)
        self.assertTrue(r["ok"], r)
        return res["id"]


class TestMigrate(Base):
    def test_dry_run_only_reads(self):
        self.write_fixture()
        with open(config.profile_path(), encoding="utf-8") as f:
            before = f.read()
        r = ep("migrate", "--dry-run")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["items_added"], 6)
        self.assertFalse(os.path.exists(config.db_path()))
        with open(config.profile_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_migrate_mapping_and_idempotence(self):
        self.write_fixture()
        with mock.patch.object(config, "PLATFORM_START", "2026-11-05"):
            r1 = ep("migrate")
        self.assertTrue(r1["ok"], r1)
        self.assertEqual((r1["items_added"], r1["pool_added"], r1["errors_added"]), (6, 2, 1))
        self.assertEqual(r1["expired_at_migration"], 1)
        r2 = ep("migrate")
        self.assertEqual((r2["items_added"], r2["pool_added"], r2["errors_added"], r2["history_rows"]), (0, 0, 0, 0))
        self.assertEqual((r2["items_present"], r2["pool_present"]), (6, 2))
        self.assertEqual(r2["kv_set"], [])
        self.assertFalse(any(r2["profile"][k] for k in ("email_changed", "en_added", "jp_added")))

        e3, e5, e6, e2 = self.item("E003"), self.item("E005"), self.item("E006"), self.item("E002")
        self.assertEqual((e3["state"], e3["reason"]), ("excluded", "expired before platform start"))
        self.assertEqual((e5["state"], e5["lane"], e5["region"]), ("ready", "browser", "KR"))
        self.assertEqual(e5["legacy_notes"], "follow first")
        self.assertEqual(e5["flags"]["possible_dup_of"], "E004")
        self.assertEqual((e6["state"], e6["lane"], e6["deadline"], e6["deadline_raw"]), ("ready", "form", "", "상시"))
        self.assertEqual(e6["all_rewarded"], 1)
        self.assertEqual((e2["prize_min_value"], e2["all_rewarded"], e2["winners"], e2["effort_min"]),
                         (5000, 1, 100, 3))
        self.assertEqual((e2["need"], e2["reason"]), ("login", "site account needed"))
        self.assertEqual(self.item("E004")["question"], "which answer?")
        self.assertEqual(self.item("E004")["tries"]["verify"], 2)
        db = store.connect()
        self.assertEqual(store.kv_get(db, "codex_home"), r"C:\fake\codex\acct-b")
        self.assertEqual(store.kv_get(db, "claude_account"), "acct-a")
        self.assertIs(store.kv_get(db, "stop"), False)
        p1 = store.pool_rows(db, "id='P001'")[0]
        self.assertEqual((p1["region"], p1["prize_min_value"], p1["all_rewarded"]), ("KR", 3000, 0))
        hist = store.history(db, "E003")
        self.assertEqual([h["action"] for h in hist], ["fail", "migrate", "expired"])
        self.assertEqual(store.next_item_id(db), "E007")
        db.close()
        with open(config.profile_path(), encoding="utf-8") as f:
            p = json.load(f)
        self.assertEqual(p["email"], FIXTURE_PROFILE.get("email", p.get("email")))
        self.assertEqual(p["en"]["phone_intl"], "+82 10-1234-5678")
        self.assertEqual(p["en"]["name"], "")  # defaults are empty: the user fills them in the app
        self.assertEqual((p["jp"]["name_katakana"], p["jp"]["name_romaji"], p["jp"]["country"]), ("", "", ""))
        self.assertNotIn("_note", p["en"])
        self.assertEqual(p["comment_password"], "pw-secret-1")  # other fields kept


class TestTransitions(Base):
    def test_matrix(self):
        """Every action against every state, as an ordinary agent (codex, not the claimer unless noted)."""
        expect = {
            "next-batch": {"new"}, "verify": {"new", "verifying", "owner"}, "claim": {"ready"},
            "release": {"submitting", "failed", "owner"},
            "requeue": {"owner", "manual", "excluded", "failed", "verifying"},
            "owner-done": set(model.STATES) - {"submitted"},
            "exclude": {"new", "verifying", "ready", "failed", "submitting"},
            "owner": {"new", "verifying", "ready", "failed", "submitting"},
            "manual": {"new", "verifying", "ready", "failed", "submitting"},
            "done": {"submitting"}, "fail": {"submitting"},
        }
        for action, ok_states in expect.items():
            for st in model.STATES:
                it = {"id": "E1", "state": st, "claimed_by": "codex"}
                err = model.check_transition(action, it, "codex")
                self.assertEqual(err is None, st in ok_states, (action, st, err))
        sub = {"id": "E1", "state": "submitting", "claimed_by": "claude"}
        for action in ("done", "fail", "exclude", "owner", "manual"):
            self.assertIsNotNone(model.check_transition(action, sub, "codex"), action)
            self.assertIsNone(model.check_transition(action, sub, "claude"), action)
            self.assertIsNone(model.check_transition(action, sub, "system"), action)

    def test_cli_flow(self):
        iid = self.ready(url="https://forms.gle/flow1")
        self.assertEqual(self.item(iid)["state"], "ready")
        r = ep("claim", "--agent", "codex", "--id", iid)
        self.assertEqual(r["item"]["state"], "submitting")
        self.assertFalse(ep("done", iid, "--agent", "claude", "--evidence", "x")["ok"])
        self.assertFalse(ep("done", iid, "--agent", "codex")["ok"])  # --evidence required
        self.assertFalse(ep("exclude", iid, "--agent", "claude", "--reason", "r")["ok"])
        self.assertTrue(ep("set", iid, "--result-date", "2026-12-01", "--title-en", "Flow")["ok"])
        self.assertEqual(self.item(iid)["state"], "submitting")  # set never changes state
        r = ep("done", iid, "--agent", "codex", "--evidence", "Thanks, response recorded")
        self.assertEqual(r["state"], "submitted")
        it = self.item(iid)
        self.assertEqual((it["submitted_by"], it["claimed_by"]), ("codex", None))
        self.assertTrue(it["submitted_at"])
        self.assertFalse(ep("owner-done", iid, "--evidence", "x")["ok"])
        self.assertFalse(ep("requeue", iid)["ok"])

    def test_fail_four_times_and_no_resubmit(self):
        iid = self.ready(url="https://forms.gle/fail1")
        for n in range(1, 4):
            ep("claim", "--agent", "codex", "--id", iid)
            r = ep("fail", iid, "--agent", "codex", "--reason", f"err{n}")
            self.assertEqual((r["state"], r["tries"]), ("failed", n))
            self.assertTrue(ep("release", iid)["ok"])
        ep("claim", "--agent", "codex", "--id", iid)
        r = ep("fail", iid, "--agent", "codex", "--reason", "err4")
        self.assertEqual(r["state"], "manual")
        it = self.item(iid)
        self.assertEqual((it["need"], it["reason"]), ("other", "failed 4 times: err4"))

        j = self.ready(url="https://forms.gle/fail2")
        ep("claim", "--agent", "claude", "--id", j)
        self.assertFalse(ep("fail", j, "--agent", "codex", "--reason", "x")["ok"])
        r = ep("fail", j, "--agent", "claude", "--reason", "completion unknown", "--no-resubmit")
        self.assertTrue(r["no_resubmit"])
        ids = [x["id"] for x in ep("list", "--state", "failed", "--flag", "no_resubmit")["items"]]
        self.assertEqual(ids, [j])
        self.assertFalse(ep("release", j)["ok"])
        self.assertTrue(ep("release", j, "--force")["ok"])

    def test_requeue_owner_manual_exclude(self):
        iid = self.ready(url="https://forms.gle/rq1", notes="keep me")
        self.assertTrue(ep("owner", iid, "--agent", "codex", "--question", "Q?")["ok"])
        self.assertEqual(self.item(iid)["question"], "Q?")
        r = ep("requeue", iid, "--note", "owner says A")
        self.assertEqual(r["state"], "new")
        it = self.item(iid)
        self.assertEqual((it["lane"], it["question"], it["need"], it["reason"]), (None, None, None, None))
        self.assertEqual(it["notes"], "keep me")
        self.assertIn("owner says A", it["verify_notes"])
        self.assertFalse(ep("requeue", iid)["ok"])  # new -> new is invalid
        self.assertTrue(ep("manual", iid, "--agent", "codex", "--need", "kakao", "--reason", "app only")["ok"])
        self.assertFalse(ep("exclude", iid, "--agent", "codex", "--reason", "x")["ok"])  # manual: owner only
        self.assertFalse(ep("owner-done", iid, "--evidence", "did it myself")["ok"])
        self.assertTrue(cli.owner_act("done", iid, "did it myself")["ok"])
        self.assertEqual(self.item(iid)["submitted_by"], "owner")
        k = self.add(url="https://forms.gle/rq2")["id"]
        self.assertTrue(ep("exclude", k, "--agent", "codex", "--reason", "not eligible")["ok"])
        self.assertFalse(ep("release", k)["ok"])
        self.assertTrue(ep("requeue", k)["ok"])

    def test_next_batch_order_and_tries(self):
        a = self.add(url="https://forms.gle/nb1", deadline=d(5))["id"]
        b = self.add(url="https://forms.gle/nb2", deadline=d(2), region="US")["id"]
        c = self.add(url="https://forms.gle/nb3", deadline=d(2))["id"]
        n = self.add(url="https://forms.gle/nb4", deadline="상시")["id"]
        r = ep("next-batch", "--agent", "codex", "--size", "10")
        self.assertEqual([x["id"] for x in r["items"]], [c, b, a, n])
        self.assertTrue(all(x["state"] == "verifying" for x in r["items"]))
        for i in (a, b, c, n):
            ep("requeue", i, "--agent", "codex")
        ep("next-batch", "--agent", "codex")
        for i in (a, b, c, n):
            ep("requeue", i, "--agent", "codex")
        self.assertEqual(ep("next-batch", "--agent", "codex")["items"], [])  # 2 tries per agent kind
        self.assertEqual(len(ep("next-batch", "--agent", "claude")["items"]), 4)


class TestClaim(Base):
    def test_order_and_capability(self):
        a = self.ready(url="https://forms.gle/c1", deadline=d(5))
        b = self.ready(url="https://forms.gle/c2", deadline=d(3), region="US")
        c = self.ready(url="https://forms.gle/c3", deadline=d(3), region="JP")
        k = self.ready(url="https://forms.gle/c4", deadline=d(3))
        e = self.ready(url="https://forms.gle/c5", deadline="")
        br = self.ready(lane="browser", url="https://forms.gle/c6", deadline=d(1))
        self.assertFalse(ep("claim", "--agent", "gemini", "--id", br)["ok"])
        self.assertFalse(ep("claim", "--agent", "gemini", "--lane", "browser")["ok"])
        self.assertFalse(ep("claim", "--agent", "owner")["ok"])
        got = [ep("claim", "--agent", "gemini")["item"]["id"] for _ in range(5)]
        self.assertEqual(got, [k, c, b, a, e])
        self.assertIsNone(ep("claim", "--agent", "gemini")["item"])
        self.assertEqual(ep("claim", "--agent", "codex")["item"]["id"], br)


class TestExpiry(Base):
    def test_auto_expire(self):
        iid = self.ready(url="https://forms.gle/x1", deadline=d(3))
        sub = self.ready(url="https://forms.gle/x2", deadline=d(3))
        ep("claim", "--agent", "codex", "--id", sub)
        with store.writer() as db:
            db.execute("UPDATE items SET deadline=? WHERE id IN (?, ?)", (d(-1), iid, sub))
        r = ep("status")
        self.assertEqual(r["expired_now"], 1)
        it = self.item(iid)
        self.assertEqual((it["state"], it["reason"]), ("excluded", "expired"))
        db = store.connect()
        self.assertEqual(store.history(db, iid)[-1]["action"], "expired")
        db.close()
        self.assertEqual(self.item(sub)["state"], "submitting")
        self.assertEqual(ep("status")["expired_now"], 0)

    def test_add_past_deadline_invalid(self):
        r = ep("add", "--json", json.dumps({"title": "Old", "org": "O", "url": "https://a.b/c", "deadline": d(-2)}))
        self.assertEqual(r["results"][0]["status"], "invalid")

    def test_timezones(self):
        kr = {"deadline": "2026-11-05", "region": "KR"}
        us = {"deadline": "2026-11-05", "region": "US"}
        jp_time = {"deadline": "2026-11-05", "deadline_time": "18:00", "region": "JP"}
        now = datetime(2026, 11, 6, 10, 0, tzinfo=model.tzinfo("Asia/Seoul"))
        self.assertTrue(model.is_expired(kr, now))
        self.assertFalse(model.is_expired(us, now))  # 23:59 New York = 13:59 KST next day
        self.assertTrue(model.is_expired(jp_time, datetime(2026, 11, 5, 18, 30, tzinfo=model.tzinfo("Asia/Seoul"))))
        self.assertFalse(model.is_expired(jp_time, datetime(2026, 11, 5, 17, 30, tzinfo=model.tzinfo("Asia/Seoul"))))
        self.assertFalse(model.is_expired(dict(us, tz="America/Los_Angeles"),
                                          datetime(2026, 11, 6, 15, 0, tzinfo=model.tzinfo("Asia/Seoul"))))


class TestParsers(unittest.TestCase):
    def test_deadlines(self):
        t = date(2026, 11, 5)
        cases = [
            ("2026-11-05", ("2026-11-05", "")), ("2026.11.05", ("2026-11-05", "")),
            ("2026/11/05", ("2026-11-05", "")), ("2026. 11. 5.", ("2026-11-05", "")),
            ("11-05", ("2026-11-05", "")), ("11/05", ("2026-11-05", "")), ("11.05", ("2026-11-05", "")),
            ("11월 5일", ("2026-11-05", "")), ("2026년 11월 30일(일)", ("2026-11-30", "")),
            ("11月5日", ("2026-11-05", "")), ("2026年12月1日 23:59まで", ("2026-12-01", "23:59")),
            ("Nov 5, 2026", ("2026-11-05", "")), ("November 5", ("2026-11-05", "")),
            ("December 1st, 2026 11:59", ("2026-12-01", "11:59")), ("5 December 2026", ("2026-12-05", "")),
            ("2026-11-05T23:59", ("2026-11-05", "23:59")), ("11-05 18:00", ("2026-11-05", "18:00")),
            ("~ 11.5(수)", ("2026-11-05", "")), ("12/31/2026", ("2026-12-31", "")),
            ("1월 3일", ("2027-01-03", "")), ("01/10", ("2026-01-10", "")),
            ("상시", ("", "")), ("공고에 없음", ("", "")), ("", ("", "")), (None, ("", "")), ("13/45", ("", "")),
        ]
        for text, want in cases:
            self.assertEqual(model.parse_deadline(text, "KR", today=t), want, text)

    def test_site_kind(self):
        cases = [
            ("https://docs.google.com/forms/d/e/x/viewform", "google_form"), ("https://forms.gle/abc", "google_form"),
            ("https://form.naver.com/response/abc", "naver_form"), ("https://naver.me/xyz", "naver_form"),
            ("https://answer.moaform.com/answers/a", "moaform"), ("https://www.nownsurvey.com/s/1", "nownsurvey"),
            ("https://www.youtube.com/watch?v=1", "youtube"), ("https://youtu.be/1", "youtube"),
            ("https://blog.naver.com/a/1", "naver_blog"), ("https://m.blog.naver.com/a/1", "naver_blog"),
            ("https://www.instagram.com/p/1/", "instagram"), ("https://x.com/a/status/1", "x"),
            ("https://twitter.com/a", "x"), ("https://www.facebook.com/a", "facebook"),
            ("https://www.threads.net/@a", "threads"), ("https://www.threads.com/@a", "threads"),
            ("https://pf.kakao.com/_x", "kakao"), ("https://open.kakao.com/o/x", "kakao"),
            ("https://www.eventhouse.kr/site/view.php?no=1", "aggregator"), ("https://supertoday.co.kr/1", "aggregator"),
            ("https://gleam.io/abc/x", "gleam"), ("https://www.rafflecopter.com/rafl/x", "gleam"),
            ("https://kingsumo.com/g/x", "gleam"), ("https://woobox.com/x", "gleam"),
            ("https://swee.viralsweep.com/x", "gleam"), ("https://www.surveymonkey.com/r/x", "site_form"),
            ("https://a.typeform.com/to/x", "site_form"), ("https://forms.office.com/r/x", "site_form"),
            ("https://x.qualtrics.com/jfe/form/x", "site_form"), ("https://netflix.com/x", "site_form"),
            ("https://docs.google.com/document/d/1", "site_form"), ("https://shop.example.kr/e", "site_form"),
        ]
        for url, want in cases:
            self.assertEqual(model.site_kind(url), want, url)
        self.assertEqual(model.site_kind("https://example.kr/f", "naver"), "naver_form")
        self.assertEqual(model.url_host("https://swee.viralsweep.com/x"), "swee.viralsweep.com")

    def test_eligibility(self):
        cases = [
            ("Open to legal residents of the United States, 18+", "residency_required"),
            ("US residents only", "residency_required"), ("open to the 50 United States and DC", "residency_required"),
            ("日本国内在住の方限定", "residency_required"), ("日本在住の方", "residency_required"),
            ("国内にお住まいの方", "residency_required"), ("국내 거주자 대상", "residency_required"),
            ("Prize ships only within Canada", "residency_required"), ("Japan only", "residency_required"),
            ("Giveaway is open worldwide", "open"), ("International entrants welcome", "open"),
            ("Open to all", "open"), ("海外からも応募可", "open"), ("全世界対象", "open"), ("どなたでも応募可能", "open"),
            ("Follow and comment to win", "unknown"), ("", "unknown"),
        ]
        for text, want in cases:
            self.assertEqual(model.eligibility_from_text(text, "US"), want, text)

    def test_norm_url(self):
        self.assertEqual(model.norm_url("https://www.Example.com/a/?utm_source=x&b=2"),
                         model.norm_url("http://example.com/a?b=2&fbclid=1"))
        self.assertEqual(model.norm_url("https://docs.google.com/forms/d/X/viewform?usp=sf_link"),
                         model.norm_url("docs.google.com/forms/d/X/formResponse"))
        self.assertTrue(model.similar("Org A", "Coffee survey event", "Org A", "Coffee survey event round 2"))
        self.assertFalse(model.similar("Org A", "Coffee survey event", "Org B", "Coffee survey event"))


class TestAddCheckPool(Base):
    def test_add_dupes(self):
        r = ep("add", "--agent", "gemini", "--json", json.dumps([
            {"title": "Coffee survey event", "org": "Org A", "url": "https://forms.gle/AAA?utm_source=x",
             "deadline": "Nov 30, 2026", "region": "us", "conditions": "Open worldwide"},
            {"title": "Other", "org": "Org B", "url": "https://forms.gle/AAA/"},
            {"title": "Coffee survey event 2", "org": "Org A", "url": "https://forms.gle/BBB"},
            {"title": "", "org": "Org C", "url": "https://forms.gle/CCC"},
            {"title": "Bad region", "org": "Org C", "url": "https://forms.gle/DDD", "region": "FR"},
        ]))
        st = [x["status"] for x in r["results"]]
        self.assertEqual(st, ["added", "duplicate", "added", "invalid", "invalid"])
        first = r["results"][0]
        self.assertEqual((first["region"], first["deadline"], first["eligibility"]), ("US", "2026-11-30", "open"))
        self.assertEqual(r["results"][2]["possible_dup_of"], first["id"])
        self.assertEqual(self.item(r["results"][2]["id"])["flags"]["possible_dup_of"], first["id"])
        c = ep("check", "https://forms.gle/AAA")
        self.assertTrue(c["known"])
        self.assertEqual(c["item"]["id"], first["id"])
        f = os.path.join(self.tmp, "rows.json")
        with open(f, "w", encoding="utf-8") as fh:
            json.dump({"title": "From file", "org": "Org F", "url": "https://forms.gle/FFF", "deadline": "상시"}, fh)
        r = ep("add", "--file", f)
        self.assertEqual(r["results"][0]["status"], "added")
        self.assertEqual(self.item(r["results"][0]["id"])["deadline_raw"], "상시")

    def test_pool(self):
        r = ep("pool-add", "--json", json.dumps([{"title": "P one", "org": "PO", "url": "https://forms.gle/P1"},
                                                  {"title": "P two", "org": "PO2", "url": "https://forms.gle/P1"}]))
        self.assertEqual([x["status"] for x in r["results"]], ["added", "duplicate"])
        pid = r["results"][0]["id"]
        self.assertEqual(pid, "P001")
        self.assertEqual(ep("pool-list", "--region", "KR")["count"], 1)
        r = ep("pool-promote", pid)
        self.assertEqual(r["results"][0]["status"], "added")
        self.assertEqual(self.item(r["results"][0]["id"])["state"], "new")
        self.assertEqual(ep("pool-promote", pid)["results"][0]["status"], "already")
        self.assertEqual(ep("pool-list")["count"], 0)

    def test_errors_wins_rewards_signups(self):
        self.assertTrue(ep("error", "--agent", "grok", "--message", "402 balance")["ok"])
        self.assertEqual(len(ep("status")["open_errors"]), 1)
        self.assertEqual(ep("resolve", "--agent", "grok")["resolved"], 1)
        self.assertEqual(ep("status")["open_errors"], [])
        a = self.ready(url="https://forms.gle/W1", prize_min_value="5,000", all_rewarded="예", winners=100)
        b = self.ready(url="https://forms.gle/W2", prize_min_value=3000, all_rewarded="아니오")
        self.ready(url="https://forms.gle/W3")
        for _ in range(3):
            iid = ep("claim", "--agent", "codex")["item"]["id"]
            ep("done", iid, "--agent", "codex", "--evidence", "ok")
        r = ep("rewards")
        self.assertEqual((r["count"], r["with_min_value"], r["sum_min_value"], r["sum_min_value_all_rewarded"]),
                         (3, 2, 8000, 5000))
        self.assertEqual(len(r["missing_min_value_ids"]), 1)
        self.assertTrue(ep("win", a, "--prize", "coffee", "--date", "2026-11-10")["ok"])
        self.assertTrue(ep("win", "-", "--prize", "gift card")["ok"])
        self.assertFalse(ep("win", "E9999", "--prize", "x")["ok"])
        self.assertEqual(ep("wins")["count"], 2)
        self.assertEqual(ep("status")["wins"], 2)
        for n, url in enumerate(("https://www.shop.example.kr/e1", "https://event.shop.example.kr/e2",
                                 "https://other.example.com/e3")):
            iid = self.add(url=url, deadline=d(3 + n))["id"]
            ep("manual", iid, "--agent", "codex", "--need", "login" if n < 2 else "identity", "--reason", f"r{n}")
        iid = self.add(url="https://pay.example.com/e", deadline=d(3))["id"]
        ep("manual", iid, "--agent", "codex", "--need", "payment", "--reason", "pay")
        r = ep("signups")
        self.assertEqual((r["sites"], r["items"]), (2, 3))
        g = r["groups"][0]
        self.assertEqual((g["host"], g["count"], g["earliest_deadline"]), ("shop.example.kr", 2, d(3)))
        self.assertEqual(g["reasons"], ["r0", "r1"])
        self.assertIsNotNone(b)


class TestSyncXlsx(Base):
    def make_xlsx(self, done_rows, answer_rows):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = config.XLSX_SHEET_MANUAL
        ws.append([config.XLSX_COL_ID, "필요한 것", config.XLSX_COL_DONE, "내 메모"])
        for iid, v in done_rows:
            ws.append([iid, "login", v, None])
        ws2 = wb.create_sheet(config.XLSX_SHEET_OWNER)
        ws2.append([config.XLSX_COL_ID, "질문", config.XLSX_COL_ANSWER, "내 메모"])
        for iid, v in answer_rows:
            ws2.append([iid, "Q", v, None])
        wb.save(config.owner_xlsx_path())

    def test_sync_once(self):
        self.assertTrue(ep("sync-xlsx")["missing"])
        m = self.add(url="https://forms.gle/S1")["id"]
        ep("manual", m, "--agent", "codex", "--need", "login", "--reason", "account")
        s = self.add(url="https://forms.gle/S2")["id"]
        ep("manual", s, "--agent", "codex", "--need", "app", "--reason", "app only")
        o = self.ready(url="https://forms.gle/S3")
        ep("owner", o, "--agent", "codex", "--question", "Which?")
        self.make_xlsx([(m, "했음"), (s, "안함")], [(o, "pick B")])
        r = ep("sync-xlsx")
        # Round 2: sync-xlsx only queues owner edits (applied=0); nothing changes until the app confirms.
        self.assertEqual((r["changed"], r["queued"], r["pending"]), (0, 3, 3), r)
        self.assertEqual(self.item(m)["state"], "manual")
        self.assertEqual(self.item(s)["state"], "manual")
        self.assertEqual(self.item(o)["state"], "owner")
        db = store.connect()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM owner_answers WHERE applied=0").fetchone()[0], 3)
        db.close()
        self.assertEqual(ep("sync-xlsx")["queued"], 0)  # same edits are not queued twice
        c = cli.confirm_xlsx(items=cli.pending_xlsx())
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["count"], 3, c)
        it = self.item(m)
        self.assertEqual((it["state"], it["submitted_by"], it["evidence"]),
                         ("submitted", "owner", "owner marked done in xlsx"))
        self.assertEqual((self.item(s)["state"], self.item(s)["reason"]), ("excluded", "owner declined in xlsx"))
        it = self.item(o)
        self.assertEqual(it["state"], "new")
        self.assertIn("owner answer: pick B", it["verify_notes"])
        r = ep("sync-xlsx")
        self.assertEqual((r["changed"], r["queued"], r["pending"]), (0, 0, 0), r)
        db = store.connect()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM owner_answers WHERE applied=1").fetchone()[0], 3)
        db.close()
        lock = os.path.join(config.owner_dir(), "~$" + os.path.basename(config.owner_xlsx_path()))
        open(lock, "w").close()
        self.assertEqual(ep("sync-xlsx"), {"ok": True, "changed": 0, "locked": True})


class TestProfileRam(Base):
    def test_profile_views(self):
        self.write_fixture()
        ep("migrate")
        for region in ("KR", "JP", "US"):
            v = profile.view(region)
            blob = json.dumps(v, ensure_ascii=False)
            self.assertNotIn("comment_password", blob)
            self.assertNotIn("pw-secret-1", blob)
            self.assertNotIn("private", blob)
            self.assertNotIn("_note", blob)
            self.assertNotIn("pw-secret-1", profile.summary_for_prompt(region))
        self.assertIn("email", profile.view("KR"))
        self.assertNotIn("country", profile.view("US"))  # empty defaults are not shown
        self.assertNotIn("address", profile.view("US"))
        self.assertEqual(profile.view("JP")["phone_intl"], "+82 10-1234-5678")
        self.assertIn("sns.instagram.handle: test_ig", profile.summary_for_prompt("KR"))

    def test_ram(self):
        f = ram.free_gb()
        self.assertIsInstance(f, float)
        self.assertGreater(f, 0)
        self.assertTrue(ram.can_launch(0, floor_gb=0))
        self.assertFalse(ram.can_launch(config.MAX_AGENTS, floor_gb=0))


class TestCliDispatch(Base):
    def test_missing_module(self):
        cli.REST_MODULES["zz-test"] = "zz_missing_module"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.dispatch_external(["zz-test", "x"])
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads(buf.getvalue()), {"ok": False, "error": "module not available: zz_missing_module"})
        finally:
            del cli.REST_MODULES["zz-test"]
        self.assertIsNone(cli.dispatch_external(["status"]))

    def test_one_json_line(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            cli.main(["get", "E404"])
        self.assertEqual(cm.exception.code, 1)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(json.loads(lines[0])["ok"])


def tearDownModule():
    shutil.rmtree(_BOOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
