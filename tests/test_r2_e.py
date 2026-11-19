"""Round 2 security tests for worker E (items E1-E6).
unittest only; no network, no real Chrome, no AI CLI, no real PyInstaller build."""
import contextlib
import hashlib
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import zipfile
from email.message import Message
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cp import crawler as cp_crawler, crawler_chrome as cp_crawler_chrome, form_read as cp_form_read  # noqa: E402
from cp import config as cp_config, store as cp_store, urlpolicy as cp_urlpolicy  # noqa: E402
from cp.agents import base as cp_base  # noqa: E402
from ep import crawler as ep_crawler, crawler_chrome as ep_crawler_chrome, form_read as ep_form_read  # noqa: E402
from ep import config as ep_config, sns_login, store as ep_store, urlpolicy as ep_urlpolicy  # noqa: E402
from ep.agents import base as ep_base  # noqa: E402


def _sys32(exe: str) -> str:
    sysroot = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(sysroot, "System32", exe)


def _load_build_mod():
    spec = importlib.util.spec_from_file_location("ep_build_r2e", os.path.join(ROOT, "build", "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _addrinfo_v4(ip: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


def _addrinfo_v6(ip: str):
    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0))]


# ================================================================ E1: URL Policy & Crawlers
class TestE1UrlPolicy(unittest.TestCase):
    def test_public_http_and_https_allowed_in_both_twins(self):
        for mod in (ep_urlpolicy, cp_urlpolicy):
            with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4("93.184.216.34")) as gai:
                ok, reason = mod.is_public_http_url("https://events.kr/event.php?id=1")
                self.assertTrue(ok, reason)
                self.assertEqual(reason, "")
                gai.assert_called_once()
            with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v6("2606:2800:220:1:248:1893:25c8:1946")):
                ok, reason = mod.is_public_http_url("http://example.org/contest")
                self.assertTrue(ok, reason)

    def test_refuses_bad_schemes_missing_host_and_local_names(self):
        bad_urls = [
            "",
            "   ",
            "file:///C:/Windows/win.ini",
            "data:text/html,<h1>x</h1>",
            "javascript:alert(1)",
            "chrome://settings",
            "about:blank",
            "view-source:https://example.com/",
            "ftp://example.com/file",
            "http:///no-host",
            "https://user:pass@example.com/",
            "http://localhost/admin",
            "http://LOCALHOST./admin",
            "http://sub.localhost:8080/",
            "http://printer.local/",
            "http://metadata.internal/latest",
            "http://intranet/",
        ]
        for mod in (ep_urlpolicy, cp_urlpolicy):
            with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4("93.184.216.34")) as gai:
                for u in bad_urls:
                    ok, reason = mod.is_public_http_url(u)
                    self.assertFalse(ok, f"{mod.__name__} should reject {u!r}")
                    self.assertTrue(reason)
                gai.assert_not_called()

    def test_refuses_private_loopback_cgnat_linklocal_ula_multicast_unspecified(self):
        blocked_ips_v4 = [
            "127.0.0.1",
            "127.255.255.254",
            "10.0.0.1",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.1.1",
            "100.64.0.1",
            "100.127.255.254",
            "169.254.169.254",
            "224.0.0.1",
            "0.0.0.0",
            "255.255.255.255",
        ]
        blocked_ips_v6 = [
            "::1",
            "::",
            "fe80::1",
            "fc00::1",
            "fd12:3456:789a::1",
            "ff02::1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:100.64.0.1",
        ]
        for mod in (ep_urlpolicy, cp_urlpolicy):
            for ip in blocked_ips_v4:
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4(ip)):
                    ok, reason = mod.is_public_http_url("https://rebind.example.com/form")
                    self.assertFalse(ok, f"{mod.__name__} should block {ip}")
                    self.assertTrue(reason)
            for ip in blocked_ips_v6:
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v6(ip)):
                    ok, reason = mod.is_public_http_url("https://rebind6.example.com/form")
                    self.assertFalse(ok, f"{mod.__name__} should block {ip}")
                    self.assertTrue(reason)
            # Mixed public + private DNS records -> refused
            mixed = _addrinfo_v4("93.184.216.34") + _addrinfo_v4("10.1.2.3")
            with mock.patch("socket.getaddrinfo", return_value=mixed):
                ok, _ = mod.is_public_http_url("https://dual.example.com/")
                self.assertFalse(ok)
            # Alternate numeric IPv4 notations refused even before DNS
            for alt in ("http://2130706433/", "http://0177.0.0.1/", "http://0x7f000001/", "http://127.1/"):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4("93.184.216.34")):
                    ok, _ = mod.is_public_http_url(alt)
                    self.assertFalse(ok, alt)

    def test_crawlers_check_every_request_and_redirect_hop(self):
        def fake_gai(host, port, *a, **kw):
            if host in ("public.example.com", "next.example.com"):
                return _addrinfo_v4("93.184.216.34")
            if host == "ssrf.example.com":
                return _addrinfo_v4("169.254.169.254")
            raise socket.gaierror("unknown")

        class FakeResp:
            def __init__(self, body: bytes, url: str, ct: str = "text/html; charset=utf-8"):
                self._body = body
                self._url = url
                self.headers = {"Content-Type": ct}

            def geturl(self):
                return self._url

            def read(self, n=-1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def make_redirect(code: int, location: str, url: str):
            hdrs = Message()
            hdrs["Location"] = location
            return urllib.error.HTTPError(url, code, "Found", hdrs, io.BytesIO(b""))

        with mock.patch("socket.getaddrinfo", side_effect=fake_gai):
            # 1) ep.crawler.Fetcher blocks redirect hop to private IP before opening it
            opened = []
            f_ep = ep_crawler.Fetcher(delay=0)

            def ep_open(req, timeout=None):
                opened.append(req.full_url)
                if req.full_url == "https://public.example.com/start":
                    raise make_redirect(302, "http://127.0.0.1:8080/secret", req.full_url)
                if req.full_url == "https://public.example.com/ok":
                    raise make_redirect(302, "https://next.example.com/final", req.full_url)
                if req.full_url == "https://next.example.com/final":
                    return FakeResp(b"hello public", req.full_url)
                raise AssertionError("unexpected open: " + req.full_url)

            f_ep._noredir.open = ep_open
            with self.assertRaises(RuntimeError):
                f_ep("https://public.example.com/start")
            self.assertEqual(opened, ["https://public.example.com/start"])  # 127.0.0.1 never opened!

            opened.clear()
            self.assertEqual(f_ep("https://public.example.com/ok"), "hello public")
            self.assertEqual(opened, ["https://public.example.com/ok", "https://next.example.com/final"])

            # ep.crawler.Fetcher.resolve blocks external redirect pointing to private host
            opened.clear()

            def ep_resolve_open(req, timeout=None):
                opened.append(req.full_url)
                raise make_redirect(302, "http://ssrf.example.com/meta", req.full_url)

            f_ep._noredir.open = ep_resolve_open
            with self.assertRaises(RuntimeError):
                f_ep.resolve("https://public.example.com/fgo")
            self.assertEqual(opened, ["https://public.example.com/fgo"])

            # 2) cp.crawler.Fetcher checks robots + redirect hops and blocks private redirects
            cp_opened = []
            f_cp = cp_crawler.Fetcher(delay=0, check_robots=True)

            def cp_open(req, timeout=None):
                cp_opened.append(req.full_url)
                if req.full_url.endswith("/robots.txt"):
                    return FakeResp(b"User-agent: *\nAllow: /\n", req.full_url, "text/plain")
                if req.full_url == "https://public.example.com/c1":
                    raise make_redirect(301, "http://ssrf.example.com/internal", req.full_url)
                raise AssertionError("unexpected cp open: " + req.full_url)

            f_cp._noredir.open = cp_open
            with self.assertRaises(RuntimeError):
                f_cp("https://public.example.com/c1")
            self.assertNotIn("http://ssrf.example.com/internal", cp_opened)
            self.assertNotIn("http://ssrf.example.com/robots.txt", cp_opened)

            # 3) Chrome crawlers guard route requests against private subresources/redirects and fulfill via pinned fetch
            for cc_mod, up_mod in ((ep_crawler_chrome, ep_urlpolicy), (cp_crawler_chrome, cp_urlpolicy)):
                routes = []
                pg = mock.Mock()
                pg.url = "https://search.naver.com/search.naver"
                pg.route.side_effect = lambda pat, fn: routes.append(fn)
                cc_mod._guard_page(pg)
                self.assertEqual(len(routes), 1)
                r_priv = mock.Mock(request=mock.Mock(url="http://192.168.0.1/steal", method="GET", headers={}))
                routes[0](r_priv)
                r_priv.abort.assert_called_once()
                fake_conn = mock.MagicMock()
                fake_conn.getresponse.return_value = mock.Mock(
                    status=200,
                    read=lambda n=-1: b"body{}",
                    getheaders=lambda: [("Content-Type", "text/css")],
                    getheader=lambda name, default=None: {"Content-Type": "text/css"}.get(name, default),
                )
                with mock.patch.object(up_mod, "PinnedHTTPSConnection", return_value=fake_conn):
                    r_pub = mock.Mock(request=mock.Mock(url="https://public.example.com/style.css", method="GET", headers={}))
                    routes[0](r_pub)
                r_pub.fulfill.assert_called_once()
                r_pub.continue_.assert_not_called()


# ================================================================ E2: form_read Read-Only & SSRF Guards
class _FakeElement:
    def __init__(self, text: str, tag: str = "BUTTON", unsafe: str = "", href: str = "", act: str = "", on_click=None):
        self._text = text
        self._tag = tag.upper()
        self._unsafe = unsafe
        self._href = href
        self._act = act
        self._on_click = on_click
        self.clicked = 0

    def is_visible(self):
        return True

    def input_value(self):
        return self._text

    def inner_text(self):
        return self._text

    def evaluate(self, js):
        if "toUpperCase" in js and "href" not in js and "UNSAFE" not in js and "submit-input" not in js:
            return self._tag
        if "submit-input" in js:
            return self._unsafe
        if "href:" in js:
            return {"tag": self._tag, "href": self._href, "act": self._act}
        return ""

    def click(self, timeout=5000):
        self.clicked += 1
        if self._on_click:
            self._on_click()


class _FakeLocator:
    def __init__(self, elements):
        self._els = list(elements)

    def count(self):
        return len(self._els)

    def nth(self, i):
        return self._els[i]


class _FakePage:
    def __init__(self, url: str, steps: list[dict], on_goto=None):
        self.url = url
        self._steps = steps
        self._idx = 0
        self._route_handler = None
        self._on_goto = on_goto
        self.main_frame = self
        self.frames = [self]
        self.evaluate_calls = []

    def route(self, pattern, handler):
        self._route_handler = handler

    def fire_request(self, url: str, method: str = "GET", nav: bool = False):
        req = mock.Mock(url=url, method=method)
        req.is_navigation_request = lambda: nav
        req.frame = self
        rt = mock.Mock(request=req)
        self._route_handler(rt)
        return rt

    def goto(self, url, wait_until="networkidle", timeout=45000):
        rt = self.fire_request(url, "GET", nav=True)
        if rt.abort.called:
            raise RuntimeError("ERR_ABORTED")
        self.url = url
        if self._on_goto:
            self._on_goto(self)

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state, timeout=8000):
        pass

    def content(self):
        return f"<html><body>{self._steps[self._idx]['text']}</body></html>"

    def inner_text(self, sel):
        return self._steps[self._idx]["text"]

    def eval_on_selector_all(self, selector, js):
        if "INPUTS_JS" in js or "aria-required" in js:
            return list(self._steps[self._idx].get("inputs", []))
        return list(self._steps[self._idx].get("buttons", []))

    def locator(self, selector):
        return _FakeLocator(self._steps[self._idx].get("els", []))

    def evaluate(self, js):
        self.evaluate_calls.append(js)
        return []


