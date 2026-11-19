"""Round 2 security regression tests for group A (items A1-A8).
unittest only; temp directories; no network, no real Chrome, no AI CLI."""
import atexit
import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_BOOT_TMP = tempfile.mkdtemp(prefix="ep-r2a-boot-")
atexit.register(shutil.rmtree, _BOOT_TMP, True)
os.environ.setdefault("EP_APP_DIR", _BOOT_TMP)
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import paths, secure, server, settings, telegram  # noqa: E402
from cp import secrets as cp_sec  # noqa: E402
from ep import prompts as ep_prompts, secrets as ep_sec  # noqa: E402


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _run_secret_cli(mod, argv: list[str], stdin_text: str = "") -> tuple[int, dict]:
    buf = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)), redirect_stdout(buf):
        rc = mod.main(argv)
    raw = buf.getvalue().strip().splitlines()
    return rc, (json.loads(raw[-1]) if raw else {})


class _FakeElementHandle:
    def __init__(self, locator, owner_frame_obj=None, same_doc=True):
        self._loc = locator
        self._owner_frame = locator.frame if owner_frame_obj is None else owner_frame_obj
        self._same_doc = same_doc

    def owner_frame(self):
        return self._owner_frame

    def fill(self, v):
        self._loc.fill(v)

    def evaluate(self, expr):
        return self._loc.evaluate(expr)


class _FakeLocator:
    def __init__(self, frame, count_n=1, tag="input", input_type="password",
                 stay_focused=True, lose_focus_on_type=False,
                 mutate_url_on_click="", mutate_url_on_type="",
                 mutate_type_on_type="", wrong_owner_frame=None, same_doc=True):
        self.frame = frame
        self._count = count_n
        self.tag = tag
        self.input_type = input_type
        self._stay_focused = stay_focused
        self._lose_focus_on_type = lose_focus_on_type
        self._mutate_url_on_click = mutate_url_on_click
        self._mutate_url_on_type = mutate_url_on_type
        self._mutate_type_on_type = mutate_type_on_type
        self._wrong_owner_frame = wrong_owner_frame
        self._same_doc = same_doc
        self.value = ""
        self.clicked = False
        self.typed_calls = []
        self._handle = _FakeElementHandle(self, wrong_owner_frame, same_doc=same_doc)

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def element_handle(self, timeout=2000):
        return self._handle if self._count > 0 else None

    def click(self, timeout=5000):
        self.clicked = True
        self.frame.focused = self._stay_focused
        if self._mutate_url_on_click:
            self.frame.url = self._mutate_url_on_click

    def fill(self, v):
        self.value = v

    def evaluate(self, expr):
        if "tagName" in expr:
            return {
                "tag": self.tag,
                "type": self.input_type,
                "focused": bool(self.frame.focused),
                "inDoc": bool(self._same_doc),
                "href": self.frame.url,
            }
        if "el.value = ''" in expr:
            self.value = ""
            return None
        return bool(self.frame.focused)

    def press_sequentially(self, v, delay=70):
        self.value = v
        self.typed_calls.append((v, delay))
        if self._lose_focus_on_type:
            self.frame.focused = False
        if self._mutate_url_on_type:
            self.frame.url = self._mutate_url_on_type
        if self._mutate_type_on_type:
            self.input_type = self._mutate_type_on_type


class _FakeFrame:
    def __init__(self, url, locator_obj=None):
        self.url = url
        self.focused = False
        self.seen_selectors = []
        self._loc = locator_obj or _FakeLocator(self, count_n=0)
        self._loc.frame = self
        if self._loc._wrong_owner_frame is None:
            self._loc._handle._owner_frame = self

    def locator(self, sel):
        self.seen_selectors.append(sel)
        return self._loc

    def evaluate(self, expr, arg=None):
        if isinstance(arg, _FakeElementHandle):
            if not arg._same_doc or arg.owner_frame() is not self:
                return False
            if "activeElement" in expr:
                return bool(self.focused)
            return True
        return True


class _FakePage:
    def __init__(self, url, frames):
        self.url = url
        self.frames = frames


class _FakeBrowser:
    def __init__(self, pages):
        self.contexts = [type("Ctx", (), {"pages": pages})()]


