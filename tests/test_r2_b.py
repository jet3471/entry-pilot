"""Round 2 security & functional regression tests for group B (items B1-B6).

No network, no real Chrome, no AI CLI execution; every test runs in an isolated temp folder.
"""
import atexit
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_BOOT = tempfile.mkdtemp(prefix="cp-r2b-boot-")
atexit.register(shutil.rmtree, _BOOT, True)
os.environ.setdefault("EP_APP_DIR", _BOOT)
os.environ.setdefault("CP_APP_DIR", _BOOT)
os.environ.setdefault("CP_DATA_DIR", os.path.join(_BOOT, "data", "contest"))
os.environ.setdefault("CP_OWNER_DIR", os.path.join(_BOOT, "data", "contest", "board"))
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import engine, paths, server, settings  # noqa: E402
from cp import cli, cmdbase, config, model, policy, preview, review, scheduler, store, workflow  # noqa: E402
from cp.agents import base as agent_base  # noqa: E402


DOSSIER = "\n\n".join(
    f"## {h}\n" + ("x" * 90 if not h.startswith("Summary") else "한국어 요약입니다. 상금과 마감을 안내합니다.")
    for h in ("Contest", "Organizer", "Stated intent", "Hidden intent", "Rules and format",
              "Recommended approach", "Needs from the user", "Summary (Korean)")
)


def cp(*argv):
    return cli.call([str(x) for x in argv])


def future_day(days: int = 10) -> str:
    return (model.today_kst() + timedelta(days=days)).isoformat()


class FakeAdapter(agent_base.Adapter):
    def __init__(self, name: str):
        self.name = name

    def command(self, prompt, kind):
        return [sys.executable, "-c", "pass"]


class ContestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp-r2b-")
        self._saved_env = dict(os.environ)
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["CP_APP_DIR"] = self.tmp
        os.environ["CP_DATA_DIR"] = os.path.join(self.tmp, "data", "contest")
        os.environ["CP_OWNER_DIR"] = os.path.join(self.tmp, "data", "contest", "board")
        os.environ["CP_PROFILE_PATH"] = os.path.join(self.tmp, "data", "profile.json")
        os.environ["EP_TEST_NO_SHELL"] = "1"
        os.makedirs(os.environ["CP_DATA_DIR"], exist_ok=True)
        with open(os.environ["CP_PROFILE_PATH"], "w", encoding="utf-8") as f:
            json.dump({"name": "홍길동", "email": "entrant@example.kr", "phone": "010-0000-0000"}, f)
        self.db = store.connect()

    def tearDown(self):
        for p in list(agent_base.PROCS.values()):
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass
        agent_base.PROCS.clear()
        self.db.close()
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_policy(self, **kw):
        path = policy.policy_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kw, f, ensure_ascii=False)

    def add_item(self, **kw) -> str:
        n = self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] + 1
        row = {
            "title": f"공모전 {n}",
            "org": "주최기관",
            "url": f"https://contest.example.kr/item/{n}",
            "deadline": future_day(10),
            "region": "KR",
        }
        row.update(kw)
        r = cp("add", "--agent", "gemini", "--json", json.dumps(row, ensure_ascii=False))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["results"][0]["status"], "added", r)
        return r["results"][0]["id"]

    def verify_proposal(self, iid: str | None = None, top_krw: int | None = 500000,
                        submit_method: str = "email", lane: str = "browser") -> str:
        iid = iid or self.add_item()
        os.makedirs(config.analysis_dir(), exist_ok=True)
        af = os.path.join(config.analysis_dir(), f"{iid}.md")
        with open(af, "w", encoding="utf-8") as f:
            f.write(DOSSIER)
        args = [
            "verify", iid, "--agent", "codex", "--lane", lane, "--work-type", "essay",
            "--theme", "주제", "--format", "2000자", "--submit-method", submit_method,
            "--analysis-file", af,
        ]
        if top_krw is not None:
            args += ["--top-prize-krw", str(top_krw)]
        r = cp(*args)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"], "proposal")
        return iid

    def write_draft(self, iid: str, send: dict | None = None, files: dict[str, bytes | str] | None = None) -> str:
        review.decide(iid, "yes")
        r = cp("draft-claim", "--agent", "codex", "--id", iid)
        self.assertTrue(r["ok"], r)
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        file_map = files if files is not None else {"work.md": "응모 본문입니다."}
        for name, content in file_map.items():
            fp = os.path.join(dd, name)
            if isinstance(content, bytes):
                with open(fp, "wb") as f:
                    f.write(content)
            else:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(content)
        spec = send if send is not None else {
            "how": "이메일",
            "to": "submit@example.kr",
            "subject": "[공모전] 응모작",
            "body": "응모 본문입니다.",
        }
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False)
        return dd