def _fake_pw(page: _FakePage):
    @contextlib.contextmanager
    def factory():
        ctx = mock.Mock()
        ctx.new_page.return_value = page
        ctx.cookies.return_value = []
        browser = mock.Mock()
        browser.new_context.return_value = ctx
        browser.new_page.return_value = page
        pw = mock.Mock()
        pw.chromium.launch.return_value = browser
        yield pw
    return factory


class TestE2FormRead(unittest.TestCase):
    def setUp(self):
        p = mock.patch("socket.getaddrinfo", side_effect=lambda h, port, *a, **k: (
            _addrinfo_v4("127.0.0.1") if "local" in h or "priv" in h else _addrinfo_v4("93.184.216.34")
        ))
        p.start()
        self.addCleanup(p.stop)

        def _make_fake_conn(*args, **kwargs):
            conn = mock.MagicMock()
            conn.getresponse.return_value = mock.Mock(
                status=200,
                read=lambda n=-1: b"<html><body>ok</body></html>",
                getheaders=lambda: [("Content-Type", "text/html; charset=utf-8")],
                getheader=lambda name, default=None: {"Content-Type": "text/html; charset=utf-8"}.get(name, default),
            )
            return conn

        for mod in (ep_urlpolicy, cp_urlpolicy):
            for cls_name in ("PinnedHTTPConnection", "PinnedHTTPSConnection"):
                pc = mock.patch.object(mod, cls_name, side_effect=_make_fake_conn)
                pc.start()
                self.addCleanup(pc.stop)

    def test_refuses_non_public_initial_url_and_aborts_private_subresources_and_redirects(self):
        for fr in (ep_form_read, cp_form_read):
            # Initial private/file URL rejected before browser launch
            pw_never = mock.Mock(side_effect=AssertionError("playwright must not start"))
            for bad in ("file:///C:/secret.txt", "http://127.0.0.1:8080/", "https://priv.example.com/form"):
                with self.assertRaises(RuntimeError):
                    fr.read_form(bad, pw_factory=pw_never)

            # Subresource to private IP is aborted by route guard while public page loads via route.fulfill
            sub_results = []

            def on_goto(pg):
                sub_results.append(pg.fire_request("http://192.168.1.1/pixel.png", "GET", nav=False))
                sub_results.append(pg.fire_request("https://public.example.com/app.css", "GET", nav=False))

            pg = _FakePage("https://public.example.com/survey", [{"text": "Question 1?", "inputs": [], "els": []}], on_goto=on_goto)
            res = fr.read_form("https://public.example.com/survey", pw_factory=_fake_pw(pg))
            self.assertTrue(res["ok"])
            sub_results[0].abort.assert_called_once()
            sub_results[1].fulfill.assert_called_once()
            sub_results[1].continue_.assert_not_called()

            # Redirect hop to private range aborts navigation and fails read_form
            def on_redir(pg2):
                rt = pg2.fire_request("http://169.254.169.254/latest/meta-data/", "GET", nav=True)
                self.assertTrue(rt.abort.called)

            pg_redir = _FakePage("https://public.example.com/go", [{"text": "x"}], on_goto=on_redir)
            with self.assertRaises(RuntimeError):
                fr.read_form("https://public.example.com/go", pw_factory=_fake_pw(pg_redir))

    def test_never_clicks_submit_or_post_form_and_aborts_non_get(self):
        for fr in (ep_form_read, cp_form_read):
            self.assertFalse(hasattr(fr, "FILL_JS"), "dummy placeholder fill must be removed")
            for unsafe_kind in ("submit-input", "submit-button", "form-submit-button", "post-form", "formmethod-post"):
                btn = _FakeElement("다음", tag="INPUT" if unsafe_kind == "submit-input" else "BUTTON", unsafe=unsafe_kind)
                pg = _FakePage("https://public.example.com/s1", [{
                    "text": "1. 선호하는 색상은?",
                    "inputs": [{"tag": "INPUT", "type": "text", "name": "q1", "required": True, "label": "색상"}],
                    "buttons": ["다음"],
                    "els": [btn],
                }])
                res = fr.read_form("https://public.example.com/s1", pw_factory=_fake_pw(pg))
                self.assertEqual(btn.clicked, 0, f"must never click {unsafe_kind}")
                self.assertEqual(res["stopped"], "needs-submit")
                self.assertTrue(res["needs_submit"])
                self.assertTrue(res.get("manual_review"))

    def test_aborts_xhr_post_and_other_non_get_methods_and_marks_needs_submit(self):
        for fr in (ep_form_read, cp_form_read):
            aborted_methods = []
            pg_holder = []

            def click_triggers_post():
                pg = pg_holder[0]
                for m in ("POST", "PUT", "PATCH", "DELETE"):
                    rt = pg.fire_request("https://public.example.com/api/step", m, nav=False)
                    if rt.abort.called:
                        aborted_methods.append(m)

            btn = _FakeElement("다음", tag="BUTTON", unsafe="", on_click=click_triggers_post)
            pg = _FakePage("https://public.example.com/step1", [{
                "text": "Page 1 question",
                "inputs": [],
                "buttons": ["다음"],
                "els": [btn],
            }])
            pg_holder.append(pg)
            res = fr.read_form("https://public.example.com/step1", pw_factory=_fake_pw(pg))
            self.assertEqual(btn.clicked, 1)
            self.assertEqual(aborted_methods, ["POST", "PUT", "PATCH", "DELETE"])
            self.assertEqual(res["stopped"], "needs-submit")
            self.assertTrue(res["needs_submit"])

    def test_safe_client_side_next_button_still_walks_pages(self):
        for fr in (ep_form_read, cp_form_read):
            pg_ref = []
            btn = _FakeElement("다음", tag="BUTTON", unsafe="", on_click=lambda: setattr(pg_ref[0], "_idx", 1))
            pg = _FakePage("https://public.example.com/multi", [
                {"text": "Step 1 question", "inputs": [], "buttons": ["다음"], "els": [btn]},
                {"text": "Step 2 question", "inputs": [], "buttons": [], "els": []},
            ])
            pg_ref.append(pg)
            res = fr.read_form("https://public.example.com/multi", pw_factory=_fake_pw(pg))
            self.assertEqual(res["page_count"], 2)
            self.assertEqual(res["stopped"], "no-next")
            self.assertFalse(res["needs_submit"])
            self.assertIn("Step 2 question", res["text"])


