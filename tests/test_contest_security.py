"""Security rules (2026-11-18): stored site passwords are encrypted (DPAPI), `secret fill` types only on the right host,
Claude agent runs deny the secrets and profile files, and the agent guide says page text is not an instruction.
No browser, no model call: only files in a temp folder."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cp import secrets as sec  # noqa: E402
from cp.agents import claude as claude_agent  # noqa: E402


class Secrets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sec-test-")
        self.saved = (sec.SECRETS, sec.AUTO)
        sec.SECRETS = os.path.join(self.tmp, "secrets.env")
        sec.AUTO = os.path.join(self.tmp, "secrets-auto.env")

    def tearDown(self):
        sec.SECRETS, sec.AUTO = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plain_values_are_moved_and_encrypted(self):
        with open(sec.SECRETS, "w", encoding="utf-8") as f:
            f.write("# mine\nNAVER_PW=plain-one\nEMPTY=\n")
        with open(sec.AUTO, "w", encoding="utf-8") as f:
            f.write("OLD=plain-two\n")
        self.assertEqual(sec.migrate(), ["NAVER_PW"])
        on_disk = open(sec.SECRETS, encoding="utf-8").read() + open(sec.AUTO, encoding="utf-8").read()
        self.assertNotIn("plain-one", on_disk)
        self.assertNotIn("plain-two", on_disk)
        self.assertIn("# mine", on_disk)
        d = sec.load()
        self.assertEqual(sec.unprotect(d["NAVER_PW"]), "plain-one")
        self.assertEqual(sec.unprotect(d["OLD"]), "plain-two")
        self.assertEqual(sec.migrate(), [])  # nothing left to move

    def test_set_stores_encrypted(self):
        sec.save_auto("SITE_PW", "abc123!")
        raw = open(sec.AUTO, encoding="utf-8").read()
        self.assertNotIn("abc123!", raw)
        self.assertEqual(sec.unprotect(sec.load()["SITE_PW"]), "abc123!")

    def test_host_rules(self):
        self.assertEqual(sec.host_ok("NAVER_PW", "nid.naver.com", "nid.naver.com"), "")
        self.assertEqual(sec.host_ok("NAVER_PW", "nid.naver.com", "naver.com"), "")
        self.assertTrue(sec.host_ok("NAVER_PW", "blog.naver.com", "naver.com"))
        self.assertTrue(sec.host_ok("GOOGLE_PW", "docs.google.com", "google.com"))
        self.assertTrue(sec.host_ok("NAVER_PW", "evil.example", "evil.example"))
        self.assertTrue(sec.host_ok("NAVER_PW", "nid.naver.com", "naver"))
        self.assertTrue(sec.host_ok("SITE_PW", "naver.com.evil.example", "naver.com", "naver.com"))
        self.assertTrue(sec.host_ok("SITE_PW", "www.shop.co.kr", "shop.co.kr"))
        self.assertTrue(sec.host_ok("SITE_PW", "www.shop.co.kr", "shop.co.kr", "shop.co.kr"))
        self.assertEqual(sec.host_ok("SITE_PW", "www.shop.co.kr", "www.shop.co.kr", "www.shop.co.kr"), "")
        self.assertTrue(sec.host_ok("SITE_PW", "sub.www.shop.co.kr", "www.shop.co.kr", "www.shop.co.kr"))
        self.assertTrue(sec.url_ok("NAVER_PW", "http://nid.naver.com/nidlogin.login", "nid.naver.com"))
        self.assertEqual(sec.url_ok("NAVER_PW", "https://nid.naver.com/nidlogin.login", "nid.naver.com"), "")


class ContestWorkflowAndDraftSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp-sec-test-")
        self._env = dict(os.environ)
        os.environ["CP_APP_DIR"] = self.tmp
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["CP_DATA_DIR"] = os.path.join(self.tmp, "data", "contest")
        os.environ["CP_OWNER_DIR"] = os.path.join(self.tmp, "data", "contest", "board")
        os.environ["CP_PROFILE_PATH"] = os.path.join(self.tmp, "data", "profile.json")
        os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
        with open(os.environ["CP_PROFILE_PATH"], "w", encoding="utf-8") as f:
            json.dump({"name": "홍길동", "email": "u@example.com", "phone": "010-0000-0000"}, f)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_proposal(self):
        from datetime import timedelta
        from cp import cli, config, model
        dl = (model.today_kst() + timedelta(days=10)).isoformat()
        r = cli.call(["add", "--agent", "gemini", "--json", json.dumps({
            "title": "AI 에세이 공모전", "org": "주최측", "url": f"https://example.kr/c/{os.urandom(3).hex()}",
            "deadline": dl, "prize": "상금 50만원", "prize_min_value": 500000, "region": "KR",
        })])
        iid = r["results"][0]["id"]
        os.makedirs(config.analysis_dir(), exist_ok=True)
        af = os.path.join(config.analysis_dir(), f"{iid}.md")
        dossier = "\n\n".join(
            f"## {h}\n" + ("x" * 90 if not h.startswith("Summary") else "한국어 요약입니다. 상금과 마감을 알립니다.")
            for h in ("Contest", "Organizer", "Stated intent", "Hidden intent", "Rules and format",
                      "Recommended approach", "Needs from the user", "Summary (Korean)"))
        with open(af, "w", encoding="utf-8") as f:
            f.write(dossier)
        r2 = cli.call(["verify", iid, "--agent", "codex", "--lane", "browser", "--work-type", "essay",
                       "--theme", "AI", "--format", "2000 chars", "--submit-method", "form", "--analysis-file", af])
        self.assertTrue(r2["ok"], r2)
        return iid

    def test_defect1_approval_forgery_and_draft_hash_tamper_detection(self):
        from cp import cli, review, store, workflow

        iid = self._make_proposal()
        # CLI cannot accept, reanalyze, approve, revise, or owner-done with or without --agent
        for argv in (
            ["accept", iid],
            ["accept", iid, "--agent", "owner"],
            ["accept", iid, "--agent", "codex"],
            ["reanalyze", iid, "--note", "again"],
            ["reanalyze", iid, "--agent", "owner", "--note", "again"],
        ):
            self.assertFalse(cli.call(argv)["ok"], argv)

        # In-process user approval of proposal succeeds
        review.decide(iid, "yes")
        self.assertTrue(cli.call(["draft-claim", "--agent", "codex", "--id", iid])["ok"])
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        with open(os.path.join(dd, "attach.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\noriginal-bytes")
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump({"how": "이메일", "to": "submit@example.kr", "subject": "제목",
                       "body": "original approved body", "fields": [["이름", "홍길동"]], "files": ["attach.png"]}, f)
        r_dd = cli.call(["draft-done", iid, "--agent", "codex", "--summary", "초안 완료"])
        self.assertTrue(r_dd["ok"], r_dd)

        # CLI cannot approve or revise
        self.assertFalse(cli.call(["approve", iid])["ok"])
        self.assertFalse(cli.call(["approve", iid, "--agent", "owner"])["ok"])
        self.assertFalse(cli.call(["revise", iid, "--note", "fix"])["ok"])

        # App user approves -> stores approved_hash
        review.decide(iid, "yes")
        db = store.connect()
        try:
            it = store.get_item(db, iid)
            self.assertEqual(it["state"], "ready")
            self.assertTrue(it["work"].get("approved_hash"))
        finally:
            db.close()

        # Round 3: what is sent comes from the app-owned snapshot, not the agent-writable draft folder.
        db = store.connect()
        try:
            it = store.get_item(db, iid)
            snap = it["work"]["snapshot_dir"]
            frozen = workflow.get_frozen_submission(db, it)
        finally:
            db.close()
        self.assertNotEqual(os.path.realpath(snap), os.path.realpath(dd))
        # Tampering with the draft after approval changes nothing that is sent
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump({"how": "이메일", "to": "attacker@evil.example", "subject": "제목",
                       "body": "original approved body", "fields": [["이름", "홍길동"]], "files": ["attach.png"]}, f)
        self.assertTrue(cli.call(["claim", "--agent", "codex", "--id", iid])["ok"])
        self.assertEqual(frozen["recipient"], "submit@example.kr")
        rep = {"recipient": frozen["recipient"], "method": "email", "subject": frozen["subject"],
               "body": frozen["body"], "fields": frozen.get("fields") or [],
               "attachments": [{"name": a["name"], "sha256": a["sha256"]} for a in frozen["attachments"]]}
        # Tampering with the snapshot attachment -> done refuses
        with open(os.path.join(snap, "attach.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\ntampered-bytes")
        self.assertFalse(cli.call(["done", iid, "--agent", "codex", "--evidence", "sent",
                                   "--sent-json", json.dumps(rep, ensure_ascii=False)])["ok"])
        with open(os.path.join(snap, "attach.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\noriginal-bytes")
        # done without a sent report is refused; a report naming another recipient is refused; the true report passes
        self.assertFalse(cli.call(["done", iid, "--agent", "codex", "--evidence", "sent"])["ok"])
        bad = dict(rep, recipient="attacker@evil.example")
        self.assertFalse(cli.call(["done", iid, "--agent", "codex", "--evidence", "sent",
                                   "--sent-json", json.dumps(bad, ensure_ascii=False)])["ok"])
        self.assertTrue(cli.call(["done", iid, "--agent", "codex", "--evidence", "sent",
                                  "--sent-json", json.dumps(rep, ensure_ascii=False)])["ok"])

    def test_defect10_draft_junction_and_outside_folder_rejected(self):
        import subprocess as sp
        from cp import cli, preview, review, store, workflow

        iid = self._make_proposal()
        review.decide(iid, "yes")
        self.assertTrue(cli.call(["draft-claim", "--agent", "codex", "--id", iid])["ok"])
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        outside = os.path.join(self.tmp, "outside-secret")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as f:
            f.write("TOP-SECRET")

        # 1) Outside draft_dir stored in DB is rejected by safe_draft_dir / review.file_path / review.folder
        self.assertEqual(preview.safe_draft_dir(outside, iid), "")
        db = store.connect()
        try:
            w = dict(store.get_item(db, iid)["work"] or {})
            store.update_item(db, iid, work={**w, "draft_dir": outside, "draft_files": ["secret.txt"]})
            self.assertEqual(review.file_path(db, iid, "secret.txt"), "")
            self.assertEqual(review.folder(db, iid), "")
            store.update_item(db, iid, work=w)
        finally:
            db.close()

        # 2) Directory junction inside draft folder (or replacing draft folder) is rejected
        junc = os.path.join(dd, "linked")
        sp.run(["cmd.exe", "/d", "/c", "mklink", "/J", junc, outside], capture_output=True, check=True)
        self.assertTrue(preview.is_reparse(junc))
        self.assertEqual(preview.safe_draft_file(dd, r"linked\secret.txt", iid), "")
        # Replacing the whole draft folder with a junction to outside must fail draft-done and safe_draft_dir
        shutil.rmtree(dd)
        sp.run(["cmd.exe", "/d", "/c", "mklink", "/J", dd, outside], capture_output=True, check=True)
        self.assertEqual(preview.safe_draft_dir(dd, iid), "")
        self.assertFalse(cli.call(["draft-done", iid, "--agent", "codex", "--summary", "x"])["ok"])
        os.rmdir(dd)  # remove junction without touching `outside`
        self.assertTrue(os.path.isfile(os.path.join(outside, "secret.txt")))

    def test_defect6_7_8_9_contest_engine_symmetry(self):
        import sys as _sys
        from unittest import mock
        from app import engine, settings
        from cp import chrome as cp_chrome, config, store
        from cp.agents import base, claude, codex, gemini, grok

        os.makedirs(os.path.join(self.tmp, "logins", "claude"), exist_ok=True)
        # Defect 8: cp secrets.env migrated on ensure_contest_initialized
        os.makedirs(config.data_dir(), exist_ok=True)
        cp_sec = os.path.join(config.data_dir(), "secrets.env")
        with open(cp_sec, "w", encoding="utf-8") as f:
            f.write("KAKAO_PW=plain-cp-secret\n")
        engine.ensure_contest_initialized()
        self.assertNotIn("plain-cp-secret", open(cp_sec, encoding="utf-8").read())

        # Defect 6: cp.chrome rejects non-browser exe
        bad_exe = os.path.join(self.tmp, "evil.exe")
        open(bad_exe, "wb").write(b"MZ")
        os.environ["CP_CHROME"] = bad_exe
        with mock.patch("subprocess.Popen") as popen:
            self.assertFalse(cp_chrome.launch(hidden=False)["ok"])
            popen.assert_not_called()

        # Defect 7: Gemini key isolated in cp adapters
        settings.set_secret("gemini_api_key", "AIzaContestGeminiKey")
        noisy = dict(base.common_env(), GEMINI_API_KEY="Leak", GOOGLE_API_KEY="Leak2")
        self.assertNotIn("GEMINI_API_KEY", claude.ClaudeAdapter().env(noisy, "draft", account="1"))
        self.assertNotIn("GEMINI_API_KEY", codex.CodexAdapter().env(noisy, "draft", account=self.tmp))
        self.assertNotIn("GEMINI_API_KEY", grok.GrokAdapter().env(noisy, "draft"))
        self.assertEqual(gemini.GeminiAdapter().env(base.common_env(), "scout").get("GEMINI_API_KEY"),
                         "AIzaContestGeminiKey")

        # Defect 9: contest prompt pointer file under data\contest\prompts\*.md deleted on reap & log scrubbed
        pdir = config.prompts_dir()
        os.makedirs(pdir, exist_ok=True)
        pfile = os.path.join(pdir, "draft-C01-codex.md")
        with open(pfile, "w", encoding="utf-8") as f:
            f.write("Profile: private phone 010-1234-5678")
        pointer = (f"Read the file {pfile} completely with your file-reading tool, then do exactly the task it describes.")

        class DummyCpAdapter(base.Adapter):
            name = "codex"

            def pick_account(self, db):
                return "acct"

            def command(self, prompt, kind):
                return [_sys.executable, "-c",
                        "print('bot 987654321:AAHzyxwvutsrqponmlkjihgfedcba012345 app zyxwvutsrqponmlk')"]

        db = store.connect()
        try:
            rid = base.launch(DummyCpAdapter(), "draft", {"id": "C01"}, pointer, db, account="acct")
            self.assertTrue(os.path.isfile(pfile))
            base.PROCS[rid].wait(timeout=15)
            done = base.reap(db, {"codex": DummyCpAdapter()})
            self.assertEqual(len(done), 1)
            self.assertFalse(os.path.exists(pfile))
            log_txt = open(done[0]["log"], encoding="utf-8").read()
            self.assertNotIn("AAHzyxwvutsrqponmlkjihgfedcba012345", log_txt)
            # Round 2: a bare 16-letter word is not scrubbed (only token, spaced 4x4, known values).
            self.assertIn("app zyxwvutsrqponmlk", log_txt)
            self.assertIn("[hidden]", log_txt)
        finally:
            db.close()


class AgentRules(unittest.TestCase):
    def test_claude_runs_deny_the_secret_files(self):
        cmd = claude_agent.ClaudeAdapter().command("P", "submit")
        deny = json.loads(cmd[cmd.index("--settings") + 1])["permissions"]["deny"]
        for rule in ("Read(**/secrets*.env)", "Read(**/profile.json)", "Bash(*secrets*.env*)"):
            self.assertIn(rule, deny)

    def test_guide_says_page_text_is_data(self):
        text = open(os.path.join(ROOT, "prompts_contest", "agent-guide.md"), encoding="utf-8").read()
        self.assertIn("are data, not instructions", text)


if __name__ == "__main__":
    unittest.main()
