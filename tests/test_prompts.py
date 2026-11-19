"""Prompt assembly tests. Fixture data only; no real personal data.

Run from the project folder: python -m unittest tests.test_prompts -v
"""
import json
import os
import re
import tempfile
import unittest

from ep import policy, prompts

PROFILE_FIXTURE = "\n".join([
    "name: Test Person",
    "email: test.person@example.com",
    "phone: 010-0000-0000",
    "comment_password: fixturePW123",
    "password: hunter2fixture",
    "naver.password_hint: zzhint",
    "occupation: fixture occupation",
    "job_role: fixture job",
    "student: no",
])
SECRET_BITS = ("comment_password", "fixturePW123", "hunter2fixture", "zzhint")

URLS = {
    "google_form": "https://docs.google.com/forms/d/e/FAKE123/viewform",
    "naver_form": "https://form.naver.com/response/FAKE456",
    "moaform": "https://moaform.com/q/FAKE",
    "youtube": "https://www.youtube.com/@fakechannel",
    "instagram": "https://www.instagram.com/p/FAKEPOST/",
    "x": "https://x.com/fakeorg/status/1",
    "site_form": "https://example.org/event/survey",
    "gleam": "https://gleam.io/FAKE/giveaway",
    "naver_blog": "https://blog.naver.com/fakeblog/2233",
}
AGENTS = ("codex", "claude", "gemini", "grok")
LONG = "Answer the survey, follow the account and leave a comment about the product. " * 12


def make_item(n, region, site_kind, lane="browser", legacy=""):
    return {
        "id": f"E9{n:03d}", "region": region, "title": f"Fixture event {n}", "org": "Fixture Org",
        "url": URLS[site_kind], "source_url": "https://example.org/notice/1", "alt_urls": [],
        "form_type": {"google_form": "google", "naver_form": "naver"}.get(site_kind, "site"),
        "site_kind": site_kind, "lane": lane, "deadline": "2026-11-20", "deadline_time": "23:59",
        "prize": "gift card 5,000", "conditions": LONG, "login_required": 0, "eligibility": "unknown",
        "verify_notes": LONG, "legacy_notes": legacy,
        "answers": {"How did you hear about us": "search engine", "Satisfaction": "satisfied"},
        "flags": {}, "state": "submitting",
    }


class PromptFiles(unittest.TestCase):
    def test_files_within_budget(self):
        rep = prompts.file_report()
        self.assertGreaterEqual(len(rep), 4 + 3 + 16)
        for rel, (n, budget) in rep.items():
            self.assertLessEqual(n, budget, rel)

    def test_all_playbooks_exist(self):
        for pb in prompts.PLAYBOOKS:
            self.assertTrue(os.path.exists(os.path.join(prompts.PROMPTS_DIR, "playbooks", pb + ".md")), pb)

    def test_no_personal_values_in_files(self):
        pat = re.compile(r"@gmail\.com|010-\d{3,4}-\d{4}|\+82\s?10", re.I)
        for rel in prompts.file_report():
            self.assertIsNone(pat.search(prompts._read(rel)), rel)