# ================================================================ E3 & E4: Task Files & Log Scrubbing
class _SpyAdapter(ep_base.Adapter):
    name = "codex"

    def __init__(self):
        self.commands_called = []

    def pick_account(self, db):
        return "acct"

    def command(self, prompt, kind):
        self.commands_called.append((prompt, kind))
        return [sys.executable, "-c", "pass"]


class _SpyCpAdapter(cp_base.Adapter):
    name = "codex"

    def __init__(self):
        self.commands_called = []

    def pick_account(self, db):
        return "acct"

    def command(self, prompt, kind):
        self.commands_called.append((prompt, kind))
        return [sys.executable, "-c", "pass"]


class TestE3TaskFilesAndE4LogScrub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2e-tasks-")
        self._env = dict(os.environ)
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "data", "board")
        os.environ["CP_APP_DIR"] = self.tmp
        os.environ["CP_DATA_DIR"] = os.path.join(self.tmp, "data", "contest")
        os.environ["CP_OWNER_DIR"] = os.path.join(self.tmp, "data", "contest", "board")
        os.makedirs(os.environ["EP_DATA_DIR"], exist_ok=True)
        os.makedirs(os.environ["CP_DATA_DIR"], exist_ok=True)
        ep_base._BOOT_REAPED.clear()
        cp_base._BOOT_REAPED.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_junction(self, link_path: str, target_path: str):
        r = subprocess.run([_sys32("cmd.exe"), "/d", "/c", "mklink", "/J", link_path, target_path],
                           capture_output=True, text=True, encoding="mbcs", errors="replace", check=True)
        return r

    def test_refuses_tasks_junction_and_parent_junction_before_writing(self):
        outside = os.path.join(self.tmp, "outside-sink")
        os.makedirs(outside, exist_ok=True)

        # 1) ep: data\tasks is a junction -> launch refuses, nothing written to outside
        ep_tasks = os.path.join(ep_config.data_dir(), "tasks")
        self._mk_junction(ep_tasks, outside)
        db = ep_store.connect()
        try:
            spy = _SpyAdapter()
            with self.assertRaises(ep_base.LaunchError):
                ep_base.launch(spy, "submit", {"id": "E01"}, "SECRET-PROFILE-DATA", db, account="acct")
            self.assertEqual(os.listdir(outside), [])
            self.assertEqual(spy.commands_called, [])
        finally:
            db.close()
            os.rmdir(ep_tasks)

        # 2) cp: data\contest\tasks is a junction -> launch refuses, nothing written to outside
        cp_tasks = os.path.join(cp_config.data_dir(), "tasks")
        self._mk_junction(cp_tasks, outside)
        cdb = cp_store.connect()
        try:
            cspy = _SpyCpAdapter()
            with self.assertRaises(cp_base.LaunchError):
                cp_base.launch(cspy, "draft", {"id": "C01"}, "SECRET-CONTEST-PROFILE", cdb, account="acct")
            self.assertEqual(os.listdir(outside), [])
            self.assertEqual(cspy.commands_called, [])
        finally:
            cdb.close()
            os.rmdir(cp_tasks)

        # 3) Parent data dir itself is a junction -> launch refuses before creating tasks
        real_store_dir = os.path.join(self.tmp, "real-db-dir")
        os.makedirs(real_store_dir, exist_ok=True)
        db2 = ep_store.connect(os.path.join(real_store_dir, "platform.db"))
        junc_data = os.path.join(self.tmp, "junc-data")
        self._mk_junction(junc_data, outside)
        os.environ["EP_DATA_DIR"] = junc_data
        try:
            spy2 = _SpyAdapter()
            with self.assertRaises(ep_base.LaunchError):
                ep_base.launch(spy2, "verify", {"id": "E02"}, "SECRET-PROFILE-PARENT", db2, account="acct")
            self.assertEqual(os.listdir(outside), [])
            self.assertEqual(spy2.commands_called, [])
        finally:
            db2.close()
            os.rmdir(junc_data)

    def test_agent_guide_read_failure_fails_launch_instead_of_putting_prompt_on_cmdline(self):
        # ep: missing agent-guide.md
        db = ep_store.connect()
        try:
            spy = _SpyAdapter()
            with mock.patch.object(ep_config, "PROJECT_DIR", os.path.join(self.tmp, "no-such-project")):
                with self.assertRaises(ep_base.LaunchError) as ctx:
                    ep_base.launch(spy, "submit", {"id": "E09"}, "Profile: 010-1234-5678 Secret Name", db, account="acct")
            self.assertIn("agent guide", str(ctx.exception).lower())
            self.assertEqual(spy.commands_called, [])
            tasks_dir = os.path.join(ep_config.data_dir(), "tasks")
            if os.path.isdir(tasks_dir):
                self.assertEqual(os.listdir(tasks_dir), [])
        finally:
            db.close()

        # cp: missing agent-guide.md
        cdb = cp_store.connect()
        try:
            cspy = _SpyCpAdapter()
            with mock.patch.object(cp_config, "PROMPTS_SRC", os.path.join(self.tmp, "no-such-prompts")):
                with self.assertRaises(cp_base.LaunchError) as ctx2:
                    cp_base.launch(cspy, "submit", {"id": "C09"}, "Profile: 010-1234-5678 Secret Name", cdb, account="acct")
            self.assertIn("agent guide", str(ctx2.exception).lower())
            self.assertEqual(cspy.commands_called, [])
        finally:
            cdb.close()

    def test_orphan_task_files_reaped_on_engine_start(self):
        for cfg, st, bmod, prefix in (
            (ep_config, ep_store, ep_base, "E"),
            (cp_config, cp_store, cp_base, "C"),
        ):
            tasks_dir = os.path.join(cfg.data_dir(), "tasks")
            os.makedirs(tasks_dir, exist_ok=True)
            guide = Path(tasks_dir, "agent-guide.md")
            guide.write_text("guide", encoding="utf-8")

            # Case 1: No running tasks -> all leftover task .md files deleted on initial reap(), guide kept
            orphan1 = Path(tasks_dir, f"codex-submit-{prefix}01.md")
            orphan2 = Path(tasks_dir, f"claude-verify-{prefix}02.md")
            orphan1.write_text("leftover 1", encoding="utf-8")
            orphan2.write_text("leftover 2", encoding="utf-8")
            db = st.connect()
            try:
                bmod._BOOT_REAPED.clear()
                bmod.reap(db)
                self.assertFalse(orphan1.exists())
                self.assertFalse(orphan2.exists())
                self.assertTrue(guide.exists())

                # Case 2: Running task exists -> older orphan deleted, active & newer files kept
                now_ts = time.time()
                started_iso = st.now_iso()
                old_orphan = Path(tasks_dir, f"gemini-submit-{prefix}10.md")
                active_file = Path(tasks_dir, f"codex-submit-{prefix}11.md")
                newer_file = Path(tasks_dir, f"claude-scout-{prefix}12.md")
                for p in (old_orphan, active_file, newer_file):
                    p.write_text("task", encoding="utf-8")
                os.utime(old_orphan, (now_ts - 120, now_ts - 120))
                os.utime(active_file, (now_ts - 60, now_ts - 60))
                os.utime(newer_file, (now_ts + 120, now_ts + 120))

                with st.writer(db):
                    cur = db.execute(
                        "INSERT INTO runs(kind, item_id, agent, pid, started) VALUES(?,?,?,?,?)",
                        ("submit", f"{prefix}11", "codex", os.getpid(), started_iso),
                    )
                    st.kv_set(db, f"run.task.{cur.lastrowid}", str(active_file))

                bmod._BOOT_REAPED.clear()
                bmod.reap(db)
                self.assertFalse(old_orphan.exists(), "orphan older than newest running task must be deleted")
                self.assertTrue(active_file.exists(), "running task's own file must stay")
                self.assertTrue(newer_file.exists(), "file newer than newest running task must stay")
                self.assertTrue(guide.exists())
            finally:
                db.close()

    def test_e4_scrub_keeps_ordinary_16_letter_words_and_scrubs_tokens_spaced_app_pw_and_exact_secrets(self):
        tok = "1234567890:AAH" + "k" * 32
        spaced_pw = "abcd efgh ijkl mnop"
        nbsp_pw = "qrst\u00a0uvwx\u00a0yzab\u00a0cdef"
        known_secret = "MyExactKnownSecret!99"
        ordinary = "responsibilities acknowledgements characterization internationalize"
        sample = f"Evidence: {ordinary}. Token={tok} pw={spaced_pw} nbsp={nbsp_pw} custom={known_secret}"

        for bmod in (ep_base, cp_base):
            self.assertFalse(hasattr(bmod, "APP_PW_CONTIG_RE"))
            cleaned = bmod.scrub_text(sample, secrets={known_secret})
            for word in ordinary.split():
                self.assertIn(word, cleaned, f"ordinary word {word!r} must not be blanked")
            self.assertNotIn(tok, cleaned)
            self.assertNotIn("abcd efgh", cleaned)
            self.assertNotIn("qrst\u00a0uvwx", cleaned)
            self.assertNotIn(known_secret, cleaned)
            self.assertIn("[hidden]", cleaned)

            log_file = os.path.join(self.tmp, f"{bmod.__name__}.log")
            Path(log_file).write_text(sample, encoding="utf-8")
            bmod.scrub_log(log_file, secrets=[known_secret])
            disk = Path(log_file).read_text(encoding="utf-8")
            self.assertIn("responsibilities", disk)
            self.assertNotIn(tok, disk)
            self.assertNotIn(spaced_pw, disk)
            self.assertNotIn(known_secret, disk)


