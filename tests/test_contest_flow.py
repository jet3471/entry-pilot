"""Contest engine (package cp): the flow verify -> proposal -> accept -> draft -> review -> approve -> ready, the user's
answers through cp.review, the filters from policy.json, the notice outbox and the prompts. No network, no agents.

Every test works in a fresh temp folder set through CP_DATA_DIR / CP_OWNER_DIR; the real data folder is never touched.
"""
import json
import os
from datetime import datetime
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_BOOT = tempfile.mkdtemp(prefix="cp-test-boot-")
os.environ["CP_APP_DIR"] = _BOOT
os.environ["CP_DATA_DIR"] = os.path.join(_BOOT, "data")
os.environ["CP_OWNER_DIR"] = os.path.join(_BOOT, "owner")

from cp import cli, cmdbase, config, crawler, model, notify, policy, prompts, review, store, workflow  # noqa: E402

TODAY = model.today_kst()


def d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def cp(*argv):
    return cli.call([str(x) for x in argv])


DOSSIER = "\n\n".join(
    f"## {h}\n" + ("x" * 90 if not h.startswith("Summary") else "한국어 요약입니다. 상금과 마감을 알립니다.")
    for h in ("Contest", "Organizer", "Stated intent", "Hidden intent", "Rules and format", "Recommended approach",
              "Needs from the user", "Summary (Korean)"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp-test-")
        os.environ["CP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["CP_OWNER_DIR"] = os.path.join(self.tmp, "owner")
        os.makedirs(os.environ["CP_DATA_DIR"])
        self.db = store.connect()
        self.addCleanup(self.db.close)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def new_item(self, **kw):
        row = {"title": "T " + kw.get("url", "x"), "org": "Org", "deadline": d(10), "region": "KR"}
        row.update(kw)
        r = cp("add", "--agent", "gemini", "--json", json.dumps(row))
        self.assertEqual(r["results"][0]["status"], "added", r)
        return r["results"][0]["id"]

    def proposal(self, top_krw=500000, **kw):
        iid = self.new_item(url=f"https://example.com/{len(self.db.execute('SELECT id FROM items').fetchall())}", **kw)
        os.makedirs(config.analysis_dir(), exist_ok=True)
        path = os.path.join(config.analysis_dir(), f"{iid}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(DOSSIER)
        r = cp("verify", iid, "--agent", "codex", "--lane", "browser", "--work-type", "essay", "--theme", "t",
               "--format", "2000 chars", "--submit-method", "form", "--analysis-file", path,
               *(["--top-prize-krw", top_krw] if top_krw else []))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"], "proposal")
        return iid

    def drafted(self, iid, send=None, extra=None):
        """Accepted, drafted by a fake agent: the item waits in `review`."""
        review.decide(iid, "yes")
        r = cp("draft-claim", "--agent", "codex", "--id", iid)
        self.assertTrue(r["ok"], r)
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        with open(os.path.join(dd, "work.md"), "w", encoding="utf-8") as f:
            f.write("essay text")
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump(send or {"how": "이메일", "to": "a@example.kr", "subject": "응모", "body": "essay text",
                               "fields": [["제목", "나의 글"]]}, f, ensure_ascii=False)
        if extra:
            extra(dd)
        r = cp("draft-done", iid, "--agent", "codex", "--summary", "수필 한 편입니다.")
        self.assertTrue(r["ok"], r)
        return iid


class Flow(Base):
    def test_user_approval_is_required_before_submit(self):
        iid = self.proposal()
        self.assertEqual(store.get_item(self.db, iid)["state"], "proposal")
        # nothing is written or submitted until the user says 통과
        self.assertEqual(review.STATE_KO["proposal"], "분석서 확인")
        msg = review.decide(iid, "yes")
        self.assertIn("통과", msg)
        self.assertEqual(store.get_item(self.db, iid)["state"], "draft")
        r = cp("draft-claim", "--agent", "codex", "--id", iid)
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        open(os.path.join(dd, "work.md"), "w", encoding="utf-8").write("text")
        json.dump({"how": "이메일", "to": "a@example.kr", "body": "text"}, open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8"))
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "한 편")["state"] == "review")
        # an agent cannot approve; only the user (owner) path moves review -> ready
        r = cp("approve", iid, "--agent", "codex")
        self.assertFalse(r["ok"], r)
        self.assertEqual(store.get_item(self.db, iid)["state"], "review")
        review.decide(iid, "yes")
        self.assertEqual(store.get_item(self.db, iid)["state"], "ready")
        rows = self.db.execute("SELECT kind FROM owner_answers WHERE item_id=?", (iid,)).fetchall()
        self.assertEqual([x[0] for x in rows], ["approve", "approve"])

    def test_revise_needs_memo_and_goes_back(self):
        iid = self.drafted(self.proposal())
        with self.assertRaises(cmdbase.CmdError):
            review.decide(iid, "revise", "  ")
        review.decide(iid, "revise", "결말을 줄여 주세요")
        it = store.get_item(self.db, iid)
        self.assertEqual(it["state"], "draft")
        self.assertEqual(it["work"]["review_note"], "결말을 줄여 주세요")

    def test_revise_proposal_reanalyzes_and_skip_excludes(self):
        a, b = self.proposal(), self.proposal()
        review.decide(a, "revise", "다른 방향으로")
        self.assertEqual(store.get_item(self.db, a)["state"], "new")
        review.decide(b, "skip")
        self.assertEqual(store.get_item(self.db, b)["state"], "excluded")
        with self.assertRaises(cmdbase.CmdError):
            review.decide(a, "yes")  # now `new`: nothing to answer

    def test_no_audit_without_auditor(self):
        iid = self.proposal(top_krw=5000000)
        w = store.get_item(self.db, iid)["work"]
        self.assertFalse(w.get("audit_required"))  # the app connects no auditor agent, so a big prize is not held back


class Review(Base):
    def test_overview_and_detail(self):
        a = self.proposal()
        b = self.drafted(self.proposal())
        ov = review.overview(self.db)
        self.assertEqual({x["id"] for x in ov["answer"]}, {a, b})
        self.assertEqual(ov["answer"][0]["id"], b)  # finished works first
        det = review.detail(self.db, b)
        self.assertTrue(det["answerable"])
        types = [u["type"] for u in det["units"]]
        self.assertIn("fields", types)
        self.assertIn("mail", types)
        self.assertEqual([f["name"] for f in det["files"]], ["SEND.json", "work.md"])
        self.assertTrue(any(x["label"] == "상금" for x in det["rows"]))
        pd = review.detail(self.db, a)
        self.assertIn("한국어 요약", pd["dossier"]["text"])
        self.assertEqual(pd["units"], [])
        self.assertIsNone(review.detail(self.db, "C999"))

    def test_files_are_limited_to_the_draft_folder(self):
        iid = self.drafted(self.proposal())
        self.assertTrue(review.file_path(self.db, iid, "work.md").endswith("work.md"))
        self.assertEqual(review.file_path(self.db, iid, "..\\..\\secret.txt"), "")
        self.assertEqual(review.file_path(self.db, iid, "nothere.txt"), "")
        self.assertEqual(review.file_path(self.db, "C999", "work.md"), "")

    def test_pdf_pages_are_shown_as_pictures(self):
        import pymupdf

        def make_pdf(dd):
            doc = pymupdf.open()
            doc.new_page().insert_text((72, 72), "hello")
            doc.new_page()
            doc.save(os.path.join(dd, "form.pdf"))
            doc.close()
        iid = self.drafted(self.proposal(), {"how": "이메일", "to": "a@example.kr", "files": ["form.pdf"], "body": "see file"},
                           make_pdf)
        det = review.detail(self.db, iid)
        imgs = [u for u in det["units"] if u["type"] == "image"]
        self.assertEqual(len(imgs), 2)
        self.assertTrue(review.preview_png(self.db, iid, "form.pdf", 0).startswith(bytes([137]) + b"PNG"))
        self.assertIsNone(review.preview_png(self.db, iid, "work.md", 0))
        self.assertIsNone(review.preview_png(self.db, iid, "form.pdf", 9))

    def test_text_work_without_send_json_is_shown_as_text(self):
        iid = self.proposal()
        dd = workflow.draft_dir_for(iid)
        store.update_item(self.db, iid, state="review", work={**store.get_item(self.db, iid)["work"],
                          "draft_dir": dd, "draft_files": ["work.md"]})
        os.makedirs(dd, exist_ok=True)
        open(os.path.join(dd, "work.md"), "w", encoding="utf-8").write("본문입니다")
        det = review.detail(self.db, iid)
        self.assertEqual(det["units"][0]["type"], "text")
        self.assertIn("본문입니다", det["units"][0]["text"])


class Notices(Base):
    def test_notice_goes_to_the_outbox_not_to_a_private_tool(self):
        iid = self.drafted(self.proposal(top_krw=2000000))  # big prize: told at once
        r = notify.run(now=datetime(2026, 1, 5, 14, 0))  # outside the 09:00 and 21:00 report hours, so the test does not depend on the clock
        self.assertEqual([x["kind"] for x in r["sent"]], ["review_big"])
        files = os.listdir(config.notices_dir())
        self.assertEqual(len(files), 1)
        m = json.load(open(os.path.join(config.notices_dir(), files[0]), encoding="utf-8"))
        self.assertIn(iid, m["text"])
        self.assertIn("공모전", m["text"])
        for bad in ("tailscale", "대표", "relay"):
            self.assertNotIn(bad, (m["subject"] + m["text"]).lower())
        self.assertEqual(notify.run(now=datetime(2026, 1, 5, 14, 5))["sent"], [])  # told once
        self.assertTrue(store.get_item(self.db, iid)["work"]["notified_review"])


class Filters(Base):
    def test_defaults_and_contest_policy_keys(self):
        p = policy.load_policy()
        self.assertEqual(p["contest_fields"], ["write", "ai"])
        self.assertEqual(p["proposal_policy"], "owner")
        self.assertEqual(p["review_policy"], "all")
        self.assertFalse(p["audit_enabled"])

    def test_field_filter_drives_the_crawler_and_prompts(self):
        self.assertTrue(policy.contest_field_ok("[공모전] 에세이 공모"))
        self.assertFalse(policy.contest_field_ok("[공모전] 사진 공모"))
        keep = {"contest_fields": ["photo"]}
        self.assertTrue(policy.contest_field_ok("[공모전] 사진 공모", {**policy.POLICY, **keep}))
        stubs = [{"source_url": "https://x.example/1", "title": "에세이", "text": "[공모전] 에세이 공모", "_deadline_iso": d(5), "links": []},
                 {"source_url": "https://x.example/2", "title": "사진", "text": "[공모전] 사진 공모", "_deadline_iso": d(5), "links": []}]
        items, counts, errors, worked = crawler.collect(lambda u: "", set(), set(), 10, TODAY,
                                                         sources=[("fake", lambda fetch, today=None: stubs)])
        self.assertEqual([i["title"] for i in items], ["에세이"])  # the photo contest is outside the chosen fields
        line = policy.policy_lines("KR", "scout", {**policy.POLICY, "contest_onsite_writing": "exclude"})
        self.assertIn("venue", line)
        line = policy.policy_lines("KR", "verify", {**policy.POLICY, "contest_mail_only": "exclude"})
        self.assertIn("mail only", line)

    def test_policy_json_in_the_contest_folder_is_used(self):
        path = os.path.join(config.data_dir(), "policy.json")
        json.dump({"contest_fields": ["photo"], "contest_ceremony": "exclude"}, open(path, "w", encoding="utf-8"))
        p = policy.load_policy()
        self.assertEqual(p["contest_fields"], ["photo"])
        self.assertEqual(p["contest_ceremony"], "exclude")


class Prompts(Base):
    def test_prompts_are_generic(self):
        iid = self.proposal()
        item = store.get_item(self.db, iid)
        for kind in ("verify", "draft"):
            text = "\n".join(t for _, t in prompts._parts(kind, item, {"agent": "claude"}))
            self.assertIn("cp.py", text)
            self.assertNotIn("{{", text)
            for bad in ("tailscale", "lch", "대표", "hwp-ai", "contest-board", "relay"):
                self.assertNotIn(bad, text.lower())
        draft = "\n".join(t for _, t in prompts._parts("draft", item, {"agent": "claude"}))
        self.assertIn("not supported in this app", draft)  # HWP is handed to the user as manual work
        self.assertIn("--need unsupported", draft)

    def test_unsupported_is_a_manual_need(self):
        iid = self.proposal()
        review.decide(iid, "yes")
        self.assertTrue(cp("draft-claim", "--agent", "claude", "--id", iid)["ok"])
        r = cp("manual", iid, "--agent", "claude", "--need", "unsupported", "--reason", "HWP form")
        self.assertTrue(r["ok"], r)
        self.assertEqual(store.get_item(self.db, iid)["state"], "manual")
        self.assertIn("unsupported", review.NEED_KO)


class Config(unittest.TestCase):
    def test_paths_come_from_the_environment(self):
        self.assertEqual(config.data_dir(), os.environ["CP_DATA_DIR"])
        self.assertEqual(config.DEFAULT_DATA_DIR, os.path.join(config.APP_DIR, "data", "contest"))
        self.assertTrue(config.STANDALONE)
        self.assertEqual(config.CLAUDE_ACCOUNTS[0], "1")
        self.assertEqual(config.INTEGRATIONS_FILE, "")  # the hook for the external tools is unset by default

    def test_chrome_has_its_own_port_and_profile(self):
        from cp import chrome
        self.assertIn("9223", chrome.CDP)
        self.assertIn("9223", " ".join(chrome.FLAGS))


if __name__ == "__main__":
    unittest.main()