class TestA1SecretSetBindAndPrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r2a-a1-")
        self.saved_ep = (ep_sec.SECRETS, ep_sec.AUTO)
        self.saved_cp = (cp_sec.SECRETS, cp_sec.AUTO)
        ep_sec.SECRETS = os.path.join(self.tmp, "ep-secrets.env")
        ep_sec.AUTO = os.path.join(self.tmp, "ep-secrets-auto.env")
        cp_sec.SECRETS = os.path.join(self.tmp, "cp-secrets.env")
        cp_sec.AUTO = os.path.join(self.tmp, "cp-secrets-auto.env")

    def tearDown(self):
        ep_sec.SECRETS, ep_sec.AUTO = self.saved_ep
        cp_sec.SECRETS, cp_sec.AUTO = self.saved_cp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_requires_domain_for_custom_keys_in_ep_and_cp(self):
        for mod in (ep_sec, cp_sec):
            rc, out = _run_secret_cli(mod, ["set", "MYSITE_PW", "--generate", "16"])
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])
            self.assertEqual(mod.load(), {})

            rc, out = _run_secret_cli(mod, ["set", "MYSITE_ID"], stdin_text="alice01\n")
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])

            # With exact https login host --domain, both generated PW and stdin ID succeed and can be filled
            rc, out = _run_secret_cli(mod, ["set", "MYSITE_PW", "--domain", "member.example.co.kr", "--generate", "16"])
            self.assertEqual(rc, 0, out)
            self.assertTrue(out["ok"])
            val, dom = mod.unpack(mod.load()["MYSITE_PW"])
            self.assertEqual(len(val), 16)
            self.assertEqual(dom, "member.example.co.kr")

            rc, out = _run_secret_cli(mod, ["set", "MYSITE_ID", "--domain", "member.example.co.kr"], stdin_text="alice01\n")
            self.assertEqual(rc, 0, out)
            self.assertEqual(mod.unpack(mod.load()["MYSITE_ID"]), ("alice01", "member.example.co.kr"))

            # Known site prefix still works without --domain, but rejects mismatched --domain
            rc_ok, _ = _run_secret_cli(mod, ["set", "NAVER_PW", "--generate", "14"])
            self.assertEqual(rc_ok, 0)
            rc_bad, _ = _run_secret_cli(mod, ["set", "NAVER_PW", "--domain", "member.example.co.kr", "--generate", "14"])
            self.assertEqual(rc_bad, 1)

    def test_bind_binds_legacy_unbound_key_without_revealing_value_and_fill_refusal_mentions_manual_login(self):
        for mod in (ep_sec, cp_sec):
            secret_val = "SuperSecret!9876"
            mod.save_auto("OLDKEY_PW", secret_val)  # legacy key stored without a domain

            # Fill refusal must instruct the agent to ask the user (manual --need login) instead of guessing
            fr = _FakeFrame("https://member.example.co.kr/login", _FakeLocator(None, tag="input", input_type="password"))
            br = _FakeBrowser([_FakePage("https://member.example.co.kr/login", [fr])])
            res = mod.fill_on_browser(br, "OLDKEY_PW", secret_val, "", "#pw", "member.example.co.kr")
            self.assertFalse(res["ok"])
            self.assertIn("manual --need login", res["error"])
            self.assertNotIn("store an allowed domain", res["error"])

            rc_fill, out_fill = _run_secret_cli(
                mod, ["fill", "OLDKEY_PW", "--selector", "#pw", "--url-contains", "member.example.co.kr"]
            )
            self.assertEqual(rc_fill, 1)
            self.assertIn("manual --need login", out_fill["error"])
            self.assertNotIn(secret_val, json.dumps(out_fill))

            # Round 3 A1: CLI secret bind must refuse for keys that already hold a value
            rc_cli_bind, out_cli_bind = _run_secret_cli(mod, ["bind", "OLDKEY_PW", "--domain", "member.example.co.kr"])
            self.assertEqual(rc_cli_bind, 1)
            self.assertFalse(out_cli_bind["ok"])
            self.assertIn("manual --need login", out_cli_bind["error"])
            self.assertNotIn(secret_val, json.dumps(out_cli_bind))
            self.assertEqual(mod.unpack(mod.load()["OLDKEY_PW"]), (secret_val, ""))

            # In-process app binding succeeds and never exposes secret_val
            out_bind = mod.owner_bind_domain("OLDKEY_PW", "member.example.co.kr")
            self.assertTrue(out_bind["ok"], out_bind)
            self.assertNotIn(secret_val, json.dumps(out_bind))
            self.assertEqual(mod.unpack(mod.load()["OLDKEY_PW"]), (secret_val, "member.example.co.kr"))

            # Rebinding an already-bound key to a different host is refused (anti-hijack)
            out_rebind = mod.owner_bind_domain("OLDKEY_PW", "other.example.co.kr")
            self.assertFalse(out_rebind["ok"])
            self.assertEqual(mod.unpack(mod.load()["OLDKEY_PW"]), (secret_val, "member.example.co.kr"))

            # Now fill_on_browser succeeds on the bound host
            res_ok = mod.fill_on_browser(
                br, "OLDKEY_PW", secret_val, "member.example.co.kr", "#pw", "member.example.co.kr"
            )
            self.assertTrue(res_ok["ok"], res_ok)

    def test_prompts_and_playbooks_updated_and_within_budget(self):
        for rel_dir, cli_name in (("prompts", "ep"), ("prompts_contest", "cp")):
            submit_txt = _read_text(os.path.join(ROOT, rel_dir, "submit.md"))
            self.assertIn(f"{cli_name} secret set <SITEKEY>_PW --domain <host> --generate 16", submit_txt)
            self.assertIn(f"{cli_name} secret set <SITEKEY>_ID --domain <host>", submit_txt)
            site_pb = _read_text(os.path.join(ROOT, rel_dir, "playbooks", "site_form.md"))
            self.assertIn("COMMENT_PW", site_pb)
            self.assertIn("--url-contains", site_pb)
        for rel, (n, budget) in ep_prompts.file_report().items():
            self.assertLessEqual(n, budget, rel)