class TestB1AutoReviewAndSubfolders(ContestBase):
    def test_policy_none_records_policy_approval_and_submits_without_loop(self):
        self.write_policy(review_policy="none")
        iid = self.verify_proposal(top_krw=3000000)
        self.write_draft(iid)
        r = cp("draft-done", iid, "--agent", "codex", "--summary", "수필 완성")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"], "ready")

        it = store.get_item(self.db, iid)
        app = it["work"].get("approval")
        self.assertIsInstance(app, dict)
        self.assertEqual(app["by"], "policy")
        self.assertEqual(app["policy"], "none")
        self.assertEqual(app["hash"], workflow.draft_hash(it["work"], iid))
        self.assertEqual(workflow.approved_draft_ok(it, self.db), (True, ""))

        # Scheduler phase_submit must launch submit rather than sending back to review
        sched = scheduler.Scheduler(
            dry_run=False,
            adapters={a: FakeAdapter(a) for a in config.AGENTS},
            db=self.db,
            log=lambda m: None,
        )
        sched.ensure_chrome = lambda: True
        with mock.patch("cp.ram.free_gb", return_value=16.0), \
                mock.patch("cp.accounts.claude_pick", return_value="1"), \
                mock.patch("cp.accounts.codex_home", return_value=self.tmp), \
                mock.patch.object(config, "TRANSLATE_PHASE", False):
            out = sched.loop_once()
        launched = [x for x in out["launched"] if x["kind"] == "submit"]
        self.assertEqual([x["item"] for x in launched], [iid])
        self.assertEqual(store.get_item(self.db, iid)["state"], "submitting")

    def test_policy_big_prize_auto_approves_small_prize_and_holds_big_prize(self):
        self.write_policy(review_policy="big_prize", review_min_prize_krw=1000000)
        small_id = self.verify_proposal(top_krw=300000)
        self.write_draft(small_id)
        r_small = cp("draft-done", small_id, "--agent", "codex", "--summary", "소액 공모작")
        self.assertTrue(r_small["ok"], r_small)
        self.assertEqual(r_small["state"], "ready")
        it_s = store.get_item(self.db, small_id)
        self.assertEqual(it_s["work"]["approval"]["by"], "policy")
        self.assertEqual(it_s["work"]["approval"]["policy"], "big_prize")
        self.assertEqual(workflow.approved_draft_ok(it_s, self.db), (True, ""))

        # Agent-writable draft_dir is isolated from the approved snapshot; snapshot tampering is still caught
        with open(os.path.join(workflow.draft_dir_for(small_id), "work.md"), "w", encoding="utf-8") as f:
            f.write("edited in draft_dir after policy approval")
        self.assertEqual(workflow.approved_draft_ok(store.get_item(self.db, small_id), self.db), (True, ""))
        with open(os.path.join(it_s["work"]["snapshot_dir"], "work.md"), "w", encoding="utf-8") as f:
            f.write("tampered in snapshot after policy approval")
        ok, why = workflow.approved_draft_ok(store.get_item(self.db, small_id), self.db)
        self.assertFalse(ok)
        self.assertIn("changed after approval", why)

        # Big prize waits for human review
        big_id = self.verify_proposal(top_krw=2000000)
        self.write_draft(big_id)
        r_big = cp("draft-done", big_id, "--agent", "codex", "--summary", "고액 공모작")
        self.assertTrue(r_big["ok"], r_big)
        self.assertEqual(r_big["state"], "review")
        self.assertIsNone(store.get_item(self.db, big_id)["work"].get("approved_hash"))

    def test_verify_no_work_and_items_without_draft_files_need_no_hash(self):
        # Exact case from B1 specification
        self.assertEqual(workflow.approved_draft_ok({"work": {"work_type": "essay"}}), (True, ""))

        # Intake item with work_type='essay', verified with --no-work -> goes to ready and claims/submits cleanly
        iid = self.add_item(work_type="essay")
        r = cp("verify", iid, "--agent", "codex", "--lane", "form", "--no-work")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"], "ready")
        it = store.get_item(self.db, iid)
        self.assertEqual(it["work"].get("work_type"), "essay")
        self.assertEqual(workflow.approved_draft_ok(it, self.db), (True, ""))
        c = cp("claim", "--agent", "codex", "--id", iid)
        self.assertTrue(c["ok"], c)
        d = cp("done", iid, "--agent", "codex", "--evidence", "투표 완료")
        self.assertTrue(d["ok"], d)
        self.assertEqual(store.get_item(self.db, iid)["state"], "submitted")

    def test_subfolders_in_draft_do_not_crash_validation_or_approval(self):
        iid = self.verify_proposal()
        dd = self.write_draft(iid)
        sub = os.path.join(dd, "build_assets", "nested")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "helper.svg"), "w", encoding="utf-8") as f:
            f.write("<svg></svg>")

        d_real, files = workflow._validated_draft_files(iid, dd)
        self.assertEqual(sorted(files), ["SEND.json", "work.md"])

        r = cp("draft-done", iid, "--agent", "codex", "--summary", "포스터 초안")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"], "review")

        # Add another subfolder while in review before user approves
        os.makedirs(os.path.join(dd, "extra_dir"), exist_ok=True)
        det = review.detail(self.db, iid)
        self.assertTrue(det["draft_hash"])
        msg = review.decide(iid, "yes", draft_hash=det["draft_hash"], require_hash=True)
        self.assertIn("통과", msg)
        self.assertEqual(store.get_item(self.db, iid)["state"], "ready")