# ================================================================ E5: Spec Evaluation & Build Security
class TestE5BuildAndSpec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = _load_build_mod()

    def test_whole_entrypilot_spec_evaluates_without_nameerror_with_stubbed_pyinstaller(self):
        tmp = tempfile.mkdtemp(prefix="ep-r2e-spec-")
        self.addCleanup(shutil.rmtree, tmp, True)
        work_dir = os.path.join(tmp, "work", "app")
        local_pyc = os.path.join(work_dir, "localpycs", "0", "pkg", "ok.pyc")
        stray_pyc = os.path.join(tmp, "stray", "bad.pyc")
        pycache_file = os.path.join(tmp, "ep", "__pycache__", "mod.pyc")

        fake_src_path = os.path.join("C:" + os.sep, "Users", "sampleacct", "work", "ep", "mod.py")
        sample_co = compile("def f():\n    return 42\n", fake_src_path, "exec")
        stub_conf = {"workpath": work_dir, "code_cache": {}}

        class StubAnalysis:
            def __init__(self, scripts, pathex=None, hiddenimports=None, datas=None, excludes=None, noarchive=False):
                self.scripts = [("main", s, "PYSOURCE") for s in scripts]
                self.pure = [("ep.mod", fake_src_path, "PYMODULE")]
                stub_conf["code_cache"][id(self.pure)] = {"ep.mod": sample_co}
                self.binaries = []
                self.datas = [
                    (dst, src, "DATA") for src, dst in (datas or [])
                ] + [
                    (os.path.join("pkg", "ok.pyc"), local_pyc, "DATA"),
                    (os.path.join("ep", "bad.pyc"), stray_pyc, "DATA"),
                    (os.path.join("ep", "__pycache__", "mod.pyc"), pycache_file, "DATA"),
                ]

        class StubPYZ:
            def __init__(self, pure):
                self.pure = list(pure)

        class StubEXE:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        class StubCOLLECT:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        def _replace_co_fn(co, new_fn):
            consts = tuple(_replace_co_fn(c, new_fn) if isinstance(c, types.CodeType) else c for c in co.co_consts)
            return co.replace(co_filename=new_fn, co_consts=consts)

        fake_pyi = types.ModuleType("PyInstaller")
        fake_building = types.ModuleType("PyInstaller.building")
        fake_b_utils = types.ModuleType("PyInstaller.building.utils")
        fake_b_utils.replace_filename_in_code_object = _replace_co_fn
        fake_config = types.ModuleType("PyInstaller.config")
        fake_config.CONF = stub_conf
        fake_utils = types.ModuleType("PyInstaller.utils")
        fake_hooks = types.ModuleType("PyInstaller.utils.hooks")
        fake_hooks.collect_submodules = lambda pkg: [pkg + ".sub"]
        fake_hooks.collect_data_files = lambda pkg: []

        spec_path = os.path.join(ROOT, "build", "entrypilot.spec")
        with open(spec_path, encoding="utf-8") as f:
            spec_src = f.read()

        ns = {
            "__file__": spec_path,
            "SPECPATH": os.path.join(ROOT, "build"),
            "DISTPATH": os.path.join(tmp, "dist"),
            "workpath": work_dir,
            "Analysis": StubAnalysis,
            "PYZ": StubPYZ,
            "EXE": StubEXE,
            "COLLECT": StubCOLLECT,
        }
        with mock.patch.dict(sys.modules, {
            "PyInstaller": fake_pyi,
            "PyInstaller.building": fake_building,
            "PyInstaller.building.utils": fake_b_utils,
            "PyInstaller.config": fake_config,
            "PyInstaller.utils": fake_utils,
            "PyInstaller.utils.hooks": fake_hooks,
        }):
            exec(compile(spec_src, spec_path, "exec"), ns)

        a = ns["a"]
        src_paths = [e[1] for e in a.datas]
        self.assertIn(local_pyc, src_paths, "localpycs under PyInstaller workpath must be kept")
        self.assertNotIn(stray_pyc, src_paths, "stray .pyc outside localpycs must be dropped")
        self.assertNotIn(pycache_file, src_paths, "__pycache__ entries must be dropped")
        rewritten_co = stub_conf["code_cache"][id(a.pure)]["ep.mod"]
        self.assertEqual(rewritten_co.co_filename, os.path.join("ep", "mod.py"))
        self.assertEqual(ns["gui"].kwargs["name"], "EntryPilot")
        self.assertEqual(ns["py"].kwargs["name"], "python")
        self.assertEqual(ns["coll"].kwargs["name"], "EntryPilot")

    def test_stage_uses_mkdtemp_and_refuses_preexisting_stage_folder(self):
        tmp = tempfile.mkdtemp(prefix="ep-r2e-stage-")
        self.addCleanup(shutil.rmtree, tmp, True)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EP_BUILD_STAGE", None)
            sdir = self.build.create_stage_dir()
            self.addCleanup(shutil.rmtree, sdir, True)
            self.assertTrue(os.path.isdir(sdir))
            self.assertNotIn("programdata", sdir.lower())

        # Pre-existing empty folder refused by create_stage_dir and stage_source
        pre = os.path.join(tmp, "already-exists")
        os.makedirs(pre)
        with self.assertRaises(SystemExit):
            self.build.create_stage_dir(pre)
        with mock.patch.dict(os.environ, {"EP_BUILD_STAGE": pre}):
            with self.assertRaises(SystemExit):
                self.build.create_stage_dir()
        with self.assertRaises(SystemExit):
            self.build.stage_source(ROOT, pre)

    def test_verify_clean_bundle_scans_setup_exe_embedded_app_zip_and_portable_folder(self):
        tmp = tempfile.mkdtemp(prefix="ep-r2e-clean-")
        self.addCleanup(shutil.rmtree, tmp, True)
        header = importlib.util.MAGIC_NUMBER + struct.pack("<I", 1) + b"\0" * 8

        def make_app_zip_bytes(py_text="x = 1\n", json_text='{"ok": true}\n', pyc_fn="app/main.py", pyc_const="ok"):
            buf = io.BytesIO()
            co = compile(f"v = {pyc_const!r}\n", pyc_fn, "exec")
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("EntryPilot.exe", b"MZ\x90\x00")
                zf.writestr("_internal/ep/mod.py", py_text)
                zf.writestr("_internal/ui/cfg.json", json_text)
                zf.writestr("_internal/pkg.pyc", header + marshal.dumps(co))
            return buf.getvalue()

        def make_fake_setup_exe(path: str, app_zip_bytes: bytes):
            outer = io.BytesIO()
            with zipfile.ZipFile(outer, "w") as zf:
                zf.writestr("app.zip", app_zip_bytes)
            Path(path).write_bytes(b"MZ\x90\x00stub" + outer.getvalue())

        user_pref = "C:" + os.sep + "Users" + os.sep + "sampleacct"
        user_json = "\\\\".join(["C:", "Users", "sampleacct"])

        # Clean Setup exe passes
        clean_exe = os.path.join(tmp, "EntryPilot-Setup.exe")
        make_fake_setup_exe(clean_exe, make_app_zip_bytes())
        self.build.verify_setup_exe(clean_exe)
        self.build.verify_clean_bundle(clean_exe)

        # Leaked user profile path in .py inside Setup exe's embedded app.zip -> fails
        bad_py_exe = os.path.join(tmp, "BadPy-Setup.exe")
        make_fake_setup_exe(bad_py_exe, make_app_zip_bytes(py_text=f"P = {user_pref + os.sep + 'secret'!r}\n"))
        with self.assertRaises(SystemExit):
            self.build.verify_setup_exe(bad_py_exe)

        # Leaked user profile path in .json inside Setup exe's embedded app.zip -> fails
        bad_json_exe = os.path.join(tmp, "BadJson-Setup.exe")
        make_fake_setup_exe(bad_json_exe, make_app_zip_bytes(json_text='{"root": "' + user_json + '\\\\app"}'))
        with self.assertRaises(SystemExit):
            self.build.verify_setup_exe(bad_json_exe)

        # Leaked user profile string constant inside .pyc in Setup exe's embedded app.zip -> fails
        bad_pyc_exe = os.path.join(tmp, "BadPyc-Setup.exe")
        make_fake_setup_exe(bad_pyc_exe, make_app_zip_bytes(pyc_const=user_pref + os.sep + "leak"))
        with self.assertRaises(SystemExit):
            self.build.verify_setup_exe(bad_pyc_exe)

        # Portable folder scan: clean passes; user profile path in .py, .json, or .pyc fails
        app_src = os.path.join(tmp, "EntryPilot")
        os.makedirs(os.path.join(app_src, "_internal", "ep"), exist_ok=True)
        Path(app_src, "EntryPilot.exe").write_bytes(b"MZ")
        Path(app_src, "_internal", "ep", "clean.py").write_text("a = 1\n", encoding="utf-8")
        Path(app_src, "_internal", "cfg.json").write_text('{"clean": 1}\n', encoding="utf-8")
        clean_co = compile("a = 1\n", "a.py", "exec")
        Path(app_src, "_internal", "mod.pyc").write_bytes(header + marshal.dumps(clean_co))

        dist_dir = os.path.join(tmp, "dist")
        os.makedirs(dist_dir, exist_ok=True)
        portable_dir = self.build.portable(app_src, dist_dir=dist_dir)
        self.build.verify_clean_bundle(portable_dir)

        for rel, content in (
            (os.path.join("_internal", "ep", "leak.py"), f"x = {user_pref + os.sep + 'file'!r}\n".encode("utf-8")),
            (os.path.join("_internal", "leak.json"), ('{"p": "' + user_json + '"}').encode("utf-8")),
            (os.path.join("_internal", "leak.pyc"), header + marshal.dumps(compile(f"s = {user_pref!r}\n", "x.py", "exec"))),
        ):
            fp = os.path.join(portable_dir, rel)
            Path(fp).write_bytes(content)
            try:
                with self.assertRaises(SystemExit, msg=rel):
                    self.build.verify_clean_bundle(portable_dir)
            finally:
                os.remove(fp)

    def test_skip_app_requires_matching_commit_and_file_hashes(self):
        tmp = tempfile.mkdtemp(prefix="ep-r2e-stamp-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake_repo = os.path.join(tmp, "repo")
        dist_dir = os.path.join(tmp, "dist")
        app_dir = os.path.join(dist_dir, "EntryPilot")
        for d in ("ep", "prompts", "cp", "prompts_contest", "ui", "app", "assets", "build"):
            os.makedirs(os.path.join(fake_repo, d), exist_ok=True)
        Path(fake_repo, "ep.py").write_text("x = 1\n", encoding="utf-8")
        Path(fake_repo, "cp.py").write_text("y = 2\n", encoding="utf-8")
        Path(fake_repo, "app", "main.py").write_text("z = 3\n", encoding="utf-8")
        shutil.copy2(os.path.join(ROOT, "build", "entrypilot.spec"), os.path.join(fake_repo, "build", "entrypilot.spec"))
        os.makedirs(app_dir, exist_ok=True)
        Path(app_dir, "EntryPilot.exe").write_bytes(b"MZ-app-v1")

        commit_a = "a" * 40
        commit_b = "b" * 40

        # Missing stamp -> refused
        with mock.patch.object(self.build, "current_commit", return_value=commit_a):
            with self.assertRaises(SystemExit):
                self.build.verify_dist_stamp(fake_repo, dist_dir)

            # Write stamp -> matching commit & hashes accepted
            stamp_path = self.build.write_dist_stamp(fake_repo, dist_dir)
            self.assertTrue(os.path.isfile(stamp_path))
            verified = self.build.verify_dist_stamp(fake_repo, dist_dir)
            self.assertEqual(verified["commit"], commit_a)

        # Different commit -> refused
        with mock.patch.object(self.build, "current_commit", return_value=commit_b):
            with self.assertRaises(SystemExit):
                self.build.verify_dist_stamp(fake_repo, dist_dir)

        # Modified source file on same commit -> refused
        Path(fake_repo, "app", "main.py").write_text("z = 999\n", encoding="utf-8")
        with mock.patch.object(self.build, "current_commit", return_value=commit_a):
            with self.assertRaises(SystemExit):
                self.build.verify_dist_stamp(fake_repo, dist_dir)

        # Restore source, tamper with built dist file -> refused
        Path(fake_repo, "app", "main.py").write_text("z = 3\n", encoding="utf-8")
        Path(app_dir, "EntryPilot.exe").write_bytes(b"MZ-tampered")
        with mock.patch.object(self.build, "current_commit", return_value=commit_a):
            with self.assertRaises(SystemExit):
                self.build.verify_dist_stamp(fake_repo, dist_dir)


# ================================================================ E6: sns_login Cookie Domain Match
class TestE6SnsLoginCookieDomain(unittest.TestCase):
    def test_exact_host_or_dot_boundary_subdomain_only(self):
        future = time.time() + 3600
        past = time.time() - 10
        wanted_x = sns_login.SNS["x"]["cookies"]
        wanted_naver = sns_login.SNS["naver"]["cookies"]
        wanted_ig = sns_login.SNS["instagram"]["cookies"]

        # Lookalike domains that previously matched via endswith must be rejected
        spoof_cookies = [
            {"name": "auth_token", "domain": "evilx.com", "expires": future},
            {"name": "auth_token", "domain": ".evilx.com", "expires": future},
            {"name": "auth_token", "domain": "netflix.com", "expires": future},
            {"name": "auth_token", "domain": "notstwitter.com", "expires": future},
            {"name": "NID_AUT", "domain": "evilnaver.com", "expires": future},
            {"name": "NID_AUT", "domain": ".evilnaver.com", "expires": future},
            {"name": "NID_AUT", "domain": "naver.com.evil.example", "expires": future},
            {"name": "sessionid", "domain": "fakeinstagram.com", "expires": future},
            {"name": "sessionid", "domain": "..instagram.com", "expires": future},
        ]
        self.assertIsNone(sns_login.find_login_cookie(spoof_cookies, wanted_x))
        self.assertIsNone(sns_login.find_login_cookie(spoof_cookies, wanted_naver))
        self.assertIsNone(sns_login.find_login_cookie(spoof_cookies, wanted_ig))

        # Exact host and dot-boundary subdomains succeed; expired cookies fail
        for good_dom in ("x.com", ".x.com", "sub.x.com", ".sub.twitter.com"):
            c = sns_login.find_login_cookie([{"name": "auth_token", "domain": good_dom, "expires": future}], wanted_x)
            self.assertIsNotNone(c, good_dom)
        for good_dom in ("naver.com", ".naver.com", "nid.naver.com", ".nid.naver.com"):
            c = sns_login.find_login_cookie([{"name": "NID_AUT", "domain": good_dom, "expires": -1}], wanted_naver)
            self.assertIsNotNone(c, good_dom)
        self.assertIsNone(sns_login.find_login_cookie(
            [{"name": "NID_AUT", "domain": ".naver.com", "expires": past}], wanted_naver
        ))


if __name__ == "__main__":
    unittest.main()
