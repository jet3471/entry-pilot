"""Round 3 security & usability regression tests for group A (items A1-A5).
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
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from ctypes import wintypes
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_BOOT_TMP = tempfile.mkdtemp(prefix="ep-r3a-boot-")
atexit.register(shutil.rmtree, _BOOT_TMP, True)
os.environ.setdefault("EP_APP_DIR", _BOOT_TMP)
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import paths, secret_binding, secure, server, settings  # noqa: E402
from cp import secrets as cp_sec  # noqa: E402
from ep import prompts as ep_prompts, secrets as ep_sec  # noqa: E402


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _run_cli(mod, argv: list[str], stdin_text: str = "") -> tuple[int, dict]:
    buf = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)), redirect_stdout(buf):
        rc = mod.main(argv)
    lines = buf.getvalue().strip().splitlines()
    return rc, (json.loads(lines[-1]) if lines else {})


class _DomInput:
    """Simulated DOM element + ElementHandle + single-element Locator supporting visibility, enabled, attrs, and re-render."""

    def __init__(self, frame, *, tag="input", input_type="password", el_id="", name="", autocomplete="",
                 visible=True, enabled=True, in_doc=True, owner_frame_obj=None,
                 lose_focus_after_type=False, detach_after_type=False, replace_with_on_type=None,
                 partial_len_on_type=None, mutate_frame_url_on_type=""):
        self.frame = frame
        self.tag = tag
        self.input_type = input_type
        self.el_id = el_id
        self.name = name
        self.autocomplete = autocomplete
        self._visible = visible
        self._enabled = enabled
        self._in_doc = in_doc
        self._owner_frame = owner_frame_obj
        self._lose_focus_after_type = lose_focus_after_type
        self._detach_after_type = detach_after_type
        self._replace_with_on_type = replace_with_on_type
        self._partial_len_on_type = partial_len_on_type
        self._mutate_frame_url_on_type = mutate_frame_url_on_type
        self.value = ""
        self.clicked = False
        self.typed = []

    def owner_frame(self):
        return self.frame if self._owner_frame is None else self._owner_frame

    def is_visible(self):
        return bool(self._visible and self.input_type != "hidden")

    def is_enabled(self):
        return bool(self._enabled)

    def element_handle(self, timeout=2000):
        return self if self._in_doc else None

    def click(self, timeout=5000):
        if not self.is_visible() or not self.is_enabled():
            raise RuntimeError("Element is not visible or enabled")
        self.clicked = True
        self.frame.active_el = self

    def fill(self, v):
        self.value = v

    def press_sequentially(self, v, delay=70):
        self.typed.append((v, delay))
        if self._partial_len_on_type is not None:
            self.value = v[:self._partial_len_on_type]
            self.frame.active_el = None
            return
        self.value = v
        if self._lose_focus_after_type:
            self.frame.active_el = None
        if self._mutate_frame_url_on_type:
            self.frame.url = self._mutate_frame_url_on_type
        if self._detach_after_type:
            self._in_doc = False
            self.frame.active_el = None
            if self._replace_with_on_type is not None:
                self._replace_with_on_type.value = v
                self.frame.replace_element(self, self._replace_with_on_type)

    def evaluate(self, expr):
        if "tagName" in expr:
            return {
                "tag": self.tag,
                "type": self.input_type,
                "autocomplete": self.autocomplete,
                "id": self.el_id,
                "name": self.name,
                "visible": self.is_visible(),
                "enabled": self.is_enabled(),
                "valueLen": len(self.value),
                "focused": self.frame.active_el is self,
                "inDoc": bool(self._in_doc),
                "href": self.frame.url,
            }
        if "el.value = ''" in expr:
            self.value = ""
            return None
        return self.frame.active_el is self


class _MultiLocator:
    def __init__(self, frame, elements: list[_DomInput]):
        self.frame = frame
        self._elements = elements

    def count(self):
        return len(self._elements)

    @property
    def first(self):
        return self._elements[0]

    def nth(self, idx: int):
        return self._elements[idx]


class _SimFrame:
    def __init__(self, url: str, selectors: dict[str, list[_DomInput]] | None = None):
        self.url = url
        self.active_el = None
        self.selectors: dict[str, list[_DomInput]] = selectors or {}
        for els in self.selectors.values():
            for el in els:
                el.frame = self

    def replace_element(self, old_el: _DomInput, new_el: _DomInput):
        new_el.frame = self
        for sel, els in self.selectors.items():
            self.selectors[sel] = [new_el if x is old_el else x for x in els]

    def locator(self, sel: str):
        return _MultiLocator(self, list(self.selectors.get(sel, [])))

    def evaluate(self, expr: str, arg=None):
        if isinstance(arg, _DomInput):
            if not arg._in_doc or arg.owner_frame() is not self:
                return False
            if "activeElement" in expr:
                return self.active_el is arg
            return True
        return True


class _SimPage:
    def __init__(self, url: str, frames: list[_SimFrame]):
        self.url = url
        self.frames = frames


class _SimBrowser:
    def __init__(self, pages: list[_SimPage]):
        self.contexts = [type("Ctx", (), {"pages": pages})()]


class TestR3A1SecretBindingAndSetProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r3a-a1-")
        self.prev_app_dir = os.environ.get("EP_APP_DIR")
        self.prev_ep_data = os.environ.get("EP_DATA_DIR")
        self.prev_cp_data = os.environ.get("CP_DATA_DIR")
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ.pop("EP_DATA_DIR", None)
        os.environ.pop("CP_DATA_DIR", None)
        paths.ensure_dirs()
        settings.save(dict(settings.DEFAULTS, modes={"events": True, "contests": True}))
        self.saved_ep = (ep_sec.SECRETS, ep_sec.AUTO)
        self.saved_cp = (cp_sec.SECRETS, cp_sec.AUTO)
        ep_sec.SECRETS, ep_sec.AUTO = ep_sec._INIT_SECRETS, ep_sec._INIT_AUTO
        cp_sec.SECRETS, cp_sec.AUTO = cp_sec._INIT_SECRETS, cp_sec._INIT_AUTO

    def tearDown(self):
        ep_sec.SECRETS, ep_sec.AUTO = self.saved_ep
        cp_sec.SECRETS, cp_sec.AUTO = self.saved_cp
        for k, v in (("EP_APP_DIR", self.prev_app_dir), ("EP_DATA_DIR", self.prev_ep_data), ("CP_DATA_DIR", self.prev_cp_data)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_bind_refuses_existing_secret_while_app_api_binds_and_unbound_returns_names_only(self):
        ep_val = "UserEventPass!2026"
        cp_val = "UserContestPass!2026"
        _write_text(os.path.join(paths.data_dir(), "secrets.env"), f"PORTAL_PW={ep_val}\nNAVER_PW=NaverPlain1!\n")
        _write_text(os.path.join(paths.data_dir(), "contest", "secrets.env"), f"CONTEST_PORTAL_PW={cp_val}\n")

        # 1) /api/secrets/unbound returns key names ONLY (never secret values or dpapi blobs)
        unb = server.handle("GET", "/api/secrets/unbound", {}, {})
        self.assertTrue(unb["ok"])
        self.assertEqual(unb["keys"], ["CONTEST_PORTAL_PW", "PORTAL_PW"])
        self.assertEqual(unb["events"], ["PORTAL_PW"])
        self.assertEqual(unb["contests"], ["CONTEST_PORTAL_PW"])
        dumped = json.dumps(unb)
        self.assertNotIn(ep_val, dumped)
        self.assertNotIn(cp_val, dumped)
        self.assertNotIn("NaverPlain1!", dumped)
        self.assertNotIn("dpapi:", dumped)

        # 2) CLI `secret bind` MUST refuse for existing values (agent cannot bind existing key to attacker.host)
        for mod, k, secret_v in ((ep_sec, "PORTAL_PW", ep_val), (cp_sec, "CONTEST_PORTAL_PW", cp_val)):
            rc, out = _run_cli(mod, ["bind", k, "--domain", "attacker.co.kr"])
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])
            self.assertIn("manual --need login", out["error"])
            self.assertNotIn(secret_v, json.dumps(out))
            # Direct bind_domain() also refuses
            direct = mod.bind_domain(k, "attacker.co.kr")
            self.assertFalse(direct["ok"])
            self.assertEqual(mod.unpack(mod.load()[k]), (secret_v, ""))

        # 3) CLI `secret set` MUST refuse to overwrite or re-bind an existing key (case-insensitive too)
        for mod, k, secret_v in ((ep_sec, "PORTAL_PW", ep_val), (cp_sec, "CONTEST_PORTAL_PW", cp_val)):
            rc1, out1 = _run_cli(mod, ["set", k, "--domain", "attacker.co.kr", "--generate", "16"])
            self.assertEqual(rc1, 1)
            self.assertFalse(out1["ok"])
            rc2, out2 = _run_cli(mod, ["set", k.lower(), "--domain", "attacker.co.kr"], stdin_text="hijack\n")
            self.assertEqual(rc2, 1)
            self.assertFalse(out2["ok"])
            self.assertEqual(mod.unpack(mod.load()[k]), (secret_v, ""))

        # 4) CLI `secret set` for a brand-NEW key during sign-up stays allowed
        for mod in (ep_sec, cp_sec):
            rc_new, out_new = _run_cli(mod, ["set", "NEWSIGNUP_PW", "--domain", "signup.example.co.kr", "--generate", "16"])
            self.assertEqual(rc_new, 0, out_new)
            self.assertTrue(out_new["ok"])
            gen_pw, gen_dom = mod.unpack(mod.load()["NEWSIGNUP_PW"])
            self.assertEqual(len(gen_pw), 16)
            self.assertEqual(gen_dom, "signup.example.co.kr")
            # Second `secret set` on NEWSIGNUP_PW is now refused
            rc_dup, _ = _run_cli(mod, ["set", "NEWSIGNUP_PW", "--domain", "evil.co.kr", "--generate", "16"])
            self.assertEqual(rc_dup, 1)
            self.assertEqual(mod.unpack(mod.load()["NEWSIGNUP_PW"]), (gen_pw, "signup.example.co.kr"))

        # 5) In-process app /api/secrets/bind binds the existing unbound keys cleanly
        r_bind_ep = server.handle("POST", "/api/secrets/bind", {}, {
            "key": "PORTAL_PW", "domain": "example.co.kr", "engine": "events",
        })
        self.assertTrue(r_bind_ep["ok"], r_bind_ep)
        self.assertNotIn(ep_val, json.dumps(r_bind_ep))
        self.assertEqual(ep_sec.unpack(ep_sec.load()["PORTAL_PW"]), (ep_val, "example.co.kr"))

        r_bind_cp = server.handle("POST", "/api/secrets/bind", {}, {
            "key": "CONTEST_PORTAL_PW", "domain": "contest.go.kr", "engine": "contests",
        })
        self.assertTrue(r_bind_cp["ok"], r_bind_cp)
        self.assertNotIn(cp_val, json.dumps(r_bind_cp))
        self.assertEqual(cp_sec.unpack(cp_sec.load()["CONTEST_PORTAL_PW"]), (cp_val, "contest.go.kr"))

        # Rebinding an already-bound key via /api/secrets/bind to a different domain is refused
        r_rebind = server.handle("POST", "/api/secrets/bind", {}, {
            "key": "PORTAL_PW", "domain": "other.co.kr", "engine": "events",
        })
        self.assertFalse(r_rebind["ok"])
        self.assertEqual(ep_sec.unpack(ep_sec.load()["PORTAL_PW"]), (ep_val, "example.co.kr"))

        # Now /api/secrets/unbound is empty
        unb_after = server.handle("GET", "/api/secrets/unbound", {}, {})
        self.assertEqual(unb_after["keys"], [])

    def test_http_endpoints_require_session_token_and_ui_has_minimal_bind_section(self):
        _write_text(os.path.join(paths.data_dir(), "secrets.env"), "HTTPKEY_PW=HttpVal!99\n")
        srv = server.serve(0)
        port = srv.server_address[1]
        try:
            # Unauthenticated GET / POST -> 403
            for method, route, payload in (
                ("GET", "/api/secrets/unbound", None),
                ("POST", "/api/secrets/bind", b'{"key":"HTTPKEY_PW","domain":"example.co.kr"}'),
            ):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=payload, method=method)
                if payload:
                    req.add_header("Content-Type", "application/json")
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(cm.exception.code, 403)
                cm.exception.close()

            # Authenticated GET /api/secrets/unbound and POST /api/secrets/bind -> 200
            req_u = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/secrets/unbound",
                headers={"X-EP-Token": server.TOKEN},
            )
            with urllib.request.urlopen(req_u, timeout=5) as resp:
                data_u = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data_u["keys"], ["HTTPKEY_PW"])
            self.assertNotIn("HttpVal!99", json.dumps(data_u))

            req_b = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/secrets/bind",
                data=json.dumps({"key": "HTTPKEY_PW", "domain": "example.co.kr"}).encode("utf-8"),
                headers={"X-EP-Token": server.TOKEN, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req_b, timeout=5) as resp:
                data_b = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data_b["ok"], data_b)
            self.assertEqual(data_b["domain"], "example.co.kr")
        finally:
            srv.shutdown()
            srv.server_close()

        ui_js = _read_text(os.path.join(ROOT, "ui", "app.js"))
        self.assertIn("/api/secrets/unbound", ui_js)
        self.assertIn("/api/secrets/bind", ui_js)
        self.assertIn("paintUnboundSecrets", ui_js)
        for rel, (n, budget) in ep_prompts.file_report().items():
            self.assertLessEqual(n, budget, rel)


class TestR3A2HostsPunycodeOriginPortAndSuffixes(unittest.TestCase):
    def test_ascii_only_punycode_and_bare_registrable_domains_vs_public_suffixes(self):
        for mod in (ep_sec, cp_sec):
            # Non-ASCII hosts (raw Korean IDN, Cyrillic homograph 'а' U+0430) must be rejected everywhere
            for non_ascii in ("예시.co.kr", "네이버.한국", "n\u0430ver.com", "https://예시.co.kr/login"):
                dom, err = mod.check_bound_domain(non_ascii)
                self.assertEqual(dom, "", non_ascii)
                self.assertTrue(err, non_ascii)
                self.assertTrue(mod.host_ok("SITE_PW", non_ascii, non_ascii, "example.co.kr"))
                self.assertTrue(mod.url_ok("SITE_PW", f"https://{non_ascii}/login", non_ascii, "example.co.kr"))

            # Punycode ASCII xn-- hosts are accepted
            for puny in ("xn--hy1b57d.co.kr", "xn--9i1b674b.xn--3e0b707e", "member.xn--hy1b57d.kr"):
                dom, err = mod.check_bound_domain(puny)
                self.assertEqual(err, "", (puny, err))
                self.assertEqual(dom, puny)
                self.assertEqual(mod.url_ok("SITE_PW", f"https://{puny}/login", puny, puny), "")

            # Bare registrable domains MUST be accepted as exact hosts (subdomains still rejected)
            for reg in ("example.co.kr", "example.go.kr", "brand.co.kr", "org.or.kr", "univ.ac.kr",
                        "isp.ne.kr", "inst.re.kr", "me.pe.kr", "shop.com.au", "example.com", "contest.kr"):
                dom, err = mod.check_bound_domain(reg)
                self.assertEqual(err, "", (reg, err))
                self.assertEqual(dom, reg)
                self.assertEqual(mod.url_ok("SITE_PW", f"https://{reg}/login", reg, reg), "")
                # Subdomain of custom bound host is NOT allowed
                self.assertTrue(mod.url_ok("SITE_PW", f"https://sub.{reg}/login", reg, reg))

            # Bare public suffixes and single-label hosts MUST be refused
            for pub in ("co.kr", "go.kr", "or.kr", "ac.kr", "ne.kr", "re.kr", "pe.kr",
                        "com", "net", "org", "kr", "com.au", "co.jp", "co.uk", "localhost", "xn--3e0b707e"):
                dom, err = mod.check_bound_domain(pub)
                self.assertEqual(dom, "", pub)
                self.assertTrue(err, pub)
                self.assertTrue(mod.url_ok("SITE_PW", f"https://{pub}/login", pub, pub))

    def test_https_origin_port_comparison_default_443(self):
        for mod in (ep_sec, cp_sec):
            # Default port 443 matches implicit 443 and explicit :443, but rejects :8443
            self.assertEqual(mod.url_ok("NAVER_PW", "https://nid.naver.com:443/nidlogin.login", "nid.naver.com"), "")
            self.assertTrue(mod.url_ok("NAVER_PW", "https://nid.naver.com:8443/nidlogin.login", "nid.naver.com"))
            self.assertTrue(mod.url_ok("NAVER_PW", "https://nid.naver.com:8443/nidlogin.login", "nid.naver.com:8443"))

            # Custom bound domain on default port 443
            dom443, err443 = mod.check_bound_domain("https://example.co.kr:443/login")
            self.assertEqual((dom443, err443), ("example.co.kr", ""))
            self.assertEqual(mod.url_ok("SITE_PW", "https://example.co.kr/login", "example.co.kr", dom443), "")
            self.assertEqual(mod.url_ok("SITE_PW", "https://example.co.kr:443/login", "example.co.kr", dom443), "")
            self.assertTrue(mod.url_ok("SITE_PW", "https://example.co.kr:8443/login", "example.co.kr", dom443))
            self.assertTrue(mod.url_ok("SITE_PW", "https://example.co.kr:8443/login", "example.co.kr:8443", dom443))

            # Custom bound domain on explicit port 8443
            dom8443, err8443 = mod.check_bound_domain("https://portal.example.co.kr:8443/sso")
            self.assertEqual((dom8443, err8443), ("portal.example.co.kr:8443", ""))
            self.assertEqual(
                mod.url_ok("SITE_PW", "https://portal.example.co.kr:8443/sso", "portal.example.co.kr:8443", dom8443), ""
            )
            self.assertEqual(
                mod.url_ok(
                    "SITE_PW",
                    "https://portal.example.co.kr:8443/sso?x=1",
                    "https://portal.example.co.kr:8443/sso",
                    dom8443,
                ),
                "",
            )
            # Wrong port (443 or 9443) or port omitted in --url-contains (defaults to 443) rejected
            self.assertTrue(mod.url_ok("SITE_PW", "https://portal.example.co.kr/sso", "portal.example.co.kr", dom8443))
            self.assertTrue(mod.url_ok("SITE_PW", "https://portal.example.co.kr:8443/sso", "portal.example.co.kr", dom8443))
            self.assertTrue(mod.url_ok("SITE_PW", "https://portal.example.co.kr:9443/sso", "portal.example.co.kr:9443", dom8443))


class TestR3A3RealLoginPages(unittest.TestCase):
    def test_known_oauth_hosts_and_x_login_paths(self):
        for mod in (ep_sec, cp_sec):
            # Kakao: kauth, accounts, logins all allowed; pf.kakao.com rejected
            for khost in ("kauth.kakao.com", "accounts.kakao.com", "logins.kakao.com"):
                self.assertEqual(mod.url_ok("KAKAO_PW", f"https://{khost}/login", "kakao.com"), "")
                self.assertEqual(mod.url_ok("KAKAO_ID", f"https://{khost}/oauth/authorize", khost), "")
            self.assertTrue(mod.url_ok("KAKAO_PW", "https://pf.kakao.com/_abc", "kakao.com"))

            # Naver, Google/Gmail/YouTube, Instagram
            self.assertEqual(mod.url_ok("NAVER_PW", "https://nid.naver.com/nidlogin.login", "nid.naver.com"), "")
            for gkey in ("GOOGLE_PW", "GMAIL_PW", "YOUTUBE_PW"):
                self.assertEqual(mod.url_ok(gkey, "https://accounts.google.com/v3/signin/identifier", "accounts.google.com"), "")
                self.assertTrue(mod.url_ok(gkey, "https://www.youtube.com/watch?v=1", "youtube.com"))
            self.assertEqual(mod.url_ok("INSTAGRAM_PW", "https://www.instagram.com/accounts/login/", "www.instagram.com"), "")

            # X / Twitter: login & OAuth pages on x.com, twitter.com, api.x.com allowed; feed/status/API rejected
            for xkey in ("X_PW", "X_USERNAME", "TWITTER_PW", "TWITTER_LOGIN"):
                for ok_url in (
                    "https://x.com/i/flow/login",
                    "https://x.com/login",
                    "https://twitter.com/i/flow/login",
                    "https://twitter.com/oauth/authenticate?oauth_token=xyz",
                    "https://api.x.com/oauth/authenticate?oauth_token=xyz",
                    "https://api.x.com/oauth/authorize?oauth_token=xyz",
                ):
                    self.assertEqual(mod.url_ok(xkey, ok_url, urlparse_host(ok_url)), "", (xkey, ok_url))
                for bad_url in (
                    "https://x.com/home",
                    "https://x.com/",
                    "https://x.com/someone/status/12345",
                    "https://x.com/login_fan/status/12345",
                    "https://twitter.com/explore",
                    "https://api.x.com/2/tweets",
                ):
                    self.assertTrue(mod.url_ok(xkey, bad_url, urlparse_host(bad_url)), (xkey, bad_url))

    def test_skips_hidden_or_disabled_duplicates_and_supports_toggled_password_and_id_suffixes(self):
        for mod in (ep_sec, cp_sec):
            # 1) Hidden + disabled duplicates before the real visible enabled password field
            hidden_pw = _DomInput(None, input_type="password", visible=False)
            disabled_pw = _DomInput(None, input_type="password", visible=True, enabled=False)
            real_pw = _DomInput(None, input_type="password", visible=True, enabled=True)
            fr = _SimFrame("https://accounts.kakao.com/login", {"input[name='password']": [hidden_pw, disabled_pw, real_pw]})
            br = _SimBrowser([_SimPage(fr.url, [fr])])
            res = mod.fill_on_browser(br, "KAKAO_PW", "KakaoPass!12", "", "input[name='password']", "kakao.com")
            self.assertTrue(res["ok"], res)
            self.assertFalse(hidden_pw.clicked)
            self.assertFalse(disabled_pw.clicked)
            self.assertTrue(real_pw.clicked)
            self.assertEqual(real_pw.value, "KakaoPass!12")
            self.assertNotIn("KakaoPass!12", json.dumps(res))

            # 2) Visibility-toggled password fields (<input type="text"> with password autocomplete or id/name)
            for kwargs in (
                {"input_type": "text", "autocomplete": "current-password"},
                {"input_type": "text", "autocomplete": "section-auth new-password"},
                {"input_type": "text", "el_id": "login_pw"},
                {"input_type": "text", "name": "user_pass"},
                {"input_type": "text", "name": "mb_password"},
            ):
                inp = _DomInput(None, **kwargs)
                f2 = _SimFrame("https://nid.naver.com/nidlogin.login", {".inp": [inp]})
                b2 = _SimBrowser([_SimPage(f2.url, [f2])])
                r2 = mod.fill_on_browser(b2, "NAVER_PW", "ToggledPw!99", "", ".inp", "nid.naver.com")
                self.assertTrue(r2["ok"], (kwargs, r2))
                self.assertEqual(inp.value, "ToggledPw!99")

            # Non-password text field (<input type="text" id="search" name="query">) still rejected for PW key
            bad_txt = _DomInput(None, input_type="text", el_id="search", name="query")
            f_bad = _SimFrame("https://nid.naver.com/nidlogin.login", {".inp": [bad_txt]})
            r_bad = mod.fill_on_browser(_SimBrowser([_SimPage(f_bad.url, [f_bad])]), "NAVER_PW", "ToggledPw!99", "", ".inp", "nid.naver.com")
            self.assertFalse(r_bad["ok"])
            self.assertEqual(bad_txt.value, "")

            # 3) ID key suffixes: _ID, _EMAIL, _USER, _USERNAME, _LOGIN
            for suffix in ("_ID", "_EMAIL", "_USER", "_USERNAME", "_LOGIN"):
                inp_id = _DomInput(None, input_type="text", name="username")
                f_id = _SimFrame("https://accounts.google.com/signin", {"#ident": [inp_id]})
                r_id = mod.fill_on_browser(
                    _SimBrowser([_SimPage(f_id.url, [f_id])]), f"GOOGLE{suffix}", "alice@example.com", "", "#ident", "accounts.google.com"
                )
                self.assertTrue(r_id["ok"], (suffix, r_id))
                self.assertEqual(inp_id.value, "alice@example.com")

    def test_post_typing_focus_move_and_rerender_succeed_while_length_mismatch_fails(self):
        for mod in (ep_sec, cp_sec):
            secret_pw = "AutoAdvance!2026"
            # 1) Focus moves after last key -> succeeds without clearing
            inp_blur = _DomInput(None, input_type="password", lose_focus_after_type=True)
            fr1 = _SimFrame("https://kauth.kakao.com/oauth/login", {"#pw": [inp_blur]})
            r1 = mod.fill_on_browser(_SimBrowser([_SimPage(fr1.url, [fr1])]), "KAKAO_PW", secret_pw, "", "#pw", "kakao.com")
            self.assertTrue(r1["ok"], r1)
            self.assertEqual(inp_blur.value, secret_pw)
            self.assertNotIn(secret_pw, json.dumps(r1))

            # 2) SPA re-renders the <input> on the last key (old handle detached, new element matching selector has typed length)
            replacement = _DomInput(None, input_type="password")
            orig = _DomInput(None, input_type="password", detach_after_type=True, replace_with_on_type=replacement)
            fr2 = _SimFrame("https://logins.kakao.com/login", {"#pw": [orig]})
            r2 = mod.fill_on_browser(_SimBrowser([_SimPage(fr2.url, [fr2])]), "KAKAO_PW", secret_pw, "", "#pw", "kakao.com")
            self.assertTrue(r2["ok"], r2)
            self.assertEqual(replacement.value, secret_pw)

            # 3) Partial value length (focus lost mid-typing) -> clears and fails
            partial = _DomInput(None, input_type="password", partial_len_on_type=5)
            fr3 = _SimFrame("https://nid.naver.com/nidlogin.login", {"#pw": [partial]})
            r3 = mod.fill_on_browser(_SimBrowser([_SimPage(fr3.url, [fr3])]), "NAVER_PW", secret_pw, "", "#pw", "nid.naver.com")
            self.assertFalse(r3["ok"])
            self.assertEqual(partial.value, "")

    def test_two_tabs_on_same_host_disambiguation(self):
        for mod in (ep_sec, cp_sec):
            # 1) Full URL in --url-contains prefers exact full URL match even when both tabs have visible #pw
            in_a = _DomInput(None, input_type="password")
            in_b = _DomInput(None, input_type="password")
            p_a = _SimPage("https://nid.naver.com/nidlogin.login?session=AAA", [_SimFrame("https://nid.naver.com/nidlogin.login?session=AAA", {"#pw": [in_a]})])
            p_b = _SimPage("https://nid.naver.com/nidlogin.login?session=BBB", [_SimFrame("https://nid.naver.com/nidlogin.login?session=BBB", {"#pw": [in_b]})])
            r_exact = mod.fill_on_browser(
                _SimBrowser([p_a, p_b]), "NAVER_PW", "Secret!123", "", "#pw", "https://nid.naver.com/nidlogin.login?session=BBB"
            )
            self.assertTrue(r_exact["ok"], r_exact)
            self.assertEqual(in_a.value, "")
            self.assertEqual(in_b.value, "Secret!123")

            # 2) Host-only --url-contains prefers the tab where #pw is visible
            in_hidden = _DomInput(None, input_type="password", visible=False)
            in_vis = _DomInput(None, input_type="password", visible=True)
            p_other = _SimPage("https://nid.naver.com/user2/help", [_SimFrame("https://nid.naver.com/user2/help", {"#pw": [in_hidden]})])
            p_login = _SimPage("https://nid.naver.com/nidlogin.login", [_SimFrame("https://nid.naver.com/nidlogin.login", {"#pw": [in_vis]})])
            r_vis = mod.fill_on_browser(_SimBrowser([p_other, p_login]), "NAVER_PW", "Secret!456", "", "#pw", "nid.naver.com")
            self.assertTrue(r_vis["ok"], r_vis)
            self.assertEqual(in_vis.value, "Secret!456")

            # 3) Still ambiguous when both tabs on host have visible #pw and --url-contains is host-only -> refused
            amb1 = _DomInput(None, input_type="password", visible=True)
            amb2 = _DomInput(None, input_type="password", visible=True)
            pa1 = _SimPage("https://nid.naver.com/nidlogin.login?x=1", [_SimFrame("https://nid.naver.com/nidlogin.login?x=1", {"#pw": [amb1]})])
            pa2 = _SimPage("https://nid.naver.com/nidlogin.login?x=2", [_SimFrame("https://nid.naver.com/nidlogin.login?x=2", {"#pw": [amb2]})])
            r_amb = mod.fill_on_browser(_SimBrowser([pa1, pa2]), "NAVER_PW", "Secret!789", "", "#pw", "nid.naver.com")
            self.assertFalse(r_amb["ok"])
            self.assertEqual(amb1.value, "")
            self.assertEqual(amb2.value, "")


def urlparse_host(u: str) -> str:
    from urllib.parse import urlparse
    return urlparse(u).hostname or ""


class TestR3A4MigrationPreservesUndecryptableAndValidatesBeforeWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r3a-a4-")
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

    def test_preserves_foreign_pc_ciphertext_migrates_new_plaintext_and_warns_once(self):
        foreign_blob = "dpapi:Qm9ndXNGb3JlaWduUGNCbG9iMTIzNDU2Nzg5MA=="
        for mod in (ep_sec, cp_sec):
            mod._UNDECRYPTABLE_WARNED.clear()
            w_start = len(mod.UNDECRYPTABLE_WARNINGS)
            _write_text(mod.AUTO, f"FOREIGN_PW={foreign_blob}\n")
            _write_text(mod.SECRETS, "NAVER_PW=FreshNaver!1\nEVENT_PW@example.co.kr=FreshEvent!2\n")

            moved = mod.migrate()
            self.assertEqual(moved, ["EVENT_PW", "NAVER_PW"])
            # Foreign undecryptable ciphertext kept EXACTLY as-is!
            auto_map = mod.read_file(mod.AUTO)
            self.assertEqual(auto_map["FOREIGN_PW"], foreign_blob)
            self.assertEqual(mod.unprotect(auto_map["NAVER_PW"]), "FreshNaver!1")
            self.assertEqual(mod.unpack(auto_map["EVENT_PW"]), ("FreshEvent!2", "example.co.kr"))

            # Plaintext emptied from secrets.env
            sec_txt = _read_text(mod.SECRETS)
            self.assertNotIn("FreshNaver!1", sec_txt)
            self.assertNotIn("FreshEvent!2", sec_txt)

            # Reported once (key name only, never ciphertext)
            self.assertEqual(len(mod.UNDECRYPTABLE_WARNINGS) - w_start, 1)
            self.assertIn("FOREIGN_PW", mod.UNDECRYPTABLE_WARNINGS[-1])
            self.assertNotIn(foreign_blob, mod.UNDECRYPTABLE_WARNINGS[-1])
            mod.migrate()
            self.assertEqual(len(mod.UNDECRYPTABLE_WARNINGS) - w_start, 1)

            # Subsequent `secret set` on a new key also keeps FOREIGN_PW intact
            rc, out = _run_cli(mod, ["set", "NEWJOIN_PW", "--domain", "join.co.kr", "--generate", "14"])
            self.assertEqual(rc, 0, out)
            self.assertEqual(mod.read_file(mod.AUTO)["FOREIGN_PW"], foreign_blob)

            # If user re-enters FOREIGN_PW in secrets.env, it replaces the undecryptable value cleanly
            _write_text(mod.SECRETS, "FOREIGN_PW@example.co.kr=ReplacedPw!3\n")
            self.assertEqual(mod.migrate(), ["FOREIGN_PW"])
            self.assertEqual(mod.unpack(mod.read_file(mod.AUTO)["FOREIGN_PW"]), ("ReplacedPw!3", "example.co.kr"))

    def test_writes_files_only_after_whole_new_content_is_validated(self):
        for mod in (ep_sec, cp_sec):
            foreign_blob = "dpapi:ForeignKeepIntact999"
            orig_auto = f"# header\nFOREIGN_PW={foreign_blob}\n"
            orig_sec = "GOOD_PW=PlainOne1!\nSECOND_PW=PlainTwo2!\n"
            _write_text(mod.AUTO, orig_auto)
            _write_text(mod.SECRETS, orig_sec)

            # Simulate unpack returning a corrupted plaintext on the 2nd key so validation fails mid-migrate
            real_unpack = mod.unpack
            calls = [0]

            def flaky_unpack(stored):
                val, dom = real_unpack(stored)
                if val == "PlainTwo2!":
                    calls[0] += 1
                    return ("corrupted", dom)
                return (val, dom)

            with mock.patch.object(mod, "unpack", side_effect=flaky_unpack):
                with self.assertRaises(OSError):
                    mod.migrate()

            self.assertGreaterEqual(calls[0], 1)
            # Neither file was rewritten on disk!
            self.assertEqual(_read_text(mod.AUTO), orig_auto)
            self.assertEqual(_read_text(mod.SECRETS), orig_sec)


class TestR3A5ProtectedDaclViaSetNamedSecurityInfoW(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r3a-a5-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sets_protected_dacl_with_explicit_sid_ace_without_icacls_reset(self):
        icacls = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "icacls.exe")
        adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32

        for idx, mod in enumerate((secure, ep_sec, cp_sec)):
            sid = mod.current_sid()
            self.assertTrue(sid.upper().startswith("S-1-5-"), sid)

            sub_dir = os.path.join(self.tmp, f"dir_{idx}")
            os.makedirs(sub_dir, exist_ok=True)
            fp = os.path.join(sub_dir, "secret.txt")
            _write_text(fp, "top-secret")

            # Grant Everyone:(R) before calling restrict_acl
            subprocess.run([icacls, fp, "/grant", "*S-1-1-0:R"],
                           capture_output=True, text=True, encoding="mbcs", errors="replace", check=True)

            with mock.patch("subprocess.run", wraps=subprocess.run) as m_run:
                self.assertTrue(mod.restrict_acl(sub_dir))
                self.assertTrue(mod.restrict_acl(fp))
                # No icacls subprocess needed (and never /reset!)
                for call_args in m_run.call_args_list:
                    cmd = call_args[0][0]
                    self.assertNotIn("/reset", cmd)

            # Verify SE_DACL_PROTECTED directly via GetNamedSecurityInfoW
            for target_p in (sub_dir, fp):
                pdacl = ctypes.c_void_p()
                psd = ctypes.c_void_p()
                rc = adv.GetNamedSecurityInfoW(
                    target_p, 1, 0x00000004, None, None, ctypes.byref(pdacl), None, ctypes.byref(psd)
                )
                self.assertEqual(rc, 0)
                try:
                    ctrl = wintypes.WORD(0)
                    rev = wintypes.DWORD(0)
                    self.assertTrue(adv.GetSecurityDescriptorControl(psd, ctypes.byref(ctrl), ctypes.byref(rev)))
                    self.assertTrue(ctrl.value & 0x1000, f"SE_DACL_PROTECTED missing on {target_p}: 0x{ctrl.value:04x}")
                finally:
                    if psd.value:
                        k32.LocalFree(psd)

            # Also verify externally via System32\icacls.exe: single explicit user (F) ACE, no Everyone, no (I)
            ver = subprocess.run([icacls, fp], capture_output=True, text=True, encoding="mbcs", errors="replace", check=True)
            aces = mod._parse_icacls_aces(ver.stdout, fp)
            self.assertEqual(len(aces), 1, ver.stdout)
            self.assertNotIn("everyone", ver.stdout.lower())
            self.assertNotIn("(I)", aces[0].upper())
            self.assertIn("(F)", aces[0].upper())


if __name__ == "__main__":
    unittest.main()