class TestB2FrozenSubmissionAndUiHash(ContestBase):
    def test_frozen_record_cli_and_done_verification(self):
        png_bytes = b"\x89PNG\r\n\x1a\napproved-image-payload"
        png_sha = hashlib.sha256(png_bytes).hexdigest()
        body_text = "안녕하세요, 응모작 제출합니다."
        body_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        iid = self.verify_proposal(submit_method="email")
        self.write_draft(
            iid,
            send={
                "how": "이메일",
                "to": "contest@organizer.kr",
                "subject": "[응모] 홍길동",
                "body": body_text,
                "fields": [["성명", "홍길동"], ["부문", "일반"]],
                "files": ["poster.png"],
            },
            files={"poster.png": png_bytes},
        )
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "포스터 1점")["ok"])
        det = review.detail(self.db, iid)
        review.decide(iid, "yes", draft_hash=det["draft_hash"], require_hash=True)

        # cp get returns frozen_submission, and python -m cp.workflow submission <id> prints it
        got = cp("get", iid)
        self.assertTrue(got["ok"])
        frozen = got["frozen_submission"]
        self.assertEqual(frozen["recipient"], "contest@organizer.kr")
        self.assertEqual(frozen["method"], "email")
        self.assertEqual(frozen["subject"], "[응모] 홍길동")
        self.assertEqual(frozen["body"], body_text)
        self.assertEqual(frozen["fields"], [["성명", "홍길동"], ["부문", "일반"]])
        self.assertEqual(
            frozen["attachments"],
            [{"name": "poster.png", "path": os.path.join(frozen["snapshot_dir"], "poster.png"),
              "sha256": png_sha, "size": len(png_bytes)}],
        )

        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "cp.workflow", "submission", iid],
            cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sub_out = json.loads(proc.stdout.strip())
        self.assertEqual(sub_out["submission"], frozen)

        # Claim and verify `done` rejects any mismatch between reported send and frozen record
        self.assertTrue(cp("claim", "--agent", "codex", "--id", iid)["ok"])

        valid_report = {
            "to": "contest@organizer.kr",
            "method": "email",
            "subject": "[응모] 홍길동",
            "body_sha256": body_sha,
            "fields": [["성명", "홍길동"], ["부문", "일반"]],
            "attachments": [{"name": "poster.png", "sha256": png_sha}],
        }
        for bad_patch in (
            {"to": "attacker@evil.kr"},
            {"method": "form"},
            {"subject": "[응모] 변조됨"},
            {"body_sha256": "0" * 64},
            {"fields": [["성명", "타인"], ["부문", "일반"]]},
            {"attachments": [{"name": "other.png", "sha256": png_sha}]},
            {"attachments": [{"name": "poster.png", "sha256": "f" * 64}]},
            {"attachments": []},
        ):
            bad_rep = {**valid_report, **bad_patch}
            ev = "SENT_JSON:" + json.dumps(bad_rep, ensure_ascii=False) + " | Message sent"
            res = cp("done", iid, "--agent", "codex", "--evidence", ev)
            self.assertFalse(res["ok"], f"should reject {bad_patch}: {res}")

        # Plain-text evidence mentioning an unapproved email is also rejected
        res_plain = cp("done", iid, "--agent", "codex", "--evidence", "Message sent to leak@evil.kr")
        self.assertFalse(res_plain["ok"])

        # Matching report succeeds
        ok_ev = "SENT_JSON:" + json.dumps(valid_report, ensure_ascii=False) + " | Gmail Message sent"
        res_ok = cp("done", iid, "--agent", "codex", "--evidence", ok_ev)
        self.assertTrue(res_ok["ok"], res_ok)
        self.assertEqual(store.get_item(self.db, iid)["state"], "submitted")

    def test_email_submit_playbook_uses_frozen_record_only(self):
        with open(os.path.join(ROOT, "prompts_contest", "playbooks", "email_submit.md"), encoding="utf-8") as f:
            pb = f.read()
        self.assertIn("cp.py submission", pb)
        self.assertIn("--sent-json", pb)
        self.assertNotIn("cp.workflow", pb)
        self.assertNotIn("SENT_JSON:", pb)
        self.assertNotIn("the address in verify_notes/notice", pb)
        self.assertIn("Never read the recipient", pb)

    def test_server_and_ui_require_matching_draft_hash_and_refresh_on_stale(self):
        paths.ensure_dirs()
        settings.update(modes={"events": True, "contests": True})
        engine.ensure_contest_initialized()

        iid = self.verify_proposal()
        dd = self.write_draft(iid)
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "수필")["ok"])

        det1 = server.handle("GET", "/api/contest/detail", {"id": [iid]}, {})
        self.assertTrue(det1["draft_hash"])

        # Missing hash from HTTP decide is refused with stale+refresh
        r_no_hash = server.handle("POST", "/api/contest/decide", {}, {"id": iid, "action": "yes", "note": ""})
        self.assertFalse(r_no_hash["ok"])
        self.assertTrue(r_no_hash.get("stale"))
        self.assertTrue(r_no_hash.get("refresh"))
        self.assertEqual(store.get_item(self.db, iid)["state"], "review")

        # Produce a new candidate snapshot (revise + redraft) after user opened screen -> old hash is refused with stale+refresh
        review.decide(iid, "revise", "문장 다듬기")
        self.assertTrue(cp("draft-claim", "--agent", "codex", "--id", iid)["ok"])
        with open(os.path.join(dd, "work.md"), "w", encoding="utf-8") as f:
            f.write("사용자가 본 뒤 바뀐 내용")
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump({"how": "이메일", "to": "submit@example.kr", "subject": "[공모전] 응모작",
                       "body": "사용자가 본 뒤 바뀐 내용"}, f, ensure_ascii=False)
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "수정 수필")["ok"])
        r_stale = server.handle("POST", "/api/contest/decide", {},
                                {"id": iid, "action": "yes", "note": "", "draft_hash": det1["draft_hash"]})
        self.assertFalse(r_stale["ok"])
        self.assertTrue(r_stale.get("stale"))
        self.assertTrue(r_stale.get("refresh"))
        self.assertEqual(store.get_item(self.db, iid)["state"], "review")

        # Re-read detail and approve with refreshed hash -> succeeds
        det2 = server.handle("GET", "/api/contest/item", {"id": [iid]}, {})
        self.assertNotEqual(det1["draft_hash"], det2["draft_hash"])
        r_ok = server.handle("POST", "/api/contest/decide", {},
                             {"id": iid, "action": "yes", "note": "", "draft_hash": det2["draft_hash"]})
        self.assertTrue(r_ok["ok"], r_ok)
        self.assertEqual(store.get_item(self.db, iid)["state"], "ready")

        # UI JS passes draft_hash and refreshes on stale/refresh
        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("data-cx-hash=", js)
        self.assertIn("payload.draft_hash", js)
        self.assertIn("r.stale || r.refresh", js)


