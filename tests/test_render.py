"""User layer tests: render (page, full list, xlsx), translate cache, backfill.

Run: python -m unittest tests.test_render -v   (or the whole suite: python -m unittest discover -s tests -v)

Every test uses a fresh temp EP_DATA_DIR / EP_OWNER_DIR; the model runner is replaced by a fake, so no
model call and no network access happens. Fixture personal data is fake.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ep import backfill, cli, config, model, prompts, render, store, translate  # noqa: E402

TODAY = model.today_kst()

FAKE_PERSONAL = ["010-9999-8888", "fixture.person@example.com", "fx.ig.handle", "fixture_handle0", "@fixture_x1", "fixture_x1",
                 "fixture.two@example.com", "FixtureName Hong", "EVIDENCE-MARKER-123", "VERIFYNOTE-MARKER-456",
                 "LEGACYNOTE-MARKER-789", r"C:\Users\me\logs"]


def d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


class FakeModel:
    """Stands in for translate.run_model: answers a translate prompt with 'KO:<text>' for every string."""

    def __init__(self, fail=None, wrong_length=False):
        self.calls = []
        self.fail = fail
        self.wrong_length = wrong_length

    def __call__(self, prompt, timeout=120):
        self.calls.append(prompt)
        if self.fail:
            raise translate.ModelError(self.fail)
        arr = translate.extract_json_array(prompt)
        if self.wrong_length:
            arr = arr[:-1]
        return "Here you go:\n" + json.dumps(["KO:" + s for s in arr], ensure_ascii=False) + "\nDone."


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-render-test-")
        self.saved = {k: os.environ.get(k) for k in ("EP_DATA_DIR", "EP_OWNER_DIR")}
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "owner")
        os.makedirs(os.environ["EP_DATA_DIR"])
        render._sensitive_cache = None
        with open(config.profile_path(), "w", encoding="utf-8") as f:
            json.dump({"name": "FixtureName Hong", "phone": "010-9999-8888", "email": "fixture.person@example.com",
                       "sns_handles": {"instagram": {"handle": "fx.ig.handle"}, "x": {"handle": "@fixture_x1"}}}, f)
        self.fake = FakeModel()
        p = mock.patch.object(translate, "run_model", self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.cleanup)
        self.build_fixture()

    def cleanup(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        render._sensitive_cache = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def item(self, iid, state, **kw):
        base = {"id": iid, "region": "KR", "title": f"Event {iid}", "org": f"Org {iid}", "url": f"https://example.com/{iid}",
                "form_type": "other", "site_kind": "site_form", "deadline": d(10), "deadline_time": "", "prize": "",
                "state": state, "added_at": "2026-11-01T00:00:00+09:00", "updated_at": "2026-11-01T00:00:00+09:00",
                "tries": {}, "flags": {}}
        base.update(kw)
        return base

    def build_fixture(self):
        now = store.now_iso()
        yesterday = (TODAY - timedelta(days=1)).isoformat() + "T10:00:00+09:00"
        secret = dict(evidence="Completed. EVIDENCE-MARKER-123 phone 010-9999-8888 mail fixture.person@example.com fx.ig.handle",
                      verify_notes="VERIFYNOTE-MARKER-456 fixture_handle0 @fixture_x1 fixture.two@example.com",
                      legacy_notes="LEGACYNOTE-MARKER-789 FixtureName Hong", notes="note fx.ig.handle")
        rows = [
            self.item("E001", "submitted", submitted_at=now, submitted_by="codex", prize="커피 쿠폰 5,000원 전원",
                      prize_min_value=5000, all_rewarded=1, winners=0, result_date=d(3), **secret),
            self.item("E002", "submitted", submitted_at=yesterday, submitted_by="claude", prize="치킨 3명",
                      prize_min_value=20000, all_rewarded=0, winners=3, result_date=d(6), **secret),
            self.item("E003", "submitted", submitted_at=yesterday, submitted_by="gemini", prize="Gift card 10 USD",
                      region="US", **secret),
            self.item("E004", "submitted", submitted_at=yesterday, submitted_by="owner"),
            self.item("E010", "manual", need="login", deadline=d(3), url="https://www.shop.example.kr/event/1",
                      reason="Login required on the shop site, use fixture.person@example.com"),
            self.item("E011", "manual", need="identity", deadline=d(5), reason="본인인증 필요"),
            self.item("E012", "manual", need="residency", deadline="", reason="Needs a local address"),
            self.item("E020", "owner", question="Which answer do you prefer?", deadline=d(2), **secret),
            self.item("E030", "failed", reason="agent ended without result"),
            self.item("E040", "excluded", reason="expired"),
            self.item("E050", "ready"),
        ]
        with store.writer() as db:
            for r in rows:
                store.insert_item(db, r)
            db.execute("INSERT INTO wins(item_id, won_at, prize, note, recorded_by, at) VALUES(?,?,?,?,?,?)",
                       ("E001", d(-1), "Coffee R", "Owner report: mapped by prize (assumption)", "owner", now))
            store.add_error(db, "codex", r"Codex credits spent, log C:\Users\me\logs\x.log mail fixture.person@example.com")
            store.kv_set(db, "blocked.grok", "usage balance exhausted")
            store.insert_pool(db, {"id": "P001", "region": "KR", "title": "Pool one", "deadline": d(2)})

    def run_render(self, **kw):
        return render.run(**kw)

    def page(self, key="html"):
        with open(config.owner_html_path() if key == "html" else config.full_list_html_path(), encoding="utf-8") as f:
            return f.read()


class RenderPage(Base):
    def test_files_and_privacy(self):
        r = self.run_render(no_xlsx=True)
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.exists(r["html"]) and os.path.exists(r["full"]))
        self.assertEqual(r["xlsx"], None)
        self.assertIn("no-xlsx", r["xlsx_skipped"])
        for key in ("html", "full"):
            text = self.page(key)
            self.assertNotIn("<script", text.lower(), key)
            self.assertNotIn("javascript:", text.lower())
            for bad in FAKE_PERSONAL:
                self.assertNotIn(bad, text, f"{key} leaks {bad!r}")
            self.assertTrue(text.startswith("<!doctype html>"))
        self.assertEqual(r["hash"], hashlib.sha256(self.page().encode("utf-8")).hexdigest())

    def test_stats_and_sections(self):
        r = self.run_render(no_xlsx=True)
        s = r["stats"]
        self.assertEqual(s["submitted"], 4)
        self.assertEqual(s["today"], 1)
        self.assertEqual(s["by_region"], {"KR": 3, "JP": 0, "US": 1})
        self.assertEqual(s["todo"], 4)  # E010, E011, E012 manual + E020 owner
        self.assertEqual(s["in_progress"], 1)  # E050 ready
        self.assertEqual(s["blocked"], ["grok"])
        text = self.page()
        order = [text.index(x) for x in ("1. 내가 할 일", "2. 오늘 제출", "3. 보상", "4. 당첨", "5. 가입하면 참여 가능한 사이트",
                                         "6. 마감", "7. 오류", "8. 확인 필요")]
        self.assertEqual(order, sorted(order))
        # todo: deadline order E020 (d+2), E010 (d+3), E011 (d+5), E012 (no deadline last)
        todo = text.split('id="todo"')[1].split("</section>")[0]
        pos = [todo.index(i) for i in ("E020", "E010", "E011", "E012")]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("로그인/가입 필요", todo)
        self.assertIn("본인인증", todo)
        self.assertIn("현지 주소 필요", todo)
        self.assertIn("확인 필요: KO:Which answer do you prefer?", todo)
        self.assertIn("KO:Login required on the shop site, use […]", todo)  # the email was scrubbed before translation
        self.assertIn("본인인증 필요", todo)  # Korean reason unchanged
        self.assertIn("생성 ", text)
        self.assertIn("· Entry Pilot</footer>", text)

    def test_rewards_wins_errors(self):
        self.run_render(no_xlsx=True)
        text = self.page()
        rew = text.split('id="rewards"')[1].split("</section>")[0]
        self.assertIn("5,000원", rew)            # all_rewarded=1 -> the value itself
        self.assertIn("추첨 (20,000원)", rew)    # raffle with a known value
        self.assertIn("확정 최소 보상 합계</b> 5,000원", rew)
        self.assertIn("걸린 보상 합계</b> 25,000원", rew)
        self.assertIn("값 미정 1건", rew)        # E003 has a prize text but no value yet
        self.assertIn("추정 작업 대기", rew)
        self.assertIn("경품 정보 없음 1건", rew)  # E004
        wins = text.split('id="wins"')[1].split("</section>")[0]
        self.assertIn("Coffee R", wins)
        self.assertIn("Event E001", wins)
        self.assertIn("KO:Owner report", wins)
        self.assertIn("총 당첨 <b>1</b>건", wins)
        sig = text.split('id="signups"')[1].split("</section>")[0]
        self.assertIn("shop.example.kr", sig)
        err = text.split('id="errors"')[1].split("</section>")[0]
        self.assertNotIn("grok", err)  # blocked agents are listed only in 확인 필요
        self.assertIn("KO:Codex credits spent", err)
        chk = text.split('id="check"')[1].split("</section>")[0]
        self.assertIn("grok", chk)
        self.assertIn("KO:usage balance exhausted", chk)
        soon = text.split('id="soon"')[1].split("</section>")[0]
        self.assertIn("후보 1", soon)  # the pool row due in 2 days

    def header(self, text):
        return text.split('<nav>')[0]

    def test_header_has_no_stopped_agent_stat(self):
        self.run_render(no_xlsx=True)
        text = self.page()
        self.assertNotIn("멈춘 에이전트", text)
        self.assertNotIn("grok", self.header(text))  # no stopped agent anywhere in the header

    def test_region_labels_split_by_country(self):
        self.run_render(no_xlsx=True)
        head = self.header(self.page())
        # fixture: submitted KR 3 / US 1 / JP 0; in progress (E050 ready) KR 1; todo KR 4
        self.assertIn("한국 3 · 미국 1 · 일본 0", head)   # 총 제출
        self.assertIn("한국 1 · 미국 0 · 일본 0", head)   # 진행 중
        self.assertIn("한국 4 · 미국 0 · 일본 0", head)   # 내가 할 일
        self.assertNotIn("지역별", head)
        self.assertNotRegex(head, r"\d+·\d+·\d+")  # no lumped triple
        self.assertLess(head.index("한국"), head.index("미국"))
        self.assertLess(head.index("미국"), head.index("일본"))

    def test_today_deadline_count(self):
        with store.writer() as db:
            store.insert_item(db, self.item("E060", "ready", deadline=d(0)))
            store.insert_item(db, self.item("E061", "owner", deadline=d(0), question="Q?", region="JP"))
            store.insert_item(db, self.item("E062", "manual", deadline=d(0), need="login", reason="x", region="US"))
            store.insert_item(db, self.item("E063", "submitted", deadline=d(0)))   # terminal: not counted
            store.insert_item(db, self.item("E064", "failed", deadline=d(0)))      # failed: not counted
            store.insert_item(db, self.item("E065", "ready", deadline=d(1)))       # tomorrow: not counted
        r = self.run_render(no_xlsx=True)
        self.assertEqual(r["stats"]["due_today"], 3)
        self.assertEqual(r["stats"]["due_today_by_region"], {"KR": 1, "US": 1, "JP": 1})
        head = self.header(self.page())
        card = head.split("오늘 마감")[1].split("</div></div>")[0]
        self.assertIn('<div class="n r">3</div>', card)
        self.assertIn("한국 1 · 미국 1 · 일본 1", card)

    def test_today_deadline_zero_and_todo_consistent(self):
        r = self.run_render(no_xlsx=True)
        self.assertEqual(r["stats"]["due_today"], 0)
        text = self.page()
        todo = text.split('id="todo"')[1].split("</section>")[0]
        self.assertEqual(todo.count('<span class="id">'), r["stats"]["todo"])
        self.assertIn(f'<h2>1. 내가 할 일 <small>{r["stats"]["todo"]}건', todo)

    def test_check_section_lists_blocked_agents(self):
        with store.writer() as db:
            store.kv_set(db, "blocked.codex", "Codex credits exhausted")
        self.run_render(no_xlsx=True)
        text = self.page()
        self.assertEqual(text.count('<section id='), text.count("</section>"))
        chk = text.split('id="check"')[1].split("</section>")[0]
        self.assertIn("8. 확인 필요", text)
        # last section, after errors
        self.assertGreater(text.index('id="check"'), text.index('id="errors"'))
        self.assertNotIn('<section', text.split('id="check"')[1])
        for agent in ("codex", "grok"):
            self.assertIn(f"<td>{agent}</td>", chk)
        self.assertNotIn("<td>phone</td>", chk)
        self.assertIn("KO:Codex credits exhausted", chk)
        self.assertNotIn("<a ", chk)  # no restart links: the user restarts in the app
        self.assertNotIn("<form", text.lower())
        self.assertNotIn("<script", text.lower())

    def test_render_hash_marks_unchanged_page(self):
        r1 = self.run_render(no_xlsx=True)
        r2 = self.run_render(no_xlsx=True)
        self.assertTrue(r1["changed"])
        self.assertFalse(r2["changed"])
        self.assertEqual(r1["hash"], r2["hash"])

    def test_check_section_empty(self):
        with store.writer() as db:
            store.kv_del(db, "blocked.grok")
        self.run_render(no_xlsx=True)
        chk = self.page().split('id="check"')[1].split("</section>")[0]
        self.assertIn("확인할 에이전트가 없습니다", chk)
        self.assertNotIn('class="btn"', chk)

    def test_wins_summary_lists_all(self):
        with store.writer() as db:
            for n in range(4):
                db.execute("INSERT INTO wins(item_id, won_at, prize, note, recorded_by, at) VALUES(?,?,?,?,?,?)",
                           ("E002", d(-2 - n), f"Prize{n}", "메모", "owner", store.now_iso()))
        self.run_render(no_xlsx=True)
        wins = self.page().split('id="wins"')[1].split("</section>")[0]
        self.assertIn("총 당첨 <b>5</b>건", wins)
        self.assertEqual(wins.count("<tr>") - 1, 5)  # header row + 5 wins
        for n in range(4):
            self.assertIn(f"Prize{n}", wins)

    def test_row_limit_and_more_line(self):
        with store.writer() as db:
            for n in range(60):
                store.insert_item(db, self.item(f"E1{n:03d}", "manual", need="other", reason="기타 사유", deadline=d(4)))
        self.run_render(no_xlsx=True)
        todo = self.page().split('id="todo"')[1].split("</section>")[0]
        self.assertEqual(todo.count('<span class="id">'), render.MAX_ROWS)
        self.assertIn("외 24건", todo)  # 64 todo rows - 40
        full = self.page("full")
        self.assertIn("E1059", full)  # the full list has everything
        self.assertIn("<details>", full)
        self.assertIn("제외 1건", full)

    def test_translation_cache_and_stable_hash(self):
        r1 = self.run_render(no_xlsx=True)
        self.assertGreater(r1["translated"], 0)
        self.assertEqual(r1["translate_errors"], 0)
        calls = len(self.fake.calls)
        stamp1 = store_gen()
        r2 = self.run_render(no_xlsx=True)
        self.assertEqual(len(self.fake.calls), calls, "second render must come from the i18n cache")
        self.assertEqual(r2["translated"], 0)
        self.assertEqual(r1["hash"], r2["hash"])
        self.assertFalse(r2["html_written"])
        self.assertEqual(store_gen(), stamp1)

    def test_translation_failure_is_marked_and_recorded(self):
        self.fake.fail = "boom"
        r = self.run_render(no_xlsx=True)
        self.assertGreater(r["translate_errors"], 0)
        text = self.page()
        self.assertIn("Which answer do you prefer? (EN)", text)
        db = store.connect()
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM i18n").fetchone()[0], 0, "failures are not cached")
            msgs = [m[0] for m in db.execute("SELECT message FROM errors")]
        finally:
            db.close()
        self.assertTrue(any("translate failed" in m and "boom" in m for m in msgs), msgs)

    def test_dry_run_writes_nothing(self):
        r = self.run_render(dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertFalse(os.path.exists(config.owner_html_path()))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(r["xlsx_skipped"], "dry-run")

    def test_main_prints_one_json_line(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = render.main(["render", "--no-xlsx"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(json.loads(lines[0])["ok"])

    def test_scrub(self):
        s = render.scrub(r"mail a.b@c.com call 010-1234-5678 or +82 10 1234 5678 @someone_x at C:\Users\x\y.log "
                         "token " + "a" * 40 + " FixtureName Hong")
        for bad in ("a.b@c.com", "1234-5678", "@someone_x", r"C:\Users", "a" * 40, "FixtureName"):
            self.assertNotIn(bad, s)
        known = " ".join(render.KNOWN_ACCOUNT_IDS)
        self.assertNotRegex(render.scrub(known).lower(), "entry-pilot-placeholder-id")
        self.assertEqual(render.scrub("퀴즈 정답 2번, 11/06 마감 2026-11-06"), "퀴즈 정답 2번, 11/06 마감 2026-11-06")
        self.assertEqual(render.safe_url("https://a.example/x?mail=fixture.person@example.com"), "")
        self.assertEqual(render.safe_url("javascript:alert(1)"), "")


def store_gen():
    db = store.connect()
    try:
        return store.kv_get(db, "render_gen")
    finally:
        db.close()


class RenderXlsx(Base):
    def test_xlsx_layout_roundtrip_and_sync(self):
        import openpyxl
        r = self.run_render()
        self.assertTrue(r["ok"], r)
        path = config.owner_xlsx_path()
        self.assertEqual(r["xlsx"], path)
        self.assertIsNone(r["xlsx_skipped"])
        self.assertEqual(cli.read_owner_xlsx(path), [])  # layout is readable, no owner cells yet
        wb = openpyxl.load_workbook(path)
        self.assertEqual(wb.sheetnames[:2], [config.XLSX_SHEET_MANUAL, config.XLSX_SHEET_OWNER])
        ws = wb[config.XLSX_SHEET_MANUAL]
        head = [c.value for c in ws[1]]
        for col in ("ID", "마감", "사이트", "제목", "해야 할 일", "링크", "내 처리", "내 답"):
            self.assertIn(col, head)
        ids = [row[0].value for row in ws.iter_rows(min_row=2)]
        self.assertEqual(ids, ["E010", "E011", "E012"])  # deadline order, no-deadline last
        self.assertEqual(ws.cell(2, head.index("사이트") + 1).value, "shop.example.kr")
        self.assertIn("로그인/가입 필요", ws.cell(2, head.index("해야 할 일") + 1).value)
        wo = wb[config.XLSX_SHEET_OWNER]
        ohead = [c.value for c in wo[1]]
        self.assertEqual(wo.cell(2, 1).value, "E020")
        self.assertIn("KO:Which answer do you prefer?", wo.cell(2, ohead.index("질문") + 1).value)
        for sheet in wb.worksheets:  # nothing personal in the owner file either
            for row in sheet.iter_rows(values_only=True):
                for v in row:
                    for bad in FAKE_PERSONAL:
                        self.assertNotIn(bad, str(v))
        # owner types answers, saves, closes
        ws.cell(2, head.index("내 처리") + 1).value = "했음"
        wo.cell(2, ohead.index("내 답") + 1).value = "the second one"
        wb.save(path)
        wb.close()
        self.assertEqual(sorted(cli.read_owner_xlsx(path)),
                         sorted([("E010", "done", "했음"), ("E020", "answer", "the second one")]))
        r = self.run_render()  # sync-xlsx runs first and only queues both (applied after confirmation in the app)
        self.assertEqual(r["owner_answers_queued"], 2)
        self.assertNotIn("owner_answers_applied", r)
        db = store.connect()
        try:
            self.assertEqual(store.get_item(db, "E010")["state"], "manual")
            self.assertEqual(store.get_item(db, "E020")["state"], "owner")
        finally:
            db.close()
        self.assertTrue(cli.confirm_xlsx(items=cli.pending_xlsx())["ok"])
        r = self.run_render()
        self.assertNotIn("owner_answers_queued", r)
        db = store.connect()
        try:
            self.assertEqual(store.get_item(db, "E010")["state"], "submitted")
            self.assertEqual(store.get_item(db, "E020")["state"], "new")
        finally:
            db.close()
        wb = openpyxl.load_workbook(path)
        self.assertEqual([row[0].value for row in wb[config.XLSX_SHEET_MANUAL].iter_rows(min_row=2)], ["E011", "E012"])
        self.assertEqual(wb[config.XLSX_SHEET_OWNER].max_row, 1)
        wb.close()

    def test_locked_xlsx_is_not_rewritten(self):
        self.run_render()
        path = config.owner_xlsx_path()
        with open(path, "rb") as f:
            before = f.read()
        with store.writer() as db:
            store.insert_item(db, self.item("E099", "manual", need="app", reason="앱 필요", deadline=d(1)))
        with mock.patch.object(cli, "_xlsx_locked", return_value=True):
            r = self.run_render()
        self.assertEqual(r["xlsx_skipped"], "locked")
        self.assertIsNone(r["xlsx"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertTrue(r["ok"])
        r = self.run_render()  # unlocked again: the new item appears
        self.assertIsNone(r["xlsx_skipped"])
        self.assertTrue(r["xlsx_changed"])

    def test_unchanged_content_does_not_touch_the_file(self):
        self.run_render()
        path = config.owner_xlsx_path()
        m = os.path.getmtime(path)
        r = self.run_render()
        self.assertFalse(r["xlsx_changed"])
        self.assertEqual(os.path.getmtime(path), m)

    def test_unapplied_owner_input_is_kept(self):
        old = {config.XLSX_SHEET_MANUAL: {"E777": {"내 처리": "했음"}, "E778": {"내 처리": "모름"}},
               config.XLSX_SHEET_OWNER: {"E888": {"내 답": "my answer"}, "E889": {"내 답": "applied one"}}}
        with store.writer() as db:
            db.execute("INSERT INTO owner_answers(item_id, kind, text, at, applied) VALUES('E889','answer','applied one','x',1)")
        data = render.gather()
        m, o, kept = render.xlsx_rows(data, TODAY, {}, old)
        self.assertEqual(kept, 2)
        self.assertIn("E777", [r[0] for r in m])       # recognized, not applied -> kept at the bottom
        self.assertNotIn("E778", [r[0] for r in m])    # not a recognized value, never an answer
        self.assertIn("E888", [r[0] for r in o])
        self.assertNotIn("E889", [r[0] for r in o])    # already applied -> may go
        self.assertEqual([r for r in o if r[0] == "E888"][0][-1], "my answer")


class TranslateCache(Base):
    def test_cache_passthrough_and_failures(self):
        self.assertTrue(translate.is_korean_or_japanese("로그인 필요"))
        self.assertTrue(translate.is_korean_or_japanese("フォローして応募"))
        self.assertFalse(translate.is_korean_or_japanese("agent ended without result"))
        out = translate.cached_many(["Hello world", "한국어 그대로", "Hello world", "Second text"])
        self.assertEqual(out, {"Hello world": "KO:Hello world", "한국어 그대로": "한국어 그대로", "Second text": "KO:Second text"})
        self.assertEqual(len(self.fake.calls), 1, "all misses go in one call")
        self.assertEqual(translate.LAST["translated"], 2)
        self.assertEqual(translate.LAST["passthrough"], 1)
        db = store.connect()
        try:
            row = db.execute("SELECT en, ko FROM i18n WHERE hash=?",
                             (hashlib.sha256(b"Hello world").hexdigest(),)).fetchone()
        finally:
            db.close()
        self.assertEqual((row["en"], row["ko"]), ("Hello world", "KO:Hello world"))
        translate.cached_many(["Hello world", "Third text"])
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(translate.LAST["cached"], 1)
        # wrong-length reply = failure: marked, not cached, error row
        self.fake.wrong_length = True
        out = translate.cached_many(["Alpha beta", "Gamma delta"])
        self.assertEqual(out["Alpha beta"], "Alpha beta (EN)")
        self.assertEqual(translate.LAST["errors"], 1)
        db = store.connect()
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM i18n WHERE en='Alpha beta'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM errors WHERE message LIKE 'translate failed%'").fetchone()[0], 1)
        finally:
            db.close()

    def test_no_call_mode_and_call_cap(self):
        out = translate.cached_many(["One text", "Two text"], allow_call=False)
        self.assertEqual(out["One text"], "One text (EN)")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(translate.LAST["deferred"], 2)
        many = [f"text number {n} " * 30 for n in range(30)]  # > CHUNK_CHARS -> several chunks
        translate.cached_many(many, max_calls=1)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertGreater(translate.LAST["deferred"], 0)

    def test_extract_json_array(self):
        self.assertEqual(translate.extract_json_array('x ["a", "b"] y'), ["a", "b"])
        self.assertEqual(translate.extract_json_array('note [1] then ["a"]'), [1])  # first decodable array wins
        with self.assertRaises(translate.ModelError):
            translate.extract_json_array("no array here")

    def test_main_text_and_env_note(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = translate.main(["translate", "--text", "agent ended without result"])
        r = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(r["ko"], "KO:agent ended without result")

    def test_pending_lists_page_strings_and_main_translates_them(self):
        got = translate.pending_strings()
        for want in ("Which answer do you prefer?", "agent ended without result", "usage balance exhausted",
                     "Needs a local address", "expired"):
            self.assertIn(want, got)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = translate.main(["translate", "--pending"])
        r = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(r["translated"], 5)
        self.assertEqual(r["errors"], 0)
        calls = len(self.fake.calls)
        with contextlib.redirect_stdout(io.StringIO()):
            translate.main(["translate", "--pending"])
        self.assertEqual(len(self.fake.calls), calls, "second run is all cache hits")

    def test_child_env_fallback_and_accounts_paths(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "x", "CLAUDECODE": "1", "KEEP_ME": "1"}):
            fake_accounts = mock.Mock()
            fake_accounts.claude_env.side_effect = RuntimeError("no account")
            with mock.patch.dict(sys.modules, {"ep.accounts": fake_accounts}), mock.patch("ep.accounts", fake_accounts, create=True):
                env = translate.child_env()
            self.assertEqual(translate.LAST["env"], "fallback")
            self.assertNotIn("CLAUDECODE", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertEqual(env["KEEP_ME"], "1")
            self.assertFalse([k for k in env if k.startswith("LCH_")])
            self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
            fake_accounts.claude_env.side_effect = None
            fake_accounts.claude_env.return_value = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
            with mock.patch.dict(sys.modules, {"ep.accounts": fake_accounts}), mock.patch("ep.accounts", fake_accounts, create=True):
                env = translate.child_env()
            self.assertEqual(translate.LAST["env"], "accounts")
            self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "t")

    def test_run_model_command_shape(self):
        cmd = translate.build_command("PROMPT")
        self.assertEqual(cmd[1:], ["-p", "PROMPT", "--model", "claude-haiku-5-5", "--tools", "", "--system-prompt",
                                   translate.SLIM_SYSTEM, "--strict-mcp-config", "--disable-slash-commands",
                                   "--setting-sources", ""])
        self.assertEqual(translate.child_env()["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")


class Backfill(Base):
    def add_prize_items(self, n=25, **kw):
        with store.writer() as db:
            for k in range(n):
                store.insert_item(db, self.item(f"E2{k:03d}", "submitted", prize=f"스타벅스 아메리카노 {k}명 추첨",
                                                added_at=f"2026-10-{10 + k % 18:02d}T00:00:00+09:00", **kw))

    def answer(self, ids, value=4500, **extra):
        return json.dumps([{"id": i, "prize_min_value": value, "winners": 10, "all_rewarded": 0, "note": "coffee", **extra}
                           for i in ids])

    def test_prompt_within_budget(self):
        self.add_prize_items(20)
        db = store.connect()
        try:
            rows = backfill.candidates(db, backfill.DEFAULT_STATES, 20)
        finally:
            db.close()
        self.assertEqual(len(rows), 20)
        text, ids = prompts.backfill_build(rows)
        self.assertLessEqual(len(text), prompts.BUDGET["total"])
        self.assertLessEqual(len(text), config.PROMPT_MAX_CHARS)
        self.assertEqual(len(ids), 20)
        for needle in ("JPY", "multiply by 9", "1,400", "KRW", "null"):
            self.assertIn(needle, text)
        self.assertNotIn("# Role", text)  # common.md is not part of this kind
        self.assertEqual(prompts.build("backfill", {"items": rows}, {}), text)
        long = [dict(r, prize="상품 " * 400, conditions="조건 " * 400, title="제목 " * 100) for r in rows]
        text2, ids2 = prompts.backfill_build(long)
        self.assertLessEqual(len(text2), prompts.BUDGET["total"])
        self.assertGreaterEqual(len(ids2), 1)

    def test_candidates_selection(self):
        self.add_prize_items(3)
        with store.writer() as db:
            store.insert_item(db, self.item("E300", "excluded", prize="x"))
            store.insert_item(db, self.item("E301", "submitted", prize=""))
            store.insert_item(db, self.item("E302", "submitted", prize="known", prize_min_value=1000))
        db = store.connect()
        try:
            ids = [r["id"] for r in backfill.candidates(db, backfill.DEFAULT_STATES, 50)]
        finally:
            db.close()
        for ok in ("E2000", "E2001", "E2002"):
            self.assertIn(ok, ids)
        for no in ("E300", "E301", "E302"):
            self.assertNotIn(no, ids)

    def test_apply_only_null_fields_and_history(self):
        self.add_prize_items(3)
        with store.writer() as db:
            db.execute("UPDATE items SET winners=77 WHERE id='E2000'")
            store.insert_item(db, self.item("E400", "submitted", prize="kept", title="untouched"))
        answers = [
            {"id": "E2000", "prize_min_value": 4500, "winners": 10, "all_rewarded": 0, "note": "coffee"},
            {"id": "E2001", "prize_min_value": "5,000원", "winners": None, "all_rewarded": 1},
            {"id": "E2002", "prize_min_value": None, "winners": None, "all_rewarded": None, "note": "no price"},
            {"id": "E400", "prize_min_value": 999, "winners": 1, "all_rewarded": 1},   # not asked: ignored
            {"id": "E2001", "prize_min_value": 1},                                    # duplicate answer
        ]
        res = backfill.apply(answers, {"E2000", "E2001", "E2002"})
        self.assertEqual(sorted(res["updated"]), ["E2000", "E2001"])
        reasons = {s["id"]: s["reason"] for s in res["skipped"]}
        self.assertEqual(reasons["E400"], "id not in the request")
        self.assertIn("no value", reasons["E2002"])
        db = store.connect()
        try:
            a, b, c, k = (store.get_item(db, i) for i in ("E2000", "E2001", "E2002", "E400"))
            self.assertEqual((a["prize_min_value"], a["winners"], a["all_rewarded"]), (4500, 77, 0))  # winners was set
            self.assertEqual((b["prize_min_value"], b["winners"], b["all_rewarded"]), (5000, None, 1))
            self.assertIsNone(c["prize_min_value"])
            self.assertIsNone(k["prize_min_value"])
            self.assertEqual(k["title"], "untouched")
            h = store.history(db, "E2000")[-1]
            self.assertEqual((h["agent"], h["action"]), ("claude", "backfill"))
            self.assertIn("prize_min_value=4500", h["detail"])
            self.assertNotIn("winners=", h["detail"])
            self.assertEqual(store.history(db, "E2002")[-1]["action"], "backfill")  # attempt marked
            # an item that was tried is not asked again
            ids = [r["id"] for r in backfill.candidates(db, backfill.DEFAULT_STATES, 50)]
            self.assertNotIn("E2002", ids)
            self.assertNotIn("E2000", ids)
        finally:
            db.close()

    def test_clean_rejects_implausible(self):
        vals, bad = backfill.clean({"prize_min_value": -5, "winners": 0, "all_rewarded": 2})
        self.assertEqual(vals, {})
        self.assertEqual(len(bad), 3)
        vals, bad = backfill.clean({"prize_min_value": 10 ** 12})
        self.assertEqual(vals, {})
        self.assertEqual(backfill.clean({"prize_min_value": 4500.0, "all_rewarded": True})[0], {"prize_min_value": 4500})

    def test_run_with_fake_model_and_dry_run(self):
        self.add_prize_items(5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = backfill.main(["backfill", "--dry-run", "--limit", "3"])
        r = json.loads(buf.getvalue())
        self.assertEqual((rc, r["considered"], len(r["ids"])), (0, 3, 3))
        self.assertTrue(r["prompt_chars"] <= prompts.BUDGET["total"])
        self.assertEqual(self.fake.calls, [])
        ids = r["ids"]
        with mock.patch.object(translate, "run_model", return_value="Sure:\n" + self.answer(ids) + "\n"):
            res = backfill.run(limit=3)
        self.assertEqual((res["ok"], res["considered"], res["updated"]), (True, 3, 3))
        # model failure: error row, nothing marked
        with mock.patch.object(translate, "run_model", side_effect=translate.ModelError("down")):
            res = backfill.run(limit=3)
        self.assertFalse(res["ok"])
        db = store.connect()
        try:
            self.assertTrue(any("backfill model call failed" in m[0] for m in db.execute("SELECT message FROM errors")))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history WHERE action='backfill'").fetchone()[0], 3)
        finally:
            db.close()

    def test_bad_state_argument(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = backfill.main(["backfill", "--state", "nope"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(buf.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