class TestA2BoundDomains(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r2a-a2-")
        self.saved = (ep_sec.SECRETS, ep_sec.AUTO)
        ep_sec.SECRETS = os.path.join(self.tmp, "secrets.env")
        ep_sec.AUTO = os.path.join(self.tmp, "secrets-auto.env")

    def tearDown(self):
        ep_sec.SECRETS, ep_sec.AUTO = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_public_suffixes_registrable_2level_and_single_labels(self):
        rejected = (
            "", "localhost", "kr", "com", "net", "org",
            "co.kr", "or.kr", "go.kr", "ac.kr", "ne.kr", "re.kr", "pe.kr",
            "com.au", "co.uk", "co.jp", "org.uk", "ne.jp",
            "http://member.example.co.kr", "*.example.com", "127.0.0.1", "a..b.com", "-bad.example.com",
        )
        for mod in (ep_sec, cp_sec):
            for bad in rejected:
                dom, err = mod.check_bound_domain(bad)
                self.assertEqual(dom, "", f"expected {bad!r} to be rejected")
                self.assertTrue(err, f"expected error for {bad!r}")
                if bad:
                    with self.assertRaises(ValueError, msg=bad):
                        mod.save_auto("CUSTOM_PW", "Abc123!@#def", domain=bad)

        accepted = (
            "shop.co.kr", "org.or.kr", "portal.go.kr", "univ.ac.kr",
            "isp.ne.kr", "inst.re.kr", "me.pe.kr", "shop.com.au", "shop.co.jp", "shop.co.uk",
            "member.example.co.kr", "www.shop.co.kr", "sso.univ.ac.kr", "login.portal.go.kr",
            "auth.shop.com.au", "id.service.co.jp", "login.danawa.com", "example.com",
            "https://member.example.co.kr/join?step=1",
        )
        for mod in (ep_sec, cp_sec):
            for good in accepted:
                dom, err = mod.check_bound_domain(good)
                self.assertEqual(err, "", f"expected {good!r} to pass: {err}")
                self.assertTrue(dom)

    def test_custom_bound_domain_matches_exact_host_only(self):
        for mod in (ep_sec, cp_sec):
            self.assertEqual(
                mod.host_ok("SITE_PW", "member.example.co.kr", "member.example.co.kr", "member.example.co.kr"), ""
            )
            # Subdomains of a custom bound domain must NOT be allowed
            self.assertTrue(
                mod.host_ok("SITE_PW", "sub.member.example.co.kr", "member.example.co.kr", "member.example.co.kr")
            )
            self.assertTrue(
                mod.host_ok("SITE_PW", "www.member.example.co.kr", "www.member.example.co.kr", "member.example.co.kr")
            )
            # Legacy invalid bound domain stored earlier is also rejected at host_ok time
            self.assertTrue(mod.host_ok("SITE_PW", "co.kr", "co.kr", "co.kr"))
            self.assertTrue(mod.host_ok("SITE_PW", "www.shop.co.kr", "www.shop.co.kr", "shop.co.kr"))


class TestA3UrlContainsAndInstagramRoot(unittest.TestCase):
    def test_full_url_compares_scheme_host_and_path_ignoring_query_and_fragment(self):
        for mod in (ep_sec, cp_sec):
            self.assertEqual(
                mod.url_ok(
                    "NAVER_PW",
                    "https://nid.naver.com/nidlogin.login?mode=form&url=https%3A%2F%2Fwww.naver.com#top",
                    "https://nid.naver.com/nidlogin.login",
                ),
                "",
            )
            self.assertEqual(
                mod.url_ok(
                    "SITE_PW",
                    "https://member.example.co.kr/auth/login?ret=/event&x=1#form",
                    "https://member.example.co.kr/auth/login?different=2#other",
                    "member.example.co.kr",
                ),
                "",
            )
            # Different path or non-https still rejected
            self.assertTrue(
                mod.url_ok(
                    "NAVER_PW",
                    "https://nid.naver.com/user2/help/myInfo?m=view",
                    "https://nid.naver.com/nidlogin.login",
                )
            )
            self.assertTrue(
                mod.url_ok(
                    "NAVER_PW",
                    "http://nid.naver.com/nidlogin.login?mode=form",
                    "https://nid.naver.com/nidlogin.login",
                )
            )
            # fill_on_browser works when tab has extra query parameters
            fr = _FakeFrame(
                "https://nid.naver.com/nidlogin.login?mode=form&svctype=1",
                _FakeLocator(None, tag="input", input_type="password"),
            )
            br = _FakeBrowser([_FakePage(fr.url, [fr])])
            res = mod.fill_on_browser(br, "NAVER_PW", "pw123", "", "#pw", "https://nid.naver.com/nidlogin.login")
            self.assertTrue(res["ok"], res)

    def test_instagram_root_login_allowed_and_content_paths_rejected(self):
        for mod in (ep_sec, cp_sec):
            for key in ("INSTAGRAM_PW", "INSTAGRAM_ID", "INSTA_PW"):
                self.assertEqual(mod.url_ok(key, "https://www.instagram.com/", "instagram.com"), "")
                self.assertEqual(mod.url_ok(key, "https://www.instagram.com/?flo=true", "https://www.instagram.com/"), "")
                self.assertEqual(mod.url_ok(key, "https://www.instagram.com", "https://www.instagram.com/"), "")
                self.assertEqual(mod.url_ok(key, "https://instagram.com/", "instagram.com"), "")
                self.assertEqual(mod.url_ok(key, "https://www.instagram.com/accounts/login/?next=%2F", "instagram.com"), "")
                # Non-login content paths must remain blocked
                self.assertTrue(mod.url_ok(key, "https://www.instagram.com/p/C12345/", "instagram.com"))
                self.assertTrue(mod.url_ok(key, "https://www.instagram.com/reel/C12345/", "instagram.com"))
                self.assertTrue(mod.url_ok(key, "https://www.instagram.com/some_account/", "instagram.com"))


class TestA4TypingElementFrameFocusAndInputTypes(unittest.TestCase):
    def test_rejects_frame_piercing_selectors_and_foreign_frame_elements(self):
        for mod in (ep_sec, cp_sec):
            loc = _FakeLocator(None, tag="input", input_type="password")
            fr = _FakeFrame("https://nid.naver.com/nidlogin.login", loc)
            br = _FakeBrowser([_FakePage(fr.url, [fr])])
            for bad_sel in ("iframe >> #pw", "#wrap >> input[type=password]",
                            "internal:control=enter-frame >> #pw", "INTERNAL:attr=[name=pw]"):
                res = mod.fill_on_browser(br, "NAVER_PW", "secret", "", bad_sel, "nid.naver.com")
                self.assertFalse(res["ok"], bad_sel)
                self.assertFalse(loc.clicked)

            # Element handle whose owner_frame is an evil child frame must be rejected without clicking or typing
            evil_fr = _FakeFrame("https://evil.example/phish")
            pierced_loc = _FakeLocator(None, tag="input", input_type="password", wrong_owner_frame=evil_fr)
            top_fr = _FakeFrame("https://nid.naver.com/nidlogin.login", pierced_loc)
            br2 = _FakeBrowser([_FakePage(top_fr.url, [top_fr, evil_fr])])
            res2 = mod.fill_on_browser(br2, "NAVER_PW", "secret", "", "#pw", "nid.naver.com")
            self.assertFalse(res2["ok"])
            self.assertFalse(pierced_loc.clicked)
            self.assertEqual(pierced_loc.typed_calls, [])

    def test_input_element_type_restrictions(self):
        for mod in (ep_sec, cp_sec):
            # Password keys: ONLY <input type="password">
            for bad_tag, bad_type in (("input", "text"), ("input", "email"), ("input", "tel"),
                                      ("input", "search"), ("input", "hidden"), ("textarea", "password"), ("div", "")):
                loc = _FakeLocator(None, tag=bad_tag, input_type=bad_type)
                fr = _FakeFrame("https://nid.naver.com/nidlogin.login", loc)
                br = _FakeBrowser([_FakePage(fr.url, [fr])])
                r = mod.fill_on_browser(br, "NAVER_PW", "secret", "", "#pw", "nid.naver.com")
                self.assertFalse(r["ok"], (bad_tag, bad_type))
                self.assertFalse(loc.clicked)
                self.assertEqual(loc.typed_calls, [])

            # _ID / _EMAIL / _USER keys: <input> of type text, email, tel, or password allowed; others rejected
            for key in ("NAVER_ID", "GOOGLE_EMAIL", "NAVER_USER"):
                host = "accounts.google.com" if key.startswith("GOOGLE_") else "nid.naver.com"
                for ok_type in ("text", "email", "tel", "password"):
                    loc = _FakeLocator(None, tag="input", input_type=ok_type)
                    fr = _FakeFrame(f"https://{host}/login", loc)
                    br = _FakeBrowser([_FakePage(fr.url, [fr])])
                    r = mod.fill_on_browser(br, key, "my_login_id", "", "#id", host)
                    self.assertTrue(r["ok"], (key, ok_type, r))
                for bad_tag, bad_type in (("textarea", "text"), ("input", "search"), ("input", "hidden")):
                    loc = _FakeLocator(None, tag=bad_tag, input_type=bad_type)
                    fr = _FakeFrame(f"https://{host}/login", loc)
                    br = _FakeBrowser([_FakePage(fr.url, [fr])])
                    r = mod.fill_on_browser(br, key, "my_login_id", "", "#id", host)
                    self.assertFalse(r["ok"], (key, bad_tag, bad_type))

    def test_post_typing_focus_or_url_or_type_change_clears_field_and_fails(self):
        for mod in (ep_sec, cp_sec):
            # 1) Round 3 A3: focus moving after the last key succeeds if value length matches on allowed host
            loc1 = _FakeLocator(None, tag="input", input_type="password", lose_focus_on_type=True)
            fr1 = _FakeFrame("https://nid.naver.com/nidlogin.login", loc1)
            br1 = _FakeBrowser([_FakePage(fr1.url, [fr1])])
            r1 = mod.fill_on_browser(br1, "NAVER_PW", "MySecretPw!", "", "#pw", "nid.naver.com")
            self.assertTrue(r1["ok"], r1)
            self.assertEqual(loc1.value, "MySecretPw!")

            # 2) Frame URL host changed to disallowed host during typing -> field cleared and failure returned
            loc2 = _FakeLocator(
                None, tag="input", input_type="password",
                mutate_url_on_type="https://blog.naver.com/other_page",
            )
            fr2 = _FakeFrame("https://nid.naver.com/nidlogin.login", loc2)
            br2 = _FakeBrowser([_FakePage("https://nid.naver.com/nidlogin.login", [fr2])])
            r2 = mod.fill_on_browser(br2, "NAVER_PW", "MySecretPw!", "", "#pw", "nid.naver.com")
            self.assertFalse(r2["ok"])
            self.assertEqual(loc2.value, "")

            # 3) Input type mutated from password to non-password text during typing -> field cleared and failure returned
            loc3 = _FakeLocator(None, tag="input", input_type="password", mutate_type_on_type="text")
            fr3 = _FakeFrame("https://nid.naver.com/nidlogin.login", loc3)
            br3 = _FakeBrowser([_FakePage(fr3.url, [fr3])])
            r3 = mod.fill_on_browser(br3, "NAVER_PW", "MySecretPw!", "", "#pw", "nid.naver.com")
            self.assertFalse(r3["ok"])
            self.assertEqual(loc3.value, "")


class TestA5MigrationDuplicateKeysAndInvalidDpapi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r2a-a5-")
        self.saved_ep = (ep_sec.SECRETS, ep_sec.AUTO)
        self.saved_cp = (cp_sec.SECRETS, cp_sec.AUTO)
        ep_sec.SECRETS = os.path.join(self.tmp, "ep-secrets.env")
        ep_sec.AUTO = os.path.join(self.tmp, "ep-secrets-auto.env")
        cp_sec.SECRETS = os.path.join(self.tmp, "cp-secrets.env")
        cp_sec.AUTO = os.path.join(self.tmp, "cp-secrets-auto.env")

    def tearDown(self):
        ep_sec.SECRETS, ep_sec.AUTO = self.saved_ep
        cp_sec.SECRETS, cp_sec.AUTO = self.saved_cp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_duplicate_key_with_trailing_empty_line_is_migrated_and_blanked(self):
        for mod in (ep_sec, cp_sec):
            _write_text(
                mod.SECRETS,
                "# user secrets\nNAVER_PW=plain_dup_secret_1\nNAVER_PW=\nKAKAO_PW=first\nKAKAO_PW=second\nKAKAO_PW=\n",
            )
            moved = mod.migrate()
            self.assertEqual(moved, ["KAKAO_PW", "NAVER_PW"])
            sec_on_disk = _read_text(mod.SECRETS)
            self.assertNotIn("plain_dup_secret_1", sec_on_disk)
            self.assertNotIn("first", sec_on_disk)
            self.assertNotIn("second", sec_on_disk)
            d = mod.load()
            self.assertEqual(mod.unprotect(d["NAVER_PW"]), "plain_dup_secret_1")
            self.assertEqual(mod.unprotect(d["KAKAO_PW"]), "second")

    def test_undecryptable_dpapi_prefix_is_reported_and_not_silently_kept(self):
        for mod in (ep_sec, cp_sec):
            mod._UNDECRYPTABLE_WARNED.clear()
            before_w = len(mod.UNDECRYPTABLE_WARNINGS)
            # Round 3 A4: undecryptable dpapi: is kept as-is, valid plaintext is migrated, and undecryptable keys are reported once
            _write_text(mod.AUTO, "BROKEN_AUTO=dpapi:AAAA\n")
            _write_text(mod.SECRETS, "NAVER_PW=valid_plain_value\nFAKE_ENC=dpapi:not_a_valid_dpapi_blob\n")
            moved = mod.migrate()
            self.assertEqual(moved, ["NAVER_PW"])
            self.assertEqual(len(mod.UNDECRYPTABLE_WARNINGS) - before_w, 1)
            warn_msg = mod.UNDECRYPTABLE_WARNINGS[-1]
            self.assertIn("BROKEN_AUTO", warn_msg)
            self.assertIn("FAKE_ENC", warn_msg)
            self.assertNotIn("not_a_valid_dpapi_blob", warn_msg)
            self.assertNotIn("valid_plain_value", _read_text(mod.SECRETS))
            self.assertEqual(mod.read_file(mod.AUTO)["BROKEN_AUTO"], "dpapi:AAAA")
            self.assertEqual(mod.unprotect(mod.read_file(mod.AUTO)["NAVER_PW"]), "valid_plain_value")

            # Second migrate() does not report the same undecryptable keys again
            mod.migrate()
            self.assertEqual(len(mod.UNDECRYPTABLE_WARNINGS) - before_w, 1)

            # CLI list reports undecryptable state without failing valid keys
            rc, out = _run_secret_cli(mod, ["list"])
            self.assertEqual(rc, 0, out)
            self.assertTrue(out["ok"])
            self.assertEqual(out["keys"]["NAVER_PW"], "set")
            self.assertEqual(out["keys"]["BROKEN_AUTO"], "undecryptable")
            self.assertEqual(out["keys"]["FAKE_ENC"], "undecryptable")


class TestA6AclSidExplicitAceRemovalAndFatFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r2a-a6-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_grants_by_sid_using_system32_icacls_and_removes_other_explicit_aces(self):
        icacls = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "icacls.exe")
        for idx, mod in enumerate((secure, ep_sec, cp_sec)):
            sid = mod.current_sid()
            self.assertTrue(sid.upper().startswith("S-1-5-"), sid)

            # Fallback via System32\whoami.exe when OpenProcessToken returns False
            with mock.patch.object(ctypes.windll.advapi32, "OpenProcessToken", return_value=0):
                self.assertEqual(mod.current_sid(), sid)

            p = os.path.join(self.tmp, f"acl_target_{idx}.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("secret")
            # Add an explicit Everyone:(R) ACE (*S-1-1-0:R) before calling restrict_acl
            r_add = subprocess.run([icacls, p, "/grant", "*S-1-1-0:R"],
                                   capture_output=True, text=True, encoding="mbcs", errors="replace", check=True)
            self.assertEqual(r_add.returncode, 0)

            # Round 3 A5: sets protected DACL directly via SetNamedSecurityInfoW without spawning icacls /reset
            orig_run = subprocess.run
            calls = []

            def spy_run(cmd, *a, **kw):
                calls.append((list(cmd), dict(kw)))
                return orig_run(cmd, *a, **kw)

            with mock.patch("subprocess.run", side_effect=spy_run):
                self.assertTrue(mod.restrict_acl(p))

            self.assertFalse(any("/reset" in cmd for cmd, _ in calls))

            # Verify via System32\icacls.exe that Everyone is gone and only 1 ACE remains
            ver = subprocess.run([icacls, p], capture_output=True, text=True, encoding="mbcs", errors="replace", check=True)
            aces = mod._parse_icacls_aces(ver.stdout, p)
            self.assertEqual(len(aces), 1, ver.stdout)
            self.assertNotIn("everyone", ver.stdout.lower())
            self.assertIn("(F)", aces[0].upper())

    def test_fat_exfat_volume_without_persistent_acls_writes_file_and_warns_once(self):
        fat_dir = os.path.join(self.tmp, "fat_drive")
        os.makedirs(fat_dir, exist_ok=True)
        for mod in (secure, ep_sec, cp_sec):
            mod._ACL_WARNED.clear()
            before = len(mod.ACL_WARNINGS)
            f1 = os.path.join(fat_dir, f"{mod.__name__}_1.txt")
            f2 = os.path.join(fat_dir, f"{mod.__name__}_2.txt")
            _write_text(f1, "a")
            _write_text(f2, "b")
            with mock.patch.object(mod, "volume_supports_acls", return_value=False), \
                    mock.patch("subprocess.run") as mrun:
                self.assertFalse(mod.restrict_acl(f1))
                self.assertFalse(mod.restrict_acl(f2))
                mrun.assert_not_called()
            self.assertEqual(len(mod.ACL_WARNINGS) - before, 1)


class TestA7OsLockAndSettingsMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r2a-a7-")
        self.prev_app_dir = os.environ.get("EP_APP_DIR")
        os.environ["EP_APP_DIR"] = self.tmp
        paths.ensure_dirs()
        settings.save(dict(settings.DEFAULTS))

    def tearDown(self):
        if self.prev_app_dir is None:
            os.environ.pop("EP_APP_DIR", None)
        else:
            os.environ["EP_APP_DIR"] = self.prev_app_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_lock_file_and_killed_holder_process_do_not_deadlock(self):
        lp = paths.settings_path() + ".lock"
        # 1) Empty lock file left on disk from an earlier crash
        with open(lp, "wb"):
            pass
        t0 = time.monotonic()
        settings.update(interests="fast")
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(settings.load()["interests"], "fast")

        # 2) Another process acquires the OS lock and gets killed abruptly -> lock released by OS immediately
        child_code = (
            f"import sys; sys.path.insert(0, {ROOT!r}); "
            f"import os, time; os.environ['EP_APP_DIR'] = {self.tmp!r}; "
            "from app import settings; "
            "ctx = settings._file_lock(5.0); ctx.__enter__(); "
            "print('HELD', flush=True); time.sleep(60)"
        )
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "-c", child_code],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "HELD")
            # Contention while alive fails quickly with short timeout
            with self.assertRaises(OSError):
                with settings._file_lock(wait_sec=0.1):
                    pass
            # Kill child abruptly; OS releases lock immediately
            proc.kill()
            proc.wait(timeout=5)
            t1 = time.monotonic()
            with settings._file_lock(wait_sec=2.0):
                pass
            self.assertLess(time.monotonic() - t1, 2.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_api_settings_merges_only_changed_keys_inside_lock(self):
        tok = "123456789:AA" + "m" * 33
        settings.set_secret("telegram_token", tok)
        settings.set_secret("gemini_api_key", "AIzaKeepMe")
        settings.update(
            telegram_bot="keep_bot",
            telegram_chat_id="987654",
            telegram_start="deadbeef1234",
            telegram_start_at=time.time(),
            notify="telegram",
            accounts={"claude": 3, "codex": 2},
            setup_done=True,
        )

        # Simulate another flow updating telegram_chat_id right before /api/settings writes
        orig_merge = settings.merge

        def interleaved_merge(changes):
            settings.update(telegram_chat_id="555777")
            return orig_merge(changes)

        with mock.patch.object(settings, "merge", side_effect=interleaved_merge):
            r = server.handle("POST", "/api/settings", {}, {
                "interests": "AI 공모전",
                "accounts": {"claude": 4},
            })
        self.assertTrue(r["ok"])
        after = settings.load()
        self.assertEqual(after["interests"], "AI 공모전")
        self.assertEqual(after["accounts"], {"claude": 4, "codex": 2})  # codex count preserved!
        self.assertEqual(after["telegram_chat_id"], "555777")           # concurrent update preserved!
        self.assertEqual(after["telegram_bot"], "keep_bot")
        self.assertEqual(after["notify"], "telegram")
        self.assertTrue(after["setup_done"])
        self.assertEqual(settings.get_secret("telegram_token"), tok)
        self.assertEqual(settings.get_secret("gemini_api_key"), "AIzaKeepMe")
        self.assertEqual(settings.get_secret("telegram_start"), "deadbeef1234")


class TestA8TelegramStartEncrypted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r2a-a8-")
        self.prev_app_dir = os.environ.get("EP_APP_DIR")
        os.environ["EP_APP_DIR"] = self.tmp
        paths.ensure_dirs()
        telegram._START_CODES.clear()
        telegram._USED_CODES.clear()

    def tearDown(self):
        telegram._START_CODES.clear()
        telegram._USED_CODES.clear()
        if self.prev_app_dir is None:
            os.environ.pop("EP_APP_DIR", None)
        else:
            os.environ["EP_APP_DIR"] = self.prev_app_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_telegram_start_encrypted_on_disk_masked_in_public_and_works_with_telegram_flow(self):
        tok = "123456789:AA" + "t" * 33
        server.save_bot(tok, {"username": "r2_test_bot", "name": "R2 Bot"})

        raw_json_text = _read_text(paths.settings_path())
        raw_disk = json.loads(raw_json_text)
        code = settings.get_secret("telegram_start")
        self.assertTrue(code)
        # On disk: DPAPI-encrypted, plaintext start code never appears in settings.json
        self.assertTrue(raw_disk["telegram_start"].startswith("dpapi:"))
        self.assertNotIn(code, raw_json_text)

        # In public() / /api/state: boolean flag only, never the plaintext code or dpapi string
        pub = settings.public()
        self.assertIs(pub["telegram_start"], True)
        st = server.state()
        self.assertNotIn(code, json.dumps(st))
        self.assertNotIn("dpapi:", json.dumps(st))

        # Telegram linking via /api/telegram/find works seamlessly without changing app/telegram.py
        ups = [{"message": {"chat": {"id": 4242, "type": "private"}, "text": f"/start {code}", "date": time.time()}}]
        with mock.patch.object(telegram, "call", return_value=ups), mock.patch.object(telegram, "send"):
            r = server.handle("POST", "/api/telegram/find", {}, {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(settings.load()["telegram_chat_id"], "4242")
        self.assertEqual(settings.get_secret("telegram_start"), "")
        self.assertIs(settings.public()["telegram_start"], False)

        # Legacy plaintext telegram_start on disk is migrated to dpapi: on load()
        with open(paths.settings_path(), "w", encoding="utf-8") as f:
            json.dump({"telegram_start": "legacyplain99"}, f)
        loaded = settings.load()
        self.assertEqual(loaded["telegram_start"], "legacyplain99")
        migrated_raw = _read_text(paths.settings_path())
        migrated_disk = json.loads(migrated_raw)
        self.assertTrue(migrated_disk["telegram_start"].startswith("dpapi:"))
        self.assertNotIn("legacyplain99", migrated_raw)


if __name__ == "__main__":
    unittest.main()