class TestB3SetAfterApproval(ContestBase):
    def test_set_submission_fields_after_approval_clears_approval_or_refuses(self):
        iid = self.verify_proposal(submit_method="form")
        self.write_draft(iid, send={"how": "누리집 양식 입력", "to": "https://contest.example.kr/apply",
                                    "fields": [["제목", "작품"]]})
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "양식 응모작")["ok"])
        review.decide(iid, "yes")
        self.assertEqual(store.get_item(self.db, iid)["state"], "ready")
        self.assertTrue(store.get_item(self.db, iid)["work"].get("approved_hash"))

        # Non-submission tracking fields stay allowed without dropping approval
        r_benign = cp("set", iid, "--agent", "codex", "--result-date", "2026-12-30", "--title-en", "Title EN")
        self.assertTrue(r_benign["ok"], r_benign)
        self.assertEqual(store.get_item(self.db, iid)["state"], "ready")
        self.assertTrue(store.get_item(self.db, iid)["work"].get("approved_hash"))

        # Changing submit_method after approval clears approved_hash & frozen_submission and sends back to review
        r_mut = cp("set", iid, "--agent", "codex", "--submit-method", "email")
        self.assertTrue(r_mut["ok"], r_mut)
        it_after = store.get_item(self.db, iid)
        self.assertEqual(it_after["state"], "review")
        self.assertIsNone(it_after["work"].get("approved_hash"))
        self.assertIsNone(it_after["work"].get("approval"))
        self.assertIsNone(it_after["work"].get("frozen_submission"))
        self.assertIsNone(store.kv_get(self.db, workflow.FROZEN_KV_PREFIX + iid))
        self.assertFalse(cp("claim", "--agent", "codex", "--id", iid)["ok"])

        # Re-approve and claim -> while submitting, changing submission fields is refused outright
        review.decide(iid, "yes")
        self.assertTrue(cp("claim", "--agent", "codex", "--id", iid)["ok"])
        for flag_args in (
            ["--submit-method", "form"],
            ["--verify-notes", "redirect to evil@example.kr"],
            ["--source-url", "https://evil.example/notice"],
            ["--eligibility", "residency_required"],
        ):
            r_sub = cp("set", iid, "--agent", "codex", *flag_args)
            self.assertFalse(r_sub["ok"], f"must refuse {flag_args} during submitting")