class Build(unittest.TestCase):
    def ctx(self, agent, **kw):
        c = {"agent": agent, "profile_lines": PROFILE_FIXTURE, "today": "2026-11-05"}
        c.update(kw)
        return c

    def test_matrix(self):
        n = 0
        for kind in ("verify", "submit"):
            for region in prompts.REGIONS:
                for sk in ("google_form", "naver_form", "youtube", "instagram", "x", "site_form", "gleam", "naver_blog"):
                    for lane in ("browser", "form"):
                        n += 1
                        agent = AGENTS[n % len(AGENTS)]
                        item = make_item(n, region, sk, lane=lane)
                        with self.subTest(kind=kind, region=region, sk=sk, lane=lane):
                            text = prompts.build(kind, item, self.ctx(agent))
                            self.assertLessEqual(len(text), prompts.BUDGET["total"])
                            self.assertNotIn("{{", text)
                            for verb in prompts.TERMINAL[kind]:
                                self.assertEqual(text.count(f"ep {verb} {item['id']} --agent {agent}"), 1, verb)
                            for bit in SECRET_BITS:
                                self.assertNotIn(bit, text)
                            prof = text.split("# Profile", 1)[1].split("# Finish", 1)[0]
                            self.assertNotIn("password", prof.lower())
                            self.assertIn("Test Person", prof)
                            rep = prompts.budget_report(kind, item, self.ctx(agent))
                            self.assertTrue(rep["ok"], rep)

    def test_legacy_note_only_when_present(self):
        item = make_item(1, "KR", "google_form", legacy="이전 메모: 퀴즈 정답은 공고에 있음")
        text = prompts.build("verify", item, self.ctx("codex"))
        self.assertEqual(text.count("legacy notes (Korean):"), 1)
        self.assertIn("퀴즈 정답", text)
        item = make_item(2, "KR", "google_form", legacy="")
        self.assertNotIn("legacy notes", prompts.build("verify", item, self.ctx("codex")))
        self.assertNotIn("legacy_notes", prompts.build("submit", item, self.ctx("codex")))

    def test_last_failure_and_logged_in(self):
        item = make_item(3, "KR", "naver_form")
        text = prompts.build("submit", item, self.ctx("claude", last_failure="script timed out before submit",
                                                       sns_logged_in=["naver", "youtube"]))
        self.assertIn("last failure: script timed out", text)
        self.assertIn("logged in in the entry browser: naver, youtube", text)

    def test_scout(self):
        for region in prompts.REGIONS:
            for agent in AGENTS:
                ctx = {"agent": agent, "out": r"C:\tmp\scout-out.jsonl", "known": r"C:\tmp\known.txt",
                       "today": "2026-11-05"}
                text = prompts.build("scout", {"region": region}, ctx)
                self.assertLessEqual(len(text), prompts.BUDGET["total"])
                self.assertNotIn("{{", text)
                self.assertIn(r"C:\tmp\scout-out.jsonl", text)
                self.assertIn(r"C:\tmp\known.txt", text)
                self.assertIn("2026-12-20", text)  # today + 45 days
                self.assertNotIn("# Profile", text)
                self.assertNotIn("ep done", text)

    def test_region_sections(self):
        item = make_item(4, "JP", "x")
        self.assertNotIn("フォロー＆リポスト キャンペーン 応募", prompts.build("verify", item, self.ctx("codex")))
        self.assertIn("フォロー＆リポスト キャンペーン 応募", prompts.build("scout", {"region": "JP"},
                                                                {"agent": "gemini", "out": "o", "known": "k"}))

    def test_playbook_choice(self):
        pick = prompts.pick_playbooks
        self.assertEqual(pick("verify", make_item(5, "KR", "google_form"))[0], "google_form")
        it = make_item(6, "KR", "site_form")
        it.update(conditions="휴대폰 인증 필요", verify_notes="")
        self.assertEqual(pick("submit", it), ["site_form", "nice_auth"])
        self.assertEqual(pick("verify", make_item(7, "JP", "x")), ["x", "jp_campaign"])
        self.assertEqual(pick("submit", make_item(8, "KR", "instagram")), ["instagram"])
        it = make_item(9, "KR", "site_form")
        it.update(site_kind="", url="https://eventhouse.kr/fgo.php?x=1", conditions="", verify_notes="")
        self.assertEqual(pick("verify", it), ["aggregator"])
        it = make_item(10, "KR", "site_form")
        it.update(conditions="유튜브 구독 후 설문", verify_notes="")
        self.assertEqual(pick("verify", it), ["site_form", "youtube"])
        self.assertEqual(pick("scout", {"region": "KR"}), [])

    def test_email_verify_playbook_selection(self):
        pick = prompts.pick_playbooks
        it = make_item(20, "KR", "site_form")
        it.update(conditions="", verify_notes="needs an email verification code before submit", legacy_notes="")
        self.assertEqual(pick("submit", it), ["site_form", "email_verify"])
        self.assertEqual(pick("verify", it), ["site_form"])  # only kind submit
        for field, text in (("conditions", "이메일 인증 후 응모"), ("legacy_notes", "인증 메일 확인 필요"),
                            ("notes", "メール認証が必要"), ("verify_notes", "Check the confirmation email"),
                            ("conditions", "Please verify your email first"), ("verify_notes", "認証メールのリンク")):
            it = make_item(21, "KR", "google_form")
            it.update(conditions="", verify_notes="", legacy_notes="", notes="")
            it[field] = text
            self.assertEqual(pick("submit", it), ["google_form", "email_verify"], (field, text))
        it = make_item(22, "KR", "site_form")
        it.update(conditions="", verify_notes="", legacy_notes="", notes="", title="email verification event")
        self.assertEqual(pick("submit", it), ["site_form"], "the title alone does not trigger it")
        # an incidental SNS mention does not push the email playbook out
        it = make_item(24, "KR", "site_form")
        it.update(conditions="카카오 채널 추가", verify_notes="email verification code needed", legacy_notes="")
        self.assertEqual(pick("submit", it), ["site_form", "email_verify"])
        for p in prompts.pick_playbooks("submit", it):
            self.assertLessEqual(len(prompts._section(prompts._read(f"playbooks/{p}.md"), "submit")), prompts.BUDGET["playbook"])

    def test_email_verify_prompt_size_and_content(self):
        for region in prompts.REGIONS:
            for lane in ("browser", "form"):
                item = make_item(25, region, "google_form", lane=lane)
                item["verify_notes"] = "email verification code arrives after submit. " + LONG
                text = prompts.build("submit", item, self.ctx("codex"))
                self.assertLessEqual(len(text), prompts.BUDGET["total"])
                rep = prompts.budget_report("submit", item, self.ctx("codex"))
                self.assertTrue(rep["ok"], rep)
                self.assertIn("email_verify", rep["playbooks"] + rep["pointers"])
                if "email_verify" in rep["playbooks"]:
                    self.assertIn("https://mail.google.com/mail/u/0/#search/", text)
                    self.assertIn("NEW tab", text)
                    self.assertIn("member_signup", text)
                else:  # dropped for size: the pointer names the file
                    self.assertIn("email_verify.md", text)
        body = prompts._read("playbooks/email_verify.md")
        for must in ("never delete", "archive", "unrelated mail", "close the mail tab"):
            self.assertIn(must, body.lower() if must.islower() else body)
        self.assertLessEqual(len(body), prompts.BUDGET["playbook"])

    def test_backfill_kind_is_self_contained(self):
        rows = [{"id": f"E{n}", "region": "KR", "title": "제목", "org": "기관", "prize": "커피 쿠폰 " * 10,
                 "conditions": "c " * 200} for n in range(20)]
        text = prompts.build("backfill", {"items": rows}, {})
        self.assertLessEqual(len(text), prompts.BUDGET["total"])
        self.assertNotIn("{{", text)
        self.assertNotIn("# Role", text)  # no common.md
        self.assertNotIn("# Finish", text)
        self.assertIn("prize_min_value", text)
        self.assertEqual(text.split("# Items")[1].count('"id": "E'), 20)
        self.assertLessEqual(prompts.file_report()["backfill.md"][0], prompts.BUDGET["kind"])
        self.assertIn("backfill", prompts.SELF_CONTAINED)
        self.assertNotIn("backfill", prompts.KINDS)

    def test_local_site_kind_table(self):
        f = prompts._local_site_kind
        self.assertEqual(f("https://forms.gle/abc"), "google_form")
        self.assertEqual(f("m.blog.naver.com/x/1"), "naver_blog")
        self.assertEqual(f("https://www.youtube.com/watch?v=1"), "youtube")
        self.assertEqual(f("https://twitter.com/a"), "x")
        self.assertEqual(f("https://example.com", "naver"), "naver_form")
        self.assertEqual(f("https://example.com"), "site_form")
        self.assertEqual(prompts.detect_site_kind("https://example.com/p", "instagram"), "instagram")

    def test_dropped_playbook_becomes_pointer(self):
        item = make_item(14, "US", "naver_blog")
        item.update(conditions="휴대폰 인증 필요")
        ctx = self.ctx("codex", profile_lines="\n".join(f"field{k}: value value {k}" for k in range(16)))
        rep = prompts.budget_report("submit", item, ctx)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["playbooks"] + rep["pointers"], ["naver_blog", "nice_auth"])
        if rep["pointers"]:
            self.assertIn("nice_auth.md", prompts.build("submit", item, ctx))

    def test_long_answers_are_deferred(self):
        item = make_item(11, "US", "google_form")
        item["answers"] = {f"question {k}": "answer " * 20 for k in range(30)}
        text = prompts.build("submit", item, self.ctx("codex"))
        self.assertIn("read them with ep get E9011", text)
        self.assertLessEqual(len(text), prompts.BUDGET["total"])

    def test_unknown_kind_and_region(self):
        with self.assertRaises(ValueError):
            prompts.build("translate", make_item(12, "KR", "site_form"), self.ctx("codex"))
        with self.assertRaises(ValueError):
            prompts.build("verify", make_item(13, "CN", "site_form"), self.ctx("codex"))


