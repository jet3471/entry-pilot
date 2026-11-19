"""Round 3 security & functional regression tests for group B (items B1-B4).

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
import unicodedata
import unittest
import zlib
from datetime import timedelta
from unittest import mock

import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_BOOT = tempfile.mkdtemp(prefix="cp-r3b-boot-")
atexit.register(shutil.rmtree, _BOOT, True)
os.environ.setdefault("EP_APP_DIR", _BOOT)
os.environ.setdefault("CP_APP_DIR", _BOOT)
os.environ.setdefault("CP_DATA_DIR", os.path.join(_BOOT, "data", "contest"))
os.environ.setdefault("CP_OWNER_DIR", os.path.join(_BOOT, "data", "contest", "board"))
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import engine, settings  # noqa: E402
from cp import cli, config, model, policy, preview, prompts, review, scheduler, store, workflow  # noqa: E402
from cp.agents import base as agent_base  # noqa: E402


DOSSIER = "\n\n".join(
    f"## {h}\n" + ("x" * 90 if not h.startswith("Summary") else "한국어 분석 요약입니다. 시상 내역과 제출 방법을 안내합니다.")
    for h in ("Contest", "Organizer", "Stated intent", "Hidden intent", "Rules and format",
              "Recommended approach", "Needs from the user", "Summary (Korean)")
)


def cp(*argv):
    return cli.call([str(x) for x in argv])


def future_day(days: int = 10) -> str:
    return (model.today_kst() + timedelta(days=days)).isoformat()


def make_png_bytes(width: int = 4, height: int = 4) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x20\x60\xa0" * width for _ in range(height))
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class FakeAdapter(agent_base.Adapter):
    def __init__(self, name: str):
        self.name = name

    def command(self, prompt, kind):
        return [sys.executable, "-c", "pass"]


class ContestR3BBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp-r3b-")
        self._saved_env = dict(os.environ)
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["CP_APP_DIR"] = self.tmp
        os.environ["CP_DATA_DIR"] = os.path.join(self.tmp, "data", "contest")
        os.environ["CP_OWNER_DIR"] = os.path.join(self.tmp, "data", "contest", "board")
        os.environ["CP_PROFILE_PATH"] = os.path.join(self.tmp, "data", "profile.json")
        os.environ["EP_TEST_NO_SHELL"] = "1"
        os.makedirs(os.environ["CP_DATA_DIR"], exist_ok=True)
        with open(os.environ["CP_PROFILE_PATH"], "w", encoding="utf-8") as f:
            json.dump({"name": "홍길동", "email": "profile.user@example.kr", "phone": "010-0000-0000"}, f)
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
        seq = self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] + 1
        item = {
            "title": f"[공모전] 테스트 공모전 {seq}",
            "org": "주최기관",
            "url": f"https://contest.example.kr/item-{seq}-{os.urandom(3).hex()}",
            "region": "KR",
            "deadline": future_day(10),
        }
        item.update(kw)
        r = cp("add", "--agent", "gemini", "--json", json.dumps(item, ensure_ascii=False))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["results"][0]["status"], "added", r)
        return r["results"][0]["id"]

    def write_dossier(self, name: str = "analysis.md", text: str = DOSSIER) -> str:
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def make_pdf(self, path: str, text: str = "Contest Work Page") -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = pymupdf.open()
        page = doc.new_page(width=420, height=300)
        page.insert_text((36, 72), text, fontsize=15)
        doc.save(path)
        doc.close()
        return path

    def verify_and_accept(self, iid: str, *, lane: str = "browser", work_type: str = "essay",
                          submit_method: str = "email", prize_krw: int | None = 300_000,
                          top_prize: str | None = "상금 30만원") -> None:
        df = self.write_dossier(f"dossier-{iid}.md")
        args = ["verify", iid, "--agent", "claude", "--lane", lane, "--work-type", work_type,
                "--submit-method", submit_method, "--analysis-file", df]
        if prize_krw is not None:
            args += ["--top-prize-krw", str(prize_krw)]
        if top_prize is not None:
            args += ["--top-prize", top_prize]
        r = cp(*args)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"], "proposal")
        msg = review.decide(iid, "yes")
        self.assertIn("통과", msg)
        it = store.get_item(self.db, iid)
        self.assertEqual(it["state"], "draft")

    def draft_candidate(self, iid: str, *, send_spec: dict | None = None,
                        pdf_name: str | None = "entry.pdf", pdf_text: str = "Approved Draft v1",
                        extra_files: dict[str, bytes] | None = None) -> tuple[str, dict]:
        r = cp("draft-claim", "--agent", "claude", "--id", iid)
        self.assertTrue(r["ok"], r)
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        if pdf_name:
            self.make_pdf(os.path.join(dd, pdf_name), pdf_text)
        for fname, data in (extra_files or {}).items():
            with open(os.path.join(dd, fname), "wb") as f:
                f.write(data)
        if send_spec is None:
            send_spec = {
                "how": "이메일",
                "to": "apply@contest.example.kr",
                "to_source": "공모전 요강 접수처 안내",
                "subject": "[응모] 홍길동 작품 제출",
                "body": "안녕하세요.\n첨부파일로 응모작을 제출합니다.",
                "fields": [],
                "files": [pdf_name] if pdf_name else [],
            }
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump(send_spec, f, ensure_ascii=False, indent=2)
        r2 = cp("draft-done", iid, "--agent", "claude", "--summary", "초안 작성 완료")
        self.assertTrue(r2["ok"], r2)
        return dd, r2

    def approve_in_ui(self, iid: str) -> dict:
        d = review.detail(self.db, iid)
        self.assertEqual(d["item"]["state"], "review")
        self.assertTrue(d.get("draft_hash"))
        res = engine.contest_decide(iid, "yes", draft_hash=d["draft_hash"])
        self.assertTrue(res["ok"], res)
        it = store.get_item(self.db, iid)
        self.assertEqual(it["state"], "ready")
        return workflow.get_frozen_submission(self.db, it)


# ==============================================================================
# B1: Required --sent-json, URL/recipient/body/field normalisation, unified CLI
# ==============================================================================
class TestR3BB1DoneAndNormalisation(ContestR3BBase):
    def test_b1_done_requires_sent_json_and_keeps_evidence_fallback(self):
        self.write_policy(review_policy="all")
        iid = self.add_item()
        self.verify_and_accept(iid, lane="browser", submit_method="email")
        self.draft_candidate(iid)
        frozen = self.approve_in_ui(iid)

        # Unified CLI command: python -X utf8 cp.py submission <ID>
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(ROOT, "cp.py"), "submission", iid],
            cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        sub_out = json.loads(proc.stdout.strip())
        self.assertTrue(sub_out["ok"])
        self.assertEqual(sub_out["submission"]["draft_hash"], frozen["draft_hash"])

        self.assertTrue(cp("claim", "--id", iid, "--agent", "codex")["ok"])

        # Without --sent-json, cp done MUST be refused with a clear message.
        no_rep = cp("done", iid, "--agent", "codex", "--evidence", "Gmail 보낸편지함 확인 완료")
        self.assertFalse(no_rep["ok"])
        self.assertIn("--sent-json", no_rep["error"])
        self.assertEqual(store.get_item(self.db, iid)["state"], "submitting")

        # Valid --sent-json passes.
        valid_rep = {
            "recipient": frozen["recipient"],
            "method": "email",
            "subject": frozen["subject"],
            "body": frozen["body"],
            "fields": [],
            "attachments": [{"name": a["name"], "sha256": a["sha256"]} for a in frozen["attachments"]],
        }
        ok_res = cp("done", iid, "--agent", "codex", "--evidence", "Gmail 발송 완료",
                    "--sent-json", json.dumps(valid_rep, ensure_ascii=False))
        self.assertTrue(ok_res["ok"], ok_res)
        self.assertEqual(store.get_item(self.db, iid)["state"], "submitted")

        # Fallback: SENT_JSON: embedded in --evidence remains accepted in code.
        iid2 = self.add_item()
        self.verify_and_accept(iid2, lane="browser", submit_method="email")
        self.draft_candidate(iid2)
        frozen2 = self.approve_in_ui(iid2)
        self.assertTrue(cp("claim", "--id", iid2, "--agent", "codex")["ok"])
        rep2 = {
            "recipient": frozen2["recipient"],
            "method": "email",
            "subject": frozen2["subject"],
            "body": frozen2["body"],
            "fields": [],
            "attachments": [{"name": a["name"], "sha256": a["sha256"]} for a in frozen2["attachments"]],
        }
        ev_fallback = "발송 완료 SENT_JSON: " + json.dumps(rep2, ensure_ascii=False)
        ok_fb = cp("done", iid2, "--agent", "codex", "--evidence", ev_fallback)
        self.assertTrue(ok_fb["ok"], ok_fb)

    def test_b1_url_normalisation_short_links_form_ids_and_https_strictness(self):
        # Host lowercasing and leading www. stripping
        self.assertTrue(model.same_submit_url("https://WWW.Contest-Host.KR/apply/", "https://contest-host.kr/apply"))
        self.assertTrue(model.same_submit_url("https://contest-host.kr/apply", "https://www.CONTEST-HOST.kr/apply/"))

        # Short links & redirects treated as the same form
        self.assertTrue(model.same_submit_url(
            "https://forms.gle/ShortSlug123",
            "https://docs.google.com/forms/d/e/1FAIpQLSeTargetFormId999/viewform?usp=sf_link",
        ))
        self.assertTrue(model.same_submit_url(
            "https://forms.gle/ShortSlug123",
            "https://docs.google.com/forms/d/e/1FAIpQLSeTargetFormId999/formResponse",
        ))
        self.assertTrue(model.same_submit_url(
            "https://goo.gl/forms/GooSlug777",
            "https://docs.google.com/forms/d/e/1FAIpQLSeTargetFormId777/viewform",
        ))
        self.assertEqual(model.site_kind("https://goo.gl/forms/GooSlug777"), "google_form")
        self.assertTrue(model.same_submit_url(
            "https://naver.me/NaverSlug555",
            "https://form.naver.com/response/NaverDocId555",
        ))

        # Different form identifiers MUST be rejected when both have IDs
        self.assertFalse(model.same_submit_url(
            "https://docs.google.com/forms/d/e/FormIdAAA/viewform",
            "https://docs.google.com/forms/d/e/FormIdBBB/formResponse",
        ))
        self.assertFalse(model.same_submit_url("https://forms.gle/SlugAAA", "https://forms.gle/SlugBBB"))
        self.assertFalse(model.same_submit_url(
            "https://form.naver.com/response/DocAAA",
            "https://form.naver.com/response/DocBBB",
        ))
        self.assertFalse(model.same_submit_url("https://forms.gle/SlugAAA", "https://docs.google.com/forms/"))

        # http vs https of the same host is NOT the same (require https when frozen is https)
        self.assertFalse(model.same_submit_url("https://contest.example.kr/apply", "http://contest.example.kr/apply"))
        self.assertFalse(model.same_submit_url("http://contest.example.kr/apply", "https://contest.example.kr/apply"))
        self.assertFalse(model.same_submit_url(
            "https://forms.gle/ShortSlug123",
            "http://docs.google.com/forms/d/e/1FAIpQLSeTargetFormId999/viewform",
        ))

        # End-to-end check on cp done with forms.gle frozen -> docs.google.com/forms sent
        self.write_policy(review_policy="all")
        iid = self.add_item(url="https://forms.gle/AbcForm99")
        self.verify_and_accept(iid, lane="form", work_type="idea", submit_method="form")
        self.draft_candidate(iid, pdf_name=None, send_spec={
            "how": "누리집 양식 입력",
            "to": "https://forms.gle/AbcForm99",
            "to_source": "공모전 공식 구글폼 단축 링크",
            "subject": "",
            "body": "",
            "fields": [["성명", "홍길동"], ["참가 부문", "일반부"]],
            "files": [],
        })
        self.approve_in_ui(iid)
        self.assertTrue(cp("claim", "--id", iid, "--agent", "codex")["ok"])

        # http downgrade rejected
        bad_http = cp("done", iid, "--agent", "codex", "--evidence", "응답 기록 완료", "--sent-json", json.dumps({
            "recipient": "http://docs.google.com/forms/d/e/1FAIpQLSeAbc99/formResponse",
            "method": "form",
            "fields": [["성명", "홍길동"], ["참가 부문", "일반부"]],
            "attachments": [],
        }, ensure_ascii=False))
        self.assertFalse(bad_http["ok"])

        # https redirect URL accepted
        good_https = cp("done", iid, "--agent", "codex", "--evidence", "응답이 기록되었습니다", "--sent-json", json.dumps({
            "recipient": "https://docs.google.com/forms/d/e/1FAIpQLSeAbc99/formResponse",
            "method": "form",
            "fields": [["성명", "홍길동"], ["참가 부문", "일반부"]],
            "attachments": [],
        }, ensure_ascii=False))
        self.assertTrue(good_https["ok"], good_https)

    def test_b1_recipient_check_allows_sender_emails_in_free_text_evidence(self):
        settings.save({"email_address": "my.sender.account@gmail.com"})
        self.write_policy(review_policy="all")
        iid = self.add_item()
        self.verify_and_accept(iid, lane="browser", submit_method="email")
        self.draft_candidate(iid, send_spec={
            "how": "이메일",
            "to": ["Submit@Contest.Example.kr", "cc-box@contest.example.kr"],
            "to_source": "요강 접수 이메일 안내",
            "subject": "[제출] 공모작",
            "body": "본문입니다.",
            "fields": [],
            "files": ["entry.pdf"],
        })
        frozen = self.approve_in_ui(iid)
        self.assertTrue(cp("claim", "--id", iid, "--agent", "codex")["ok"])

        # Wrong / extra recipient in --sent-json MUST fail.
        wrong_to = cp("done", iid, "--agent", "codex", "--evidence", "발송 완료", "--sent-json", json.dumps({
            "recipient": ["submit@contest.example.kr", "intruder@other.example.kr"],
            "method": "email",
            "subject": "[제출] 공모작",
            "body": "본문입니다.",
            "attachments": [{"name": "entry.pdf", "sha256": frozen["file_hashes"]["entry.pdf"]}],
        }, ensure_ascii=False))
        self.assertFalse(wrong_to["ok"])

        # Mentioning sender's own Gmail (settings email_address), profile email, or inquiry email in --evidence MUST NOT fail.
        ev_with_sender = (
            "발신 계정 my.sender.account@gmail.com (프로필 profile.user@example.kr) 에서 "
            "submit@contest.example.kr 및 cc-box@contest.example.kr 로 전송 완료 (문의처 helpdesk@contest.example.kr)"
        )
        ok_to = cp("done", iid, "--agent", "codex", "--evidence", ev_with_sender, "--sent-json", json.dumps({
            "recipient": ["cc-box@contest.example.kr", "submit@contest.example.kr"],
            "method": "email",
            "subject": "[제출] 공모작",
            "body": "본문입니다.",
            "attachments": [{"name": "entry.pdf", "sha256": frozen["file_hashes"]["entry.pdf"]}],
        }, ensure_ascii=False))
        self.assertTrue(ok_to["ok"], ok_to)

    def test_b1_body_nfc_whitespace_and_field_string_coercion(self):
        self.write_policy(review_policy="all")
        iid = self.add_item()
        self.verify_and_accept(iid, lane="form", work_type="idea", submit_method="form")
        nfc_body = "가나다라마바사 응모 본문\n두 번째 줄입니다."
        self.draft_candidate(iid, pdf_name=None, send_spec={
            "how": "누리집 양식 입력",
            "to": "https://www.portal.example.kr/contest/apply?no=42",
            "to_source": "공모전 접수 페이지",
            "subject": "",
            "body": nfc_body,
            "fields": [["팀 인원", "3"], ["개인정보 동의", "True"], ["예상 점수", "98.5"]],
            "files": [],
        })
        self.approve_in_ui(iid)
        self.assertTrue(cp("claim", "--id", iid, "--agent", "codex")["ok"])

        # Non-matching field value fails even after str() conversion
        bad_field = cp("done", iid, "--agent", "codex", "--evidence", "제출", "--sent-json", json.dumps({
            "recipient": "https://portal.example.kr/contest/apply?no=42",
            "method": "form",
            "body": nfc_body,
            "fields": [["팀 인원", 4], ["개인정보 동의", True], ["예상 점수", 98.5]],
            "attachments": [],
        }, ensure_ascii=False))
        self.assertFalse(bad_field["ok"])

        # NFD Hangul + CRLF + NBSP + trailing spaces in body AND int/bool/float field values pass!
        nfd_crlf_body = unicodedata.normalize("NFD", "  가나다라마바사\u00a0  응모   본문  \r\n두 번째 줄입니다.   \r\n")
        ok_norm = cp("done", iid, "--agent", "codex", "--evidence", "제출 완료", "--sent-json", json.dumps({
            "recipient": "https://portal.example.kr/contest/apply?no=42",
            "method": "form",
            "body": nfd_crlf_body,
            "fields": [["팀 인원", 3], ["개인정보 동의", True], ["예상 점수", 98.5]],
            "attachments": [],
        }, ensure_ascii=False))
        self.assertTrue(ok_norm["ok"], ok_norm)

    def test_b1_prompts_use_unified_commands_and_include_subject(self):
        self.write_policy(review_policy="all")
        iid = self.add_item()
        self.verify_and_accept(iid, lane="browser", submit_method="email")
        self.draft_candidate(iid)
        self.approve_in_ui(iid)
        it = store.get_item(self.db, iid)

        rendered = prompts.build("submit", it, {"agent": "codex"})
        self.assertIn(f"cp.py submission {iid}", rendered)
        self.assertIn("--sent-json", rendered)
        self.assertIn('"subject"', rendered)
        self.assertNotIn("cp.workflow", rendered)
        self.assertNotIn("SENT_JSON:", rendered)

        pb_dir = os.path.join(ROOT, "prompts_contest", "playbooks")
        with open(os.path.join(pb_dir, "email_submit.md"), encoding="utf-8") as f:
            email_pb = f.read()
        self.assertIn("cp.py submission {{ID}}", email_pb)
        self.assertIn("--sent-json", email_pb)
        self.assertIn('"subject"', email_pb)
        self.assertNotIn("cp.workflow", email_pb)
        self.assertNotIn("SENT_JSON:", email_pb)

        for pb_name in ("google_form.md", "naver_form.md", "moaform.md", "nownsurvey.md", "site_form.md"):
            with open(os.path.join(pb_dir, pb_name), encoding="utf-8") as f:
                txt = f.read()
            submit_sec = prompts._section(txt, "submit")
            self.assertTrue(submit_sec, pb_name)
            self.assertIn("cp.py submission {{ID}}", submit_sec, pb_name)
            self.assertIn("--sent-json", submit_sec, pb_name)
            self.assertNotIn("cp.workflow", submit_sec, pb_name)
            self.assertNotIn("SENT_JSON:", submit_sec, pb_name)


# ==============================================================================
# B2: verify --no-work submit instruction & pre-R2 re-approval flow
# ==============================================================================
class TestR3BB2NoWorkAndPreR2Migration(ContestR3BBase):
    def test_b2_no_work_item_has_own_submit_prompt_and_submits_without_frozen_record(self):
        iid = self.add_item(title="[퀴즈] 안전 상식 퀴즈", url="https://forms.gle/QuizNoWork1")
        vr = cp("verify", iid, "--agent", "claude", "--no-work", "--lane", "form", "--notes", "3문항 객관식 퀴즈")
        self.assertTrue(vr["ok"], vr)
        self.assertEqual(vr["state"], "ready")

        it = store.get_item(self.db, iid)
        rendered = prompts.build("submit", it, {"agent": "codex"})
        # Must NOT instruct the agent to fetch a frozen submission record or pass --sent-json anywhere in the prompt!
        self.assertNotIn("submission " + iid, rendered)
        self.assertNotIn("cp.py submission", rendered)
        self.assertNotIn("--sent-json", rendered)
        self.assertIn("--evidence", rendered)

        self.assertTrue(cp("claim", "--id", iid, "--agent", "codex")["ok"])
        done_res = cp("done", iid, "--agent", "codex", "--evidence", "응답이 기록되었습니다 (정답 제출 완료)")
        self.assertTrue(done_res["ok"], done_res)
        self.assertEqual(store.get_item(self.db, iid)["state"], "submitted")

    def _make_pre_r2_item(self, *, lane: str = "form") -> str:
        iid = self.add_item()
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        self.make_pdf(os.path.join(dd, "legacy.pdf"), "Legacy Pre-R2 Draft")
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump({
                "how": "누리집 양식 입력",
                "to": "https://portal.example.kr/legacy/apply",
                "to_source": "레거시 공고문",
                "subject": "",
                "body": "",
                "fields": [["이름", "홍길동"]],
                "files": ["legacy.pdf"],
            }, f, ensure_ascii=False)
        legacy_hash = workflow._compute_folder_hash(dd, ["SEND.json", "legacy.pdf"])
        store.update_item(
            self.db, iid, state="ready", lane=lane,
            work={
                "work_type": "essay",
                "submit_method": "form",
                "top_prize_krw": 200_000,
                "accepted_at": "2026-01-01T10:00:00+09:00",
                "accepted_by": "owner",
                "draft_dir": dd,
                "draft_files": ["SEND.json", "legacy.pdf"],
                "approved_by": "owner",
                "approved_at": "2026-01-01T11:00:00+09:00",
                "approved_hash": legacy_hash,
                "approval": {"by": "owner", "hash": legacy_hash, "at": "2026-01-01T11:00:00+09:00"},
            },
        )
        store.kv_del(self.db, workflow.FROZEN_KV_PREFIX + iid)
        return iid

    def test_b2_pre_r2_approved_draft_sent_back_to_review_once_on_claim_and_scheduler(self):
        # 1) Direct claim --id sends item back to review once with "다시 확인해 주세요"
        iid1 = self._make_pre_r2_item(lane="form")
        c1 = cp("claim", "--id", iid1, "--agent", "codex")
        self.assertFalse(c1["ok"])
        self.assertEqual(c1.get("state"), "review")
        self.assertIn("다시 확인해 주세요", c1.get("error", ""))

        it1 = store.get_item(self.db, iid1)
        self.assertEqual(it1["state"], "review")
        self.assertIn("다시 확인해 주세요", it1.get("notes") or "")
        self.assertEqual((it1.get("work") or {}).get("reapproval_note"), "다시 확인해 주세요")
        det1 = review.detail(self.db, iid1)
        self.assertIn("다시 확인해 주세요", det1["note"])

        # Once user re-approves in UI, frozen record is created and claim + done succeed!
        frozen1 = self.approve_in_ui(iid1)
        self.assertTrue(cp("claim", "--id", iid1, "--agent", "codex")["ok"])
        d1 = cp("done", iid1, "--agent", "codex", "--evidence", "제출 완료", "--sent-json", json.dumps({
            "recipient": "https://portal.example.kr/legacy/apply",
            "method": "form",
            "fields": [["이름", "홍길동"]],
            "attachments": [{"name": "legacy.pdf", "sha256": frozen1["file_hashes"]["legacy.pdf"]}],
        }, ensure_ascii=False))
        self.assertTrue(d1["ok"], d1)

        # 2) Auto-claim by lane skips & converts pre-R2 item to review once
        iid2 = self._make_pre_r2_item(lane="form")
        c2 = cp("claim", "--lane", "form", "--agent", "codex")
        self.assertTrue(c2["ok"])
        self.assertIsNone(c2["item"])
        self.assertEqual(store.get_item(self.db, iid2)["state"], "review")
        self.assertIn("다시 확인해 주세요", store.get_item(self.db, iid2).get("notes") or "")

        # 3) Scheduler phase_submit sends pre-R2 item back to review once
        self.write_policy(ram_floor_gb=0)
        iid3 = self._make_pre_r2_item(lane="form")
        sched = scheduler.Scheduler(dry_run=False, db=self.db, log=lambda _: None)
        g = sched.gates()
        summary = {"launched": [], "would": [], "reaped": [], "actions": [], "notes": [], "stopped": False}
        sched.phase_submit(g, summary)
        self.assertEqual(store.get_item(self.db, iid3)["state"], "review")
        self.assertIn("다시 확인해 주세요", store.get_item(self.db, iid3).get("notes") or "")


# ==============================================================================
# B3: Freeze accepted_top_prize_krw, unknown prize review, post-accept change
# ==============================================================================
class TestR3BB3PrizeFreezeAndPolicy(ContestR3BBase):
    def test_b3_unknown_prize_requires_human_review_and_blocks_auto_accept(self):
        # Even under review_policy="big_prize" or "none", unknown prize (None or 0) MUST require human review.
        for rev_pol in ("big_prize", "none"):
            self.write_policy(review_policy=rev_pol, review_min_prize_krw=1_000_000, proposal_policy="auto")
            for raw_prize in (None, 0):
                iid = self.add_item()
                df = self.write_dossier(f"d-{iid}.md")
                v_args = ["verify", iid, "--agent", "claude", "--lane", "browser", "--work-type", "essay",
                          "--submit-method", "email", "--analysis-file", df]
                if raw_prize is not None:
                    v_args += ["--top-prize-krw", str(raw_prize)]
                self.assertTrue(cp(*v_args)["ok"])

                # Scheduler phase_accept must NOT auto-accept unknown prize proposals.
                sched = scheduler.Scheduler(dry_run=False, db=self.db)
                g = sched.gates()
                s = {"launched": [], "would": [], "reaped": [], "actions": [], "notes": [], "stopped": False}
                sched.phase_accept(g, s)
                self.assertEqual(store.get_item(self.db, iid)["state"], "proposal")

                # Even after owner accepts the analysis with unknown prize, draft-done MUST go to review!
                review.decide(iid, "yes")
                it_acc = store.get_item(self.db, iid)
                self.assertIn("accepted_top_prize_krw", it_acc["work"])
                self.assertIsNone(it_acc["work"]["accepted_top_prize_krw"])
                self.assertTrue(policy.needs_review(it_acc))
                self.assertTrue(policy.is_big(it_acc))

                _, r_done = self.draft_candidate(iid)
                self.assertEqual(r_done["state"], "review")
                self.assertEqual(store.get_item(self.db, iid)["state"], "review")

    def test_b3_frozen_prize_controls_policy_and_later_change_sends_back_to_review(self):
        self.write_policy(review_policy="big_prize", review_min_prize_krw=1_000_000)

        # Case A: Accepted with big prize (2,000,000 KRW); agent lowers top_prize_krw during draft -> STILL goes to review!
        iid_big = self.add_item()
        self.verify_and_accept(iid_big, prize_krw=2_000_000, top_prize="대상 200만원")
        it_big = store.get_item(self.db, iid_big)
        self.assertEqual(it_big["work"]["accepted_top_prize_krw"], 2_000_000)

        s_low = cp("set", iid_big, "--agent", "claude", "--top-prize-krw", "50000")
        self.assertTrue(s_low["ok"], s_low)
        it_after_low = store.get_item(self.db, iid_big)
        self.assertEqual(policy.effective_prize_krw(it_after_low), 2_000_000)
        self.assertTrue(policy.prize_changed_after_accept(it_after_low["work"]))
        self.assertTrue(policy.needs_review(it_after_low))
        self.assertTrue(policy.is_big(it_after_low))

        _, r_big_done = self.draft_candidate(iid_big)
        self.assertEqual(r_big_done["state"], "review")

        # Case B: Accepted with known small prize (300,000 KRW) -> auto-approves under big_prize;
        # ANY later change to prize sends the item back to review and clears approval!
        iid_small = self.add_item()
        self.verify_and_accept(iid_small, prize_krw=300_000, top_prize="상금 30만원")
        _, r_small_done = self.draft_candidate(iid_small)
        self.assertEqual(r_small_done["state"], "ready")
        self.assertIsNotNone(workflow.get_frozen_submission(self.db, store.get_item(self.db, iid_small)))

        s_chg = cp("set", iid_small, "--agent", "claude", "--top-prize-krw", "500000")
        self.assertTrue(s_chg["ok"], s_chg)
        self.assertEqual(s_chg["state"], "review")
        it_rev = store.get_item(self.db, iid_small)
        self.assertEqual(it_rev["state"], "review")
        self.assertIsNone(workflow.get_frozen_submission(self.db, it_rev))

        # Re-approving in UI refreshes accepted_top_prize_krw and allows normal claim.
        self.approve_in_ui(iid_small)
        self.assertEqual(store.get_item(self.db, iid_small)["work"]["accepted_top_prize_krw"], 500_000)
        self.assertTrue(cp("claim", "--id", iid_small, "--agent", "codex")["ok"])


# ==============================================================================
# B4: Full target display, app-owned snapshots, non-PDF attachment previews
# ==============================================================================
class TestR3BB4SnapshotsAndReviewDisplay(ContestR3BBase):
    def test_b4_approval_screen_shows_full_recipients_full_url_and_method(self):
        self.write_policy(review_policy="all")

        # 1) Multi-recipient email: full addresses + method shown without clipping
        iid_em = self.add_item()
        self.verify_and_accept(iid_em, lane="browser", submit_method="email")
        addr1 = "contest-submission-primary-desk-2026@very-long-organizer-foundation.or.kr"
        addr2 = "contest-submission-backup-archive-2026@very-long-organizer-foundation.or.kr"
        self.draft_candidate(iid_em, send_spec={
            "how": "이메일",
            "to": [addr1, addr2],
            "to_source": "요강 5쪽 접수처 안내",
            "subject": "[응모] 작품 제출",
            "body": "제출 본문입니다.",
            "fields": [],
            "files": ["entry.pdf"],
        })
        det_em = review.detail(self.db, iid_em)
        rows_em = {r["label"]: r["value"] for r in det_em["rows"]}
        self.assertIn("이메일", rows_em["보내는 곳"])
        self.assertIn(addr1, rows_em["보내는 곳"])
        self.assertIn(addr2, rows_em["보내는 곳"])
        target_em = [u for u in det_em["units"] if u["type"] == "target"]
        self.assertEqual(len(target_em), 1)
        self.assertEqual(target_em[0]["how"], "이메일")
        self.assertEqual(target_em[0]["to"], [addr1, addr2])

        # 2) Long form URL (>120 chars with path & query): full URL + method shown (never host-only or 90-char clipped)
        iid_fm = self.add_item()
        self.verify_and_accept(iid_fm, lane="form", work_type="idea", submit_method="form")
        full_url = (
            "https://www.national-contest-portal.or.kr/programs/2026/autumn-innovation-challenge/"
            "application-form/step1?track=digital-ai&category=citizen&lang=ko"
        )
        self.draft_candidate(iid_fm, pdf_name=None, send_spec={
            "how": "누리집 양식 입력",
            "to": full_url,
            "to_source": "공모전 공식 누리집 접수 바로가기",
            "subject": "",
            "body": "제안 요약입니다.",
            "fields": [["성명", "홍길동"]],
            "files": [],
        })
        det_fm = review.detail(self.db, iid_fm)
        rows_fm = {r["label"]: r["value"] for r in det_fm["rows"]}
        self.assertEqual(rows_fm["보내는 곳"], f"누리집 양식 입력 → {full_url}")
        target_fm = [u for u in det_fm["units"] if u["type"] == "target"]
        self.assertEqual(len(target_fm), 1)
        self.assertEqual(target_fm[0]["how"], "누리집 양식 입력")
        self.assertEqual(target_fm[0]["to"], [full_url])

        # UI review renderer supports target and attachment units
        with open(os.path.join(ROOT, "ui", "app.js"), encoding="utf-8") as f:
            app_js = f.read()
        self.assertIn('u.type === "target"', app_js)
        self.assertIn('u.type === "attachment"', app_js)

    def test_b4_snapshot_isolates_preview_hash_frozen_record_and_submit_paths_from_draft_edits(self):
        self.write_policy(review_policy="all")
        iid = self.add_item()
        self.verify_and_accept(iid, lane="browser", submit_method="email")
        dd, r_done = self.draft_candidate(iid, pdf_text="Original Approved Snapshot Content")

        it = store.get_item(self.db, iid)
        snap_dir = it["work"]["snapshot_dir"]
        snap_hash = it["work"]["snapshot_hash"]
        expected_root = os.path.realpath(os.path.join(config.data_dir(), "snapshots", iid, snap_hash))
        self.assertEqual(os.path.realpath(snap_dir), expected_root)
        self.assertNotEqual(os.path.realpath(snap_dir), os.path.realpath(dd))

        orig_png = review.preview_png(self.db, iid, "entry.pdf", 0)
        orig_pdf_sha, _ = preview.sha256_file(os.path.join(snap_dir, "entry.pdf"))

        # Mutate drafts/<ID> AFTER draft-done (simulating an agent overwriting files in drafts/<ID>):
        self.make_pdf(os.path.join(dd, "entry.pdf"), "TAMPERED DRAFT FOLDER CONTENT")
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump({
                "how": "이메일",
                "to": "wrong-recipient@other.example.kr",
                "to_source": "tampered",
                "subject": "TAMPERED SUBJECT",
                "body": "TAMPERED BODY",
                "fields": [],
                "files": ["entry.pdf"],
            }, f, ensure_ascii=False)
        with open(os.path.join(dd, "extra.bin"), "wb") as f:
            f.write(b"unapproved-bytes")

        # Review detail, preview PNG, file download path, and folder STILL read from the snapshot!
        det = review.detail(self.db, iid)
        self.assertEqual({r["label"]: r["value"] for r in det["rows"]}["보내는 곳"], "이메일 → apply@contest.example.kr")
        self.assertEqual(review.preview_png(self.db, iid, "entry.pdf", 0), orig_png)
        self.assertEqual(os.path.realpath(review.file_path(self.db, iid, "entry.pdf")),
                         os.path.realpath(os.path.join(snap_dir, "entry.pdf")))
        self.assertEqual(os.path.realpath(review.folder(self.db, iid)), os.path.realpath(snap_dir))

        # Approving in UI succeeds and freezes the snapshot (NOT the mutated draft folder)!
        frozen = self.approve_in_ui(iid)
        self.assertEqual(frozen["recipient"], "apply@contest.example.kr")
        self.assertEqual(frozen["file_hashes"]["entry.pdf"], orig_pdf_sha)
        self.assertEqual(os.path.realpath(frozen["snapshot_dir"]), os.path.realpath(snap_dir))
        self.assertEqual(
            os.path.realpath(frozen["attachments"][0]["path"]),
            os.path.realpath(os.path.join(snap_dir, "entry.pdf")),
        )

        # cp submission <ID> and submit prompt point to snapshot_dir and omit draft_dir!
        sub_res = cp("submission", iid)
        self.assertTrue(sub_res["ok"], sub_res)
        self.assertEqual(os.path.realpath(sub_res["submission"]["snapshot_dir"]), os.path.realpath(snap_dir))
        self.assertEqual(
            os.path.realpath(sub_res["submission"]["attachments"][0]["path"]),
            os.path.realpath(os.path.join(snap_dir, "entry.pdf")),
        )

        it_ready = store.get_item(self.db, iid)
        submit_prompt = prompts.build("submit", it_ready, {"agent": "codex"})
        self.assertIn("snapshot_dir", submit_prompt)
        self.assertNotIn('"draft_dir"', submit_prompt)

    def test_b4_non_pdf_attachments_show_image_or_name_size_sha256_never_decoy_pdf(self):
        self.write_policy(review_policy="all")
        iid = self.add_item()
        self.verify_and_accept(iid, lane="browser", work_type="design", submit_method="email")

        png_bytes = make_png_bytes(6, 6)
        zip_bytes = b"PK\x03\x04fake-archive-payload-for-hash-display"
        expected_zip_sha = hashlib.sha256(zip_bytes).hexdigest()

        r_claim = cp("draft-claim", "--agent", "claude", "--id", iid)
        self.assertTrue(r_claim["ok"], r_claim)
        dd = workflow.draft_dir_for(iid)
        os.makedirs(dd, exist_ok=True)
        with open(os.path.join(dd, "poster.png"), "wb") as f:
            f.write(png_bytes)
        with open(os.path.join(dd, "bundle.zip"), "wb") as f:
            f.write(zip_bytes)
        # Author places a decoy PDF and attempts SEND.json "preview" substitution for bundle.zip:
        self.make_pdf(os.path.join(dd, "decoy_preview.pdf"), "Benign Decoy PDF Never Sent")
        with open(os.path.join(dd, "SEND.json"), "w", encoding="utf-8") as f:
            json.dump({
                "how": "이메일",
                "to": "design@contest.example.kr",
                "to_source": "디자인 공모전 요강",
                "subject": "[디자인] 응모작 제출",
                "body": "포스터 이미지와 원본 압축파일을 제출합니다.",
                "fields": [],
                "files": ["poster.png", "bundle.zip"],
                "preview": {"bundle.zip": "decoy_preview.pdf"},
            }, f, ensure_ascii=False, indent=2)

        r_done = cp("draft-done", iid, "--agent", "claude", "--summary", "포스터 및 번들 작성")
        self.assertTrue(r_done["ok"], r_done)

        det = review.detail(self.db, iid)
        img_units = [u for u in det["units"] if u["type"] == "image"]
        att_units = [u for u in det["units"] if u["type"] == "attachment"]

        # Decoy PDF must NEVER be shown as a preview! Only poster.png image unit is present.
        self.assertEqual([u["label"] for u in img_units], ["poster.png"])
        self.assertFalse(any("decoy_preview.pdf" in str(u) for u in det["units"]))
        # Non-PDF, non-image attachment shows name + size + sha256
        self.assertEqual(len(att_units), 1)
        self.assertEqual(att_units[0]["name"], "bundle.zip")
        self.assertEqual(att_units[0]["size"], len(zip_bytes))
        self.assertEqual(att_units[0]["sha256"], expected_zip_sha)


if __name__ == "__main__":
    unittest.main()