class TestB4PdfPreviewSecurity(ContestBase):
    def test_preview_cache_is_app_owned_keyed_by_sha256_and_ignores_dot_preview(self):
        iid = self.verify_proposal()
        dd = self.write_draft(iid, send={"how": "이메일", "to": "a@example.kr", "files": ["entry.pdf"]})
        pdf_path = os.path.join(dd, "entry.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "Real PDF Page 1")
        doc.save(pdf_path)
        doc.close()

        # Poison <draft>/.preview with fake PNG files stamped in the future
        poison_dir = os.path.join(dd, ".preview")
        os.makedirs(poison_dir, exist_ok=True)
        fake_bytes = b"\x89PNG\r\n\x1a\nFAKE-POISON-PREVIEW"
        future = time.time() + 7200
        for fn in ("entry.pdf-0.png", "page-0.png"):
            p = os.path.join(poison_dir, fn)
            with open(p, "wb") as f:
                f.write(fake_bytes)
            os.utime(p, (future, future))

        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "PDF 제출")["ok"])
        png1 = review.preview_png(self.db, iid, "entry.pdf", 0)
        self.assertIsNotNone(png1)
        self.assertNotEqual(png1, fake_bytes)
        self.assertTrue(png1.startswith(b"\x89PNG\r\n\x1a\n"))

        with open(pdf_path, "rb") as f:
            sha1 = hashlib.sha256(f.read()).hexdigest()
        expected_cache = os.path.join(config.data_dir(), "preview-cache", sha1, "page-0.png")
        self.assertTrue(os.path.isfile(expected_cache))

        # Modify PDF bytes in draft_dir with an OLDER mtime -> review.preview_png stays pinned to the snapshot,
        # while page_png on the modified file still renders the new PDF keyed by SHA-256 (not mtime)
        doc2 = pymupdf.open()
        p2 = doc2.new_page()
        p2.draw_rect(p2.rect, color=(0, 0, 0), fill=(0, 0, 0))
        doc2.save(pdf_path)
        doc2.close()
        past = time.time() - 7200
        os.utime(pdf_path, (past, past))
        self.assertEqual(review.preview_png(self.db, iid, "entry.pdf", 0), png1)
        png2 = preview.page_png(dd, "entry.pdf", 0)
        self.assertIsNotNone(png2)
        self.assertNotEqual(png1, png2)

    def test_real_page_count_and_warning_beyond_30_pages(self):
        iid = self.verify_proposal()
        dd = self.write_draft(iid, send={"how": "이메일", "to": "a@example.kr", "files": ["long.pdf"]})
        doc = pymupdf.open()
        for i in range(35):
            doc.new_page().insert_text((72, 72), f"Page {i + 1}")
        doc.save(os.path.join(dd, "long.pdf"))
        doc.close()
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "35쪽 기획서")["ok"])

        self.assertEqual(preview.page_count(dd, "long.pdf"), 35)
        det = review.detail(self.db, iid)
        imgs = [u for u in det["units"] if u["type"] == "image"]
        warns = [u for u in det["units"] if u["type"] == "warn"]
        self.assertEqual(len(imgs), 30)
        self.assertEqual(imgs[0]["label"], "long.pdf 1/35쪽")
        self.assertEqual(imgs[-1]["label"], "long.pdf 30/35쪽")
        self.assertTrue(any("35쪽" in w["text"] and "30쪽" in w["text"] for w in warns), warns)
        self.assertIsNotNone(review.preview_png(self.db, iid, "long.pdf", 29))
        self.assertIsNone(review.preview_png(self.db, iid, "long.pdf", 30))

    def test_pixel_limit_caps_tall_page_rendering(self):
        iid = self.verify_proposal()
        dd = self.write_draft(iid, send={"how": "이메일", "to": "a@example.kr", "files": ["tall.pdf"]})
        doc = pymupdf.open()
        # 50 x 14000 pt -> naive width=1100 scaling (22x) would be 1100 x 308000 = 338.8 MP (> 40 MP)
        doc.new_page(width=50, height=14000).insert_text((5, 50), "tall")
        doc.save(os.path.join(dd, "tall.pdf"))
        doc.close()
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "긴 페이지")["ok"])

        png = review.preview_png(self.db, iid, "tall.pdf", 0)
        self.assertIsNotNone(png)
        w, h = struct.unpack(">II", png[16:24])
        self.assertLessEqual(w * h, preview.MAX_PIXELS)
        self.assertLess(w, preview.WIDTH)  # had to scale down below WIDTH=1100 to stay within 40 MP