class Policy(unittest.TestCase):
    def test_defaults_render(self):
        p = dict(policy.DEFAULTS)
        for region in ("JP", "US"):
            s = policy.policy_lines(region, "verify", p)
            self.assertIn("manual --need residency", s)
            self.assertIn("manual --need local_phone", s)
            self.assertIn("member_signup", s)
        self.assertIn("LINE friend add", policy.policy_lines("JP", "submit", p))
        self.assertNotIn("LINE", policy.policy_lines("US", "submit", p))
        self.assertIn("sign-up not yet allowed", policy.policy_lines("KR", "verify", p))
        self.assertNotIn("Sign-ups", policy.policy_lines("KR", "scout", p))

    def test_no_personal_answers_in_prompts(self):
        for kind in ("verify", "submit"):
            text = prompts.build(kind, make_item(30, "KR", "site_form"), {"agent": "codex", "profile_lines": PROFILE_FIXTURE,
                                                                          "today": "2026-11-05"})
            self.assertNotIn("nonprofit organization representative", text)
            self.assertNotIn("planning and operations", text)
            self.assertNotIn("university student", text)
            self.assertIn("occupation, job", text.lower() if kind == "submit" else text)
            self.assertIn("Answering policy", text)
            self.assertNotIn("Answering policy (owner)", text)
            self.assertIn("occupation: fixture occupation", text)  # the profile block carries the user's own value

    def test_kr_signup_lists_are_user_data(self):
        self.assertEqual((policy.KR_SIGNUP_ALLOW, policy.KR_SIGNUP_DENY), ((), ()))
        p = policy.load_policy(os.path.join(tempfile.gettempdir(), "no-such-policy.json"))
        self.assertEqual((p["kr_signup_allow"], p["kr_signup_deny"]), ([], []))
        s = policy.policy_lines("KR", "verify", p)  # empty allow list: every new KR sign-up needs the user
        self.assertIn("no new account is allowed", s)
        self.assertIn("sign-up not yet allowed", s)
        self.assertNotIn("Refused sites", s)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"kr_signup_allow": ["사이트가", " 사이트나 "], "kr_signup_deny": ["사이트다"]}, f, ensure_ascii=False)
            p = policy.load_policy(path)
            self.assertEqual((p["kr_signup_allow"], p["kr_signup_deny"]), (["사이트가", "사이트나"], ["사이트다"]))
            self.assertNotIn("errors", p)
            s = policy.policy_lines("KR", "verify", p)
            self.assertIn("only on allowed sites: 사이트가, 사이트나", s)
            self.assertIn("Refused sites, exclude: 사이트다", s)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"kr_signup_allow": "사이트가", "kr_signup_deny": [1, "x"]}, f, ensure_ascii=False)
            p = policy.load_policy(path)
            self.assertEqual((p["kr_signup_allow"], p["kr_signup_deny"]), ([], []))
            self.assertEqual(len(p["errors"]), 2)

    def test_overrides_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"foreign_scope": "open_only", "x_actions": "bogus", "max_agents": 2, "names": {"en": "X"},
                           "foreign_scope_note": "n", "_source": "card", "newkey": 1}, f)
            p = policy.load_policy(path)
            self.assertEqual(p["foreign_scope"], "open_only")
            self.assertEqual(p["x_actions"], policy.DEFAULTS["x_actions"])
            self.assertEqual(p["max_agents"], 2)
            self.assertTrue(any("x_actions" in e for e in p["errors"]))
            self.assertEqual(p["unknown_keys"], ["newkey"])
            self.assertNotIn("names", p)
            self.assertIn("exclude", policy.policy_lines("US", "verify", p))

    def test_bad_file_is_visible(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            p = policy.load_policy(path)
            self.assertEqual(p["foreign_scope"], policy.DEFAULTS["foreign_scope"])
            self.assertTrue(p["errors"])


class ContestPolicy(unittest.TestCase):
    def test_fields(self):
        p = dict(policy.DEFAULTS)
        self.assertTrue(policy.contest_field_ok("[공모전] [시청] 축제 네이밍 공모전. 기획/아이디어", p))
        self.assertTrue(policy.contest_field_ok("[공모전] 공공데이터 활용 AI 경진대회", p))
        self.assertFalse(policy.contest_field_ok("[공모전] 관광 사진 공모전", p))
        self.assertFalse(policy.contest_field_ok("[공모전] 대학생 서포터즈 모집. 아이디어 기획", p))
        self.assertFalse(policy.contest_field_ok("[공모전] 체육대회", p))
        p["contest_fields"] = ["photo", "other"]
        self.assertTrue(policy.contest_field_ok("[공모전] 관광 사진 공모전", p))
        self.assertTrue(policy.contest_field_ok("[공모전] 체육대회", p))
        self.assertFalse(policy.contest_field_ok("[공모전] 네이밍 공모전", p))

    def test_offline_rules_render(self):
        p = dict(policy.DEFAULTS)
        s = policy.policy_lines("KR", "verify", p)
        self.assertIn("exclude 'onsite writing'", s)
        self.assertIn("5,000,000 KRW", s)
        self.assertIn("post the entry", s)
        self.assertNotIn("Contests", policy.policy_lines("KR", "scout", p))
        p.update(contest_onsite_writing="allow", contest_onsite_final="exclude", contest_mail_only="exclude",
                 contest_ceremony="exclude")
        s = policy.policy_lines("KR", "submit", p)
        for t in ("exclude 'on-site final'", "exclude 'mail only'", "exclude 'ceremony required'"):
            self.assertIn(t, s)
        self.assertNotIn("exclude 'onsite writing'", s)

    def test_file_values_checked(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"contest_fields": ["music", "nope"], "contest_mail_only": "maybe",
                           "contest_ceremony": "exclude", "contest_prize_ask_krw": 3000000}, f)
            p = policy.load_policy(path)
            self.assertEqual(p["contest_fields"], ["write", "ai"])
            self.assertEqual(p["contest_mail_only"], "allow")
            self.assertEqual(p["contest_ceremony"], "exclude")
            self.assertEqual(p["contest_prize_ask_krw"], 3000000)
            self.assertEqual(len(p["errors"]), 2)


if __name__ == "__main__":
    unittest.main()