class TestB5StreamingHashSafeTextAndCaps(ContestBase):
    def test_text_preview_without_send_json_enforces_safe_draft_file(self):
        iid = self.verify_proposal()
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("SECRET-OUTSIDE")
        with open(os.path.join(dd, "work.md"), "w", encoding="utf-8") as f:
            f.write("안전한 본문")
        junc = os.path.join(dd, "linked_dir")
        outside_dir = os.path.join(self.tmp, "out_dir")
        os.makedirs(outside_dir, exist_ok=True)
        with open(os.path.join(outside_dir, "leak.txt"), "w", encoding="utf-8") as f:
            f.write("LEAKED")
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", junc, outside_dir], capture_output=True, check=True)

        store.update_item(self.db, iid, state="review", work={
            **store.get_item(self.db, iid)["work"],
            "draft_dir": dd,
            "draft_files": ["work.md", r"..\outside.txt", r"..\..\outside.txt", r"linked_dir\leak.txt"],
        })
        det = review.detail(self.db, iid)
        texts = [u["text"] for u in det["units"] if u["type"] == "text"]
        self.assertEqual(texts, ["안전한 본문"])
        os.rmdir(junc)

    def test_streaming_hash_and_file_count_and_size_caps(self):
        iid = self.verify_proposal()
        big_chunk_data = b"abcd1234" * 40000  # 320 KB > 64 KB HASH_CHUNK
        dd = self.write_draft(iid, send={"how": "이메일", "to": "a@example.kr", "body": "ok", "files": ["pic.png"]},
                              files={"pic.png": b"\x89PNG\r\n\x1a\n" + big_chunk_data})
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "정상 크기")["ok"])
        h = workflow.draft_hash(store.get_item(self.db, iid)["work"], iid)
        self.assertEqual(len(h), 64)

        # > 50 files rejected with clear error
        iid_many = self.verify_proposal()
        many_files = {f"extra_{i:02d}.txt": "x" for i in range(51)}
        self.write_draft(iid_many, files=many_files)
        r_many = cp("draft-done", iid_many, "--agent", "codex", "--summary", "파일 과다")
        self.assertFalse(r_many["ok"])
        self.assertIn("50", r_many["error"])

        # > 200 MB single file rejected with clear error (sparse file on NTFS uses zero disk blocks)
        iid_huge = self.verify_proposal()
        dd_huge = self.write_draft(iid_huge)
        huge_path = os.path.join(dd_huge, "huge.png")
        with open(huge_path, "wb") as f:
            f.seek(preview.MAX_FILE_BYTES + 1)
            f.write(b"\x00")
        r_huge = cp("draft-done", iid_huge, "--agent", "codex", "--summary", "초대용량")
        self.assertFalse(r_huge["ok"])
        self.assertIn("200 MB", r_huge["error"])


class TestB6SendJsonValidationAndRevise(ContestBase):
    def test_send_json_types_checked_at_draft_done_and_list_of_to_accepted(self):
        iid_ok = self.verify_proposal()
        self.write_draft(iid_ok, send={
            "how": "이메일",
            "to": ["first@example.kr", "second@example.kr"],
            "subject": "복수 수신",
            "body": "본문",
            "fields": [["성명", "홍길동"]],
        })
        r_ok = cp("draft-done", iid_ok, "--agent", "codex", "--summary", "복수 수신자")
        self.assertTrue(r_ok["ok"], r_ok)
        det = review.detail(self.db, iid_ok)
        send_rows = [r["value"] for r in det["rows"] if r["label"] == "보내는 곳"]
        self.assertTrue(any("first@example.kr, second@example.kr" in v for v in send_rows), det["rows"])

        bad_specs = [
            {"how": "이메일", "to": 12345, "body": "본문"},
            {"how": "이메일", "to": [], "body": "본문"},
            {"how": "이메일", "to": ["ok@example.kr", 99], "body": "본문"},
            {"how": ["이메일"], "to": "ok@example.kr", "body": "본문"},
            {"how": "누리집 양식 입력", "to": "https://example.kr", "fields": {"이름": "홍길동"}},
            {"how": "누리집 양식 입력", "to": "https://example.kr", "fields": [["이름", 123]]},
            {"how": "누리집 양식 입력", "to": "https://example.kr", "fields": [["이름"]]},
            {"how": "이메일", "to": "ok@example.kr", "body": "본문", "files": "work.md"},
            {"how": "이메일", "to": "ok@example.kr", "body": "본문", "files": [123]},
            {"how": "이메일", "to": "ok@example.kr", "body": "본문", "files": ["missing.pdf"]},
            {"how": "이메일", "to": "ok@example.kr", "subject": 999, "body": "본문"},
        ]
        iid_bad = self.verify_proposal()
        dd = self.write_draft(iid_bad)
        for spec in bad_specs:
            with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
                json.dump(spec, f, ensure_ascii=False)
            r = cp("draft-done", iid_bad, "--agent", "codex", "--summary", "검증 테스트")
            self.assertFalse(r["ok"], f"spec should fail draft-done: {spec}")

    def test_invalid_send_json_never_crashes_review_and_turns_approval_into_revise(self):
        iid = self.verify_proposal()
        dd = self.write_draft(iid)
        self.assertTrue(cp("draft-done", iid, "--agent", "codex", "--summary", "초안")["ok"])

        # Corrupt SEND.json in snapshot on disk with non-string `to` and invalid `fields` while item is in `review`
        snap_dir = store.get_item(self.db, iid)["work"]["snapshot_dir"]
        with open(os.path.join(snap_dir, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump({"how": "이메일", "to": 98765, "fields": {"bad": 1}, "body": "본문"}, f)

        # Direct sheet_rows and review.detail must NOT crash (e.g. startswith on non-string)
        rows, _ = review.sheet_rows(store.get_item(self.db, iid), {"how": ["bad"], "to": 98765})
        self.assertIsInstance(rows, list)
        det = review.detail(self.db, iid)
        self.assertTrue(any(u["type"] == "warn" and "SEND.json" in u["label"] for u in det["units"]), det["units"])

        # Approving an invalid spec refuses readiness and sends the item back to `draft` as a revise request
        r = server.handle("POST", "/api/contest/decide", {},
                          {"id": iid, "action": "yes", "note": "", "draft_hash": det["draft_hash"]})
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("revised"))
        self.assertTrue(r.get("refresh"))

        it_after = store.get_item(self.db, iid)
        self.assertEqual(it_after["state"], "draft")
        self.assertIn("SEND.json", it_after["work"].get("review_note", ""))
        self.assertEqual(it_after["work"].get("revisions"), 1)
        self.assertIsNone(it_after["work"].get("approved_hash"))


if __name__ == "__main__":
    unittest.main()
