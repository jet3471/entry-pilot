"""Round 3 security tests for worker E (items E1-E5).
unittest only; no network, no real Chrome, no AI CLI, no real PyInstaller build."""
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
import zipfile
from email.message import Message
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import secure as app_secure  # noqa: E402
from cp import config as cp_config, crawler as cp_crawler, crawler_chrome as cp_crawler_chrome  # noqa: E402
from cp import form_read as cp_form_read, store as cp_store, urlpolicy as cp_urlpolicy  # noqa: E402
from cp.agents import base as cp_base  # noqa: E402
from ep import config as ep_config, crawler as ep_crawler, crawler_chrome as ep_crawler_chrome  # noqa: E402
from ep import form_read as ep_form_read, prompts as ep_prompts, store as ep_store, urlpolicy as ep_urlpolicy  # noqa: E402
from ep.agents import base as ep_base  # noqa: E402

EXPECTED_CGNAT_MSG = "내부망 주소(100.64/10)라 열지 않았습니다. 프록시나 Tail" "scale을 쓰면 끄고 다시 시도하세요"


def _sys32(exe: str) -> str:
    sysroot = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(sysroot, "System32", exe)


def _load_build_mod():
    spec = importlib.util.spec_from_file_location("ep_build_r3e", os.path.join(ROOT, "build", "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _addrinfo_v4(ip: str, port: int = 443):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


def _addrinfo_v6(ip: str, port: int = 443):
    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0))]


class _FakeRawSocket:
    """In-memory socket recording connect sockaddr and outgoing HTTP wire bytes."""

    def __init__(self, af: int, response_bytes: bytes):
        self.af = af
        self.connected_to = None
        self.sent = bytearray()
        self._resp = response_bytes
        self.closed = False
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def setsockopt(self, *a):
        pass

    def bind(self, addr):
        pass

    def connect(self, sockaddr):
        self.connected_to = sockaddr

    def sendall(self, data: bytes):
        self.sent.extend(data)

    def makefile(self, mode="rb", *a, **kw):
        return io.BytesIO(self._resp)

    def close(self):
        self.closed = True


# ================================================================ E1: DNS Rebinding & Browser Pinned Routing
class TestR3E1DnsPinningAndBrowsers(unittest.TestCase):
    def test_pinned_http_and_https_connections_use_checked_ip_original_host_and_sni(self):
        raw_http_resp = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello"
        for mod in (ep_urlpolicy, cp_urlpolicy):
            self.assertTrue(issubclass(mod.PinnedHTTPConnection, http.client.HTTPConnection))
            self.assertTrue(issubclass(mod.PinnedHTTPSConnection, http.client.HTTPSConnection))

            # 1) HTTP: connects to pinned IPv4 without calling getaddrinfo, sends original Host header
            created_socks = []

            def fake_socket_ctor(af, sock_type):
                s = _FakeRawSocket(af, raw_http_resp)
                created_socks.append(s)
                return s

            with mock.patch("socket.getaddrinfo", side_effect=AssertionError("connect must not re-resolve DNS")), \
                    mock.patch("socket.socket", side_effect=fake_socket_ctor):
                conn = mod.PinnedHTTPConnection("events.example.kr", 8080, pinned_ips=["93.184.216.34"], timeout=10)
                conn.request("GET", "/list?page=1")
                resp = conn.getresponse()
                self.assertEqual(resp.read(), b"hello")
                conn.close()
            self.assertEqual(len(created_socks), 1)
            self.assertEqual(created_socks[0].connected_to, ("93.184.216.34", 8080))
            self.assertIn(b"Host: events.example.kr:8080\r\n", bytes(created_socks[0].sent))

            # 2) HTTPS: connects to pinned IPv6 without getaddrinfo, wraps TLS with original hostname SNI & CERT_REQUIRED
            created_socks.clear()
            wrap_calls = []
            ctx = mock.Mock(spec=ssl.SSLContext)

            def fake_wrap(sock, server_hostname=None):
                wrap_calls.append((sock, server_hostname))
                return sock

            ctx.wrap_socket.side_effect = fake_wrap
            with mock.patch("socket.getaddrinfo", side_effect=AssertionError("HTTPS connect must not re-resolve DNS")), \
                    mock.patch("socket.socket", side_effect=fake_socket_ctor):
                sconn = mod.PinnedHTTPSConnection(
                    "secure.example.org", 443,
                    pinned_ips=["2606:2800:220:1:248:1893:25c8:1946"],
                    context=ctx, timeout=10,
                )
                sconn.request("GET", "/form")
                sresp = sconn.getresponse()
                self.assertEqual(sresp.read(), b"hello")
                sconn.close()
            self.assertEqual(created_socks[0].connected_to, ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0))
            self.assertEqual(wrap_calls[0][1], "secure.example.org")
            self.assertIn(b"Host: secure.example.org\r\n", bytes(created_socks[0].sent))

            # Default SSL context enforces certificate verification and hostname check
            default_conn = mod.PinnedHTTPSConnection("secure.example.org", pinned_ips=["93.184.216.34"])
            self.assertTrue(default_conn._context.check_hostname)
            self.assertEqual(default_conn._context.verify_mode, ssl.CERT_REQUIRED)

    def test_crawlers_defeat_dns_rebinding_and_recheck_every_redirect_hop(self):
        for crawler_mod in (ep_crawler, cp_crawler):
            # Rebinding scenario: 1st getaddrinfo returns public IP, 2nd would return 127.0.0.1 if client resolved again
            gai_calls = []

            def rebinding_gai(host, port, *a, **kw):
                gai_calls.append(host)
                if host == "rebind.example.com":
                    return _addrinfo_v4("93.184.216.34") if gai_calls.count(host) == 1 else _addrinfo_v4("127.0.0.1")
                if host == "hop2.example.com":
                    return _addrinfo_v4("93.184.216.35")
                if host == "internal-hop.example.com":
                    return _addrinfo_v4("127.0.0.1")
                raise socket.gaierror("unknown")

            connected_targets = []
            sni_names = []

            def make_sock_ctor(responses_by_ip):
                def ctor(af, sock_type):
                    s = _FakeRawSocket(af, b"")
                    orig_connect = s.connect

                    def on_connect(sockaddr):
                        orig_connect(sockaddr)
                        connected_targets.append(sockaddr[0])
                        s._resp = responses_by_ip[sockaddr[0]]

                    s.connect = on_connect
                    return s
                return ctor

            responses = {
                "93.184.216.34": (
                    b"HTTP/1.1 302 Found\r\nLocation: https://hop2.example.com/final\r\nContent-Length: 0\r\n\r\n"
                ),
                "93.184.216.35": (
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: 9\r\n\r\nsafe-page"
                ),
            }
            fake_ssl_ctx = mock.Mock(spec=ssl.SSLContext)
            fake_ssl_ctx.wrap_socket.side_effect = lambda sock, server_hostname=None: (
                sni_names.append(server_hostname) or sock
            )

            fetcher = (crawler_mod.Fetcher(delay=0, check_robots=False)
                       if crawler_mod is cp_crawler else crawler_mod.Fetcher(delay=0))
            with mock.patch("socket.getaddrinfo", side_effect=rebinding_gai), \
                    mock.patch("socket.socket", side_effect=make_sock_ctor(responses)), \
                    mock.patch("ssl.create_default_context", return_value=fake_ssl_ctx):
                # Reconstruct opener so HTTPSHandler uses fake_ssl_ctx
                fetcher._noredir = (cp_urlpolicy if crawler_mod is cp_crawler else ep_urlpolicy).build_pinned_opener(
                    crawler_mod._NoRedirect
                )
                for h in fetcher._noredir.handlers:
                    if hasattr(h, "_context"):
                        h._context = fake_ssl_ctx
                body = fetcher("https://rebind.example.com/start")

            self.assertEqual(body, "safe-page")
            self.assertEqual(connected_targets, ["93.184.216.34", "93.184.216.35"])
            self.assertNotIn("127.0.0.1", connected_targets)
            self.assertEqual(sni_names, ["rebind.example.com", "hop2.example.com"])

            # Redirect hop to host resolving to 127.0.0.1 is refused before connecting to 127.0.0.1
            connected_targets.clear()
            gai_calls.clear()
            bad_redir_responses = {
                "93.184.216.34": (
                    b"HTTP/1.1 302 Found\r\nLocation: https://internal-hop.example.com/admin\r\nContent-Length: 0\r\n\r\n"
                ),
            }
            with mock.patch("socket.getaddrinfo", side_effect=rebinding_gai), \
                    mock.patch("socket.socket", side_effect=make_sock_ctor(bad_redir_responses)):
                with self.assertRaises(RuntimeError):
                    fetcher("https://rebind.example.com/start")
            self.assertEqual(connected_targets, ["93.184.216.34"])

    def test_form_read_and_crawler_chrome_fulfill_pinned_block_sw_close_popups_and_restrict_methods(self):
        # 1) form_read: service_workers="block", popup closed immediately, route.fulfill used, HEAD allowed, POST aborted
        for fr_mod, up_mod in ((ep_form_read, ep_urlpolicy), (cp_form_read, cp_urlpolicy)):
            popup_page = mock.Mock()
            ctx_handlers = {}
            page_handlers = {}
            route_handlers = []

            page = mock.Mock()
            page.url = "https://public.example.com/survey"
            page.main_frame = page
            page.frames = [page]
            page.content.return_value = "<html><body>Simple question</body></html>"
            page.inner_text.return_value = "Simple question"
            page.eval_on_selector_all.return_value = []
            page.locator.return_value = mock.Mock(count=lambda: 0)
            page.on.side_effect = lambda ev, fn: page_handlers.setdefault(ev, fn)

            def attach_route(pat, fn):
                route_handlers.append(fn)

            page.route.side_effect = attach_route

            def fake_goto(url, **kw):
                req = mock.Mock(url=url, method="GET", headers={"User-Agent": "Test"}, frame=page)
                req.is_navigation_request = lambda: True
                rt = mock.Mock(request=req)
                route_handlers[0](rt)
                rt.fulfill.assert_called_once()
                rt.continue_.assert_not_called()
                # Trigger popup via context "page" event during page load
                if "page" in ctx_handlers:
                    ctx_handlers["page"](popup_page)

            page.goto.side_effect = fake_goto

            ctx = mock.Mock()
            ctx.new_page.return_value = page
            ctx.cookies.return_value = []
            ctx.on.side_effect = lambda ev, fn: ctx_handlers.setdefault(ev, fn)

            browser = mock.Mock()
            browser.new_context.return_value = ctx

            @contextlib.contextmanager
            def pw_fac():
                pw = mock.Mock()
                pw.chromium.launch.return_value = browser
                yield pw

            fake_conn = mock.MagicMock()
            fake_conn.getresponse.return_value = mock.Mock(
                status=200,
                read=lambda n=-1: b"<html><body>Simple question</body></html>",
                getheaders=lambda: [("Content-Type", "text/html; charset=utf-8")],
                getheader=lambda name, default=None: {"Content-Type": "text/html; charset=utf-8"}.get(name, default),
            )

            with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4("93.184.216.34")), \
                    mock.patch.object(up_mod, "PinnedHTTPSConnection", return_value=fake_conn):
                res = fr_mod.read_form("https://public.example.com/survey", pw_factory=pw_fac)
                self.assertTrue(res["ok"])
                browser.new_context.assert_called_once_with(service_workers="block")
                popup_page.close.assert_called_once()

                # HEAD is allowed and fulfilled; POST/PUT/DELETE are aborted
                r_head = mock.Mock(request=mock.Mock(
                    url="https://public.example.com/ping", method="HEAD", headers={},
                    is_navigation_request=lambda: False, frame=page,
                ))
                route_handlers[0](r_head)
                r_head.fulfill.assert_called_once()
                for bad_m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
                    r_bad = mock.Mock(request=mock.Mock(
                        url="https://public.example.com/submit", method=bad_m, headers={},
                        is_navigation_request=lambda: False, frame=page,
                    ))
                    route_handlers[0](r_bad)
                    r_bad.abort.assert_called_once()

        # 2) crawler_chrome: _open_guarded_page blocks service workers, closes popups, allows only GET/HEAD, fulfills
        for cc_mod, up_mod in ((ep_crawler_chrome, ep_urlpolicy), (cp_crawler_chrome, cp_urlpolicy)):
            ctx_events = {}
            pg_events = {}
            routes = []
            pg = mock.Mock()
            pg.on.side_effect = lambda ev, fn: pg_events.setdefault(ev, fn)
            pg.route.side_effect = lambda pat, fn: routes.append(fn)

            ctx = mock.Mock()
            ctx.new_page.return_value = pg
            ctx.on.side_effect = lambda ev, fn: ctx_events.setdefault(ev, fn)
            default_ctx = mock.Mock()
            default_ctx.cookies.return_value = [{"name": "NID_AUT", "value": "tok", "domain": ".naver.com", "path": "/"}]

            b = mock.Mock()
            b.contexts = [default_ctx]
            b.new_context.return_value = ctx

            fake_conn = mock.MagicMock()
            fake_conn.getresponse.return_value = mock.Mock(
                status=200,
                read=lambda n=-1: b"ok",
                getheaders=lambda: [("Content-Type", "text/html")],
                getheader=lambda name, default=None: {"Content-Type": "text/html"}.get(name, default),
            )

            with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4("93.184.216.34")), \
                    mock.patch.object(up_mod, "PinnedHTTPSConnection", return_value=fake_conn):
                with cc_mod._open_guarded_page(b) as opened_pg:
                    self.assertIs(opened_pg, pg)
                    b.new_context.assert_called_once_with(service_workers="block")
                    ctx.add_cookies.assert_called_once()

                    pop1 = mock.Mock()
                    ctx_events["page"](pop1)
                    pop1.close.assert_called_once()

                    pop2 = mock.Mock()
                    pg_events["popup"](pop2)
                    pop2.close.assert_called_once()

                    for ok_m in ("GET", "HEAD"):
                        r_ok = mock.Mock(request=mock.Mock(url="https://public.example.com/a", method=ok_m, headers={}))
                        routes[0](r_ok)
                        r_ok.fulfill.assert_called_once()
                    for bad_m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
                        r_no = mock.Mock(request=mock.Mock(url="https://public.example.com/a", method=bad_m, headers={}))
                        routes[0](r_no)
                        r_no.abort.assert_called_once()


# ================================================================ E2: Legitimate Networks (NAT64, 198.18/15, 100.64/10, IPv6)
class TestR3E2LegitimateNetworks(unittest.TestCase):
    def test_nat64_decoded_and_judged_by_embedded_ipv4(self):
        for mod in (ep_urlpolicy, cp_urlpolicy):
            # Well-known 64:ff9b::/96 and local-use 64:ff9b:1::/48 embedding public IPv4 -> allowed
            for nat64_pub in (
                "64:ff9b::93.184.216.34",
                "64:ff9b:1::93.184.216.34",
                "64:ff9b:1:fffe::93.184.216.34",
                "64:ff9b:1:5db8:00d8:2200::",      # RFC 6052 /48
                "64:ff9b:1:0:005d:b8d8:2200:0",    # RFC 6052 /64
                "64:ff9b::198.18.0.5",             # NAT64 + fake-ip 198.18/15
            ):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v6(nat64_pub)):
                    ok, reason, ips = mod.resolve_public_http_url("https://events.kr/event.php?id=10")
                    self.assertTrue(ok, f"{nat64_pub} failed: {reason}")
                    self.assertEqual(reason, "")
                    self.assertTrue(ips)

            # NAT64 embedding private/loopback/metadata IPv4 -> refused
            for nat64_priv in (
                "64:ff9b::127.0.0.1",
                "64:ff9b::10.0.0.1",
                "64:ff9b::169.254.169.254",
                "64:ff9b:1::192.168.1.1",
                "64:ff9b:1:7f00:0000:0100::",      # RFC 6052 /48 127.0.0.1
                "64:ff9b:1:0:007f:0000:0100:0",    # RFC 6052 /64 127.0.0.1
            ):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v6(nat64_priv)):
                    ok, reason = mod.is_public_http_url("https://rebind.example.com/")
                    self.assertFalse(ok, f"{nat64_priv} must be blocked")
                    self.assertTrue(reason)

    def test_benchmark_198_18_allowed_cgnat_100_64_blocked_with_korean_message_and_ipv6_public_passes(self):
        for mod in (ep_urlpolicy, cp_urlpolicy):
            # 198.18.0.0/15 (Clash / sing-box fake-ip) -> allowed
            for fake_ip in ("198.18.0.1", "198.18.0.0", "198.19.12.34", "198.19.255.254"):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4(fake_ip)):
                    ok, reason = mod.is_public_http_url("https://forms.gle/abc")
                    self.assertTrue(ok, f"{fake_ip}: {reason}")
                    self.assertEqual(reason, "")
            with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v6("::ffff:198.18.0.2")):
                ok, reason = mod.is_public_http_url("https://forms.gle/abc")
                self.assertTrue(ok, reason)

            # 100.64.0.0/10 -> blocked with exact Korean message
            for cgnat_ip in ("100.64.0.1", "100.100.100.100", "100.127.255.254"):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4(cgnat_ip)):
                    ok, reason = mod.is_public_http_url("https://peer.example.com/")
                    self.assertFalse(ok)
                    self.assertEqual(reason, EXPECTED_CGNAT_MSG)
                ok_lit, reason_lit = mod.is_public_http_url(f"http://{cgnat_ip}/status")
                self.assertFalse(ok_lit)
                self.assertEqual(reason_lit, EXPECTED_CGNAT_MSG)
            for wrapped_cgnat in ("::ffff:100.64.0.1", "64:ff9b::100.64.1.2"):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v6(wrapped_cgnat)):
                    ok, reason = mod.is_public_http_url("https://peer.example.com/")
                    self.assertFalse(ok)
                    self.assertEqual(reason, EXPECTED_CGNAT_MSG)

            # Public IPv6 addresses (DNS AAAA and literal URL) -> pass
            for pub_v6 in (
                "2606:2800:220:1:248:1893:25c8:1946",
                "2001:4860:4860::8888",
                "2606:4700:4700::1111",
                "2404:6800:4004:800::200e",
            ):
                with mock.patch("socket.getaddrinfo", return_value=_addrinfo_v6(pub_v6)):
                    ok, reason = mod.is_public_http_url("https://ipv6.example.org/entry")
                    self.assertTrue(ok, f"{pub_v6}: {reason}")
                ok_lit, reason_lit = mod.is_public_http_url(f"https://[{pub_v6}]/entry")
                self.assertTrue(ok_lit, f"literal [{pub_v6}]: {reason_lit}")


# ================================================================ E3: form_read Anchor & Button Type Restrictions
class _R3FakeEl:
    def __init__(self, text: str, tag: str = "BUTTON", raw_type: str = "button", prop_type: str = "button",
                 in_anchor: bool = False, form_method: str = "", formmethod: str = "", href: str = "", on_click=None):
        self._text = text
        self._tag = tag.upper()
        self._raw_type = raw_type
        self._prop_type = prop_type
        self._in_anchor = in_anchor
        self._form_method = form_method
        self._formmethod = formmethod
        self._href = href
        self._on_click = on_click
        self.clicked = 0

    def is_visible(self):
        return True

    def input_value(self):
        return self._text

    def inner_text(self):
        return self._text

    def evaluate(self, js):
        if "toUpperCase" in js and "href" not in js and "submit-input" not in js:
            return self._tag
        if "submit-input" in js:
            if self._tag == "A" or self._in_anchor:
                return "anchor-link"
            if self._tag == "INPUT" and (self._raw_type in ("submit", "image") or self._prop_type in ("submit", "image")):
                return "submit-input"
            if self._tag not in ("BUTTON", "INPUT"):
                return "non-button-element"
            if self._raw_type.lower() != "button" or self._prop_type.lower() != "button":
                return "submit-button" if self._tag == "BUTTON" else "non-button-input"
            if self._formmethod and self._formmethod.lower() != "get":
                return "formmethod-" + self._formmethod.lower()
            if self._form_method and self._form_method.lower() != "get":
                return "post-form"
            return ""
        if "href:" in js:
            return {"tag": self._tag, "href": self._href, "act": ""}
        return ""

    def click(self, timeout=5000):
        self.clicked += 1
        if self._on_click:
            self._on_click()


class _R3FakePage:
    def __init__(self, url: str, steps: list[dict]):
        self.url = url
        self._steps = steps
        self._idx = 0
        self._route_handler = None
        self.main_frame = self
        self.frames = [self]
        self.context = mock.Mock(cookies=lambda urls=None: [])

    def on(self, event, fn):
        pass

    def route(self, pattern, handler):
        self._route_handler = handler

    def goto(self, url, **kw):
        req = mock.Mock(url=url, method="GET", headers={}, frame=self, is_navigation_request=lambda: True)
        rt = mock.Mock(request=req)
        self._route_handler(rt)
        if rt.abort.called:
            raise RuntimeError("ERR_ABORTED")
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state, timeout=8000):
        pass

    def content(self):
        return f"<html><body>{self._steps[self._idx]['text']}</body></html>"

    def inner_text(self, sel):
        return self._steps[self._idx]["text"]

    def eval_on_selector_all(self, selector, js):
        if "aria-required" in js:
            return list(self._steps[self._idx].get("inputs", []))
        return list(self._steps[self._idx].get("buttons", []))

    def locator(self, selector):
        els = list(self._steps[self._idx].get("els", []))
        return mock.Mock(count=lambda: len(els), nth=lambda i: els[i])


def _r3_pw_factory(pg: _R3FakePage):
    @contextlib.contextmanager
    def fac():
        ctx = mock.Mock()
        ctx.new_page.return_value = pg
        ctx.cookies.return_value = []
        b = mock.Mock()
        b.new_context.return_value = ctx
        pw = mock.Mock()
        pw.chromium.launch.return_value = b
        yield pw
    return fac


class TestR3E3FormReadNextButtonPolicy(unittest.TestCase):
    def setUp(self):
        p = mock.patch("socket.getaddrinfo", return_value=_addrinfo_v4("93.184.216.34"))
        p.start()
        self.addCleanup(p.stop)

        def _fake_conn(*a, **kw):
            c = mock.MagicMock()
            c.getresponse.return_value = mock.Mock(
                status=200,
                read=lambda n=-1: b"<html><body>ok</body></html>",
                getheaders=lambda: [("Content-Type", "text/html")],
                getheader=lambda name, default=None: {"Content-Type": "text/html"}.get(name, default),
            )
            return c

        for mod in (ep_urlpolicy, cp_urlpolicy):
            pc = mock.patch.object(mod, "PinnedHTTPSConnection", side_effect=_fake_conn)
            pc.start()
            self.addCleanup(pc.stop)

    def test_never_clicks_anchors_default_buttons_role_buttons_or_post_form_buttons(self):
        for fr in (ep_form_read, cp_form_read):
            # Verify UNSAFE_NEXT_JS source checks anchor, button type, and post-form
            self.assertIn("anchor-link", fr.UNSAFE_NEXT_JS)
            self.assertIn("post-form", fr.UNSAFE_NEXT_JS)

            unsafe_controls = [
                ("anchor <a href>", _R3FakeEl("다음", tag="A", raw_type="", prop_type="", href="https://public.example.com/step2")),
                ("anchor role=button", _R3FakeEl("시작하기", tag="A", raw_type="button", prop_type="", href="/confirm?tok=1")),
                ("button inside <a>", _R3FakeEl("다음", tag="BUTTON", raw_type="button", prop_type="button", in_anchor=True)),
                ("button without type (default submit)", _R3FakeEl("다음", tag="BUTTON", raw_type="", prop_type="submit")),
                ("button type=submit", _R3FakeEl("다음", tag="BUTTON", raw_type="submit", prop_type="submit")),
                ("div role=button", _R3FakeEl("다음", tag="DIV", raw_type="", prop_type="")),
                ("button type=button in POST form", _R3FakeEl("다음", tag="BUTTON", raw_type="button", prop_type="button", form_method="post")),
                ("button type=button formmethod=post", _R3FakeEl("다음", tag="BUTTON", raw_type="button", prop_type="button", formmethod="post")),
            ]
            for desc, ctrl in unsafe_controls:
                pg = _R3FakePage("https://public.example.com/q1", [{
                    "text": "1. 설문 문항입니다.",
                    "inputs": [],
                    "buttons": ["다음"],
                    "els": [ctrl],
                }])
                res = fr.read_form("https://public.example.com/q1", pw_factory=_r3_pw_factory(pg))
                self.assertEqual(ctrl.clicked, 0, f"must never click {desc}")
                self.assertEqual(res["stopped"], "needs-submit", desc)
                self.assertTrue(res["needs_submit"], desc)
                self.assertTrue(res.get("manual_review"), desc)

            # Safe <button type="button"> outside POST form (or in GET form) advances normally
            for fm in ("", "get"):
                pg_holder = []
                safe_btn = _R3FakeEl(
                    "다음", tag="BUTTON", raw_type="button", prop_type="button", form_method=fm,
                    on_click=lambda: setattr(pg_holder[0], "_idx", 1),
                )
                pg_ok = _R3FakePage("https://public.example.com/q1", [
                    {"text": "Step 1", "inputs": [], "buttons": ["다음"], "els": [safe_btn]},
                    {"text": "Step 2", "inputs": [], "buttons": [], "els": []},
                ])
                pg_holder.append(pg_ok)
                res_ok = fr.read_form("https://public.example.com/q1", pw_factory=_r3_pw_factory(pg_ok))
                self.assertEqual(safe_btn.clicked, 1)
                self.assertEqual(res_ok["page_count"], 2)
                self.assertEqual(res_ok["stopped"], "no-next")
                self.assertFalse(res_ok["needs_submit"])

        # Prompt budgets and updated needs-submit guidance stay intact
        rep = ep_prompts.file_report()
        self.assertLessEqual(rep["verify.md"][0], rep["verify.md"][1])
        self.assertLessEqual(rep["playbooks/nownsurvey.md"][0], rep["playbooks/nownsurvey.md"][1])


# ================================================================ E4: Task Files (Random, O_EXCL, ACL, Hard Links)
class _R3EpAdapter(ep_base.Adapter):
    name = "codex"

    def pick_account(self, db):
        return "acct"

    def command(self, prompt, kind):
        return [sys.executable, "-c", "pass"]


class _R3CpAdapter(cp_base.Adapter):
    name = "codex"

    def pick_account(self, db):
        return "acct"

    def command(self, prompt, kind):
        return [sys.executable, "-c", "pass"]


class TestR3E4TaskFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3e-tasks-")
        self._env = dict(os.environ)
        os.environ["EP_APP_DIR"] = self.tmp
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "data", "board")
        os.environ["CP_APP_DIR"] = self.tmp
        os.environ["CP_DATA_DIR"] = os.path.join(self.tmp, "data", "contest")
        os.environ["CP_OWNER_DIR"] = os.path.join(self.tmp, "data", "contest", "board")
        os.makedirs(os.environ["EP_DATA_DIR"], exist_ok=True)
        os.makedirs(os.environ["CP_DATA_DIR"], exist_ok=True)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_random_exclusive_task_files_acl_protection_fallback_and_hardlink_refusal(self):
        for bmod, cfg_mod, st_mod, ad_cls, iid in (
            (ep_base, ep_config, ep_store, _R3EpAdapter, "E41"),
            (cp_base, cp_config, cp_store, _R3CpAdapter, "C41"),
        ):
            # 1) Tasks folder protected with app.secure.restrict_acl and random token_hex filename used
            with mock.patch.object(app_secure, "restrict_acl", wraps=app_secure.restrict_acl) as macl:
                instr1, path1 = bmod._task_file_with_path("codex", "submit", iid, "prompt-one")
                instr2, path2 = bmod._task_file_with_path("codex", "submit", iid, "prompt-two")
            tasks_dir = os.path.join(cfg_mod.data_dir(), "tasks")
            self.assertGreaterEqual(macl.call_count, 1)
            self.assertEqual(os.path.normcase(macl.call_args[0][0]), os.path.normcase(tasks_dir))
            self.assertNotEqual(path1, path2)
            self.assertRegex(os.path.basename(path1), rf"^codex-submit-{iid}-[0-9a-f]{{32}}\.md$")
            self.assertIn(path1, instr1)
            self.assertEqual(Path(path1).read_text(encoding="utf-8"), "prompt-one")
            self.assertEqual(Path(path2).read_text(encoding="utf-8"), "prompt-two")
            bmod.cleanup_task_file(path1)
            bmod.cleanup_task_file(path2)

            # 2) Pre-existing target file is refused via O_CREAT|O_EXCL without overwriting existing content
            fixed_tok = "a1b2c3d4" * 4
            with mock.patch.object(bmod.secrets, "token_hex", return_value=fixed_tok):
                _, created = bmod._task_file_with_path("codex", "verify", iid, "original-keep")
                try:
                    with self.assertRaises(bmod.LaunchError):
                        bmod._task_file_with_path("codex", "verify", iid, "attacker-overwrite")
                    self.assertEqual(Path(created).read_text(encoding="utf-8"), "original-keep")
                finally:
                    bmod.cleanup_task_file(created)

            # 3) Hard-linked target (st_nlink > 1) is refused before writing prompt content
            real_fstat = os.fstat

            def fake_fstat_hardlinked(fd):
                st = real_fstat(fd)
                m = mock.Mock(wraps=st)
                m.st_nlink = 2
                return m

            with mock.patch("os.fstat", side_effect=fake_fstat_hardlinked):
                with self.assertRaises(bmod.LaunchError) as hl_ctx:
                    bmod._task_file_with_path("codex", "submit", iid, "TOP-SECRET-PROMPT")
                self.assertIn("hard link", str(hl_ctx.exception).lower())

            # Real NTFS hard link on agent-guide.md is also refused
            guide_path = os.path.join(tasks_dir, "agent-guide.md")
            hardlink_copy = os.path.join(self.tmp, f"hl-{iid}.md")
            os.link(guide_path, hardlink_copy)
            try:
                self.assertGreater(os.stat(guide_path).st_nlink, 1)
                with self.assertRaises(bmod.LaunchError):
                    bmod._task_file_with_path("codex", "submit", iid, "SECRET-2")
            finally:
                os.remove(hardlink_copy)

            # 4) Silent fallback when `app` / `app.secure` is not importable, while keeping reparse refusal
            with mock.patch.dict(sys.modules, {"app": None, "app.secure": None}):
                _, p_noapp = bmod._task_file_with_path("codex", "submit", iid, "standalone-engine-ok")
                self.assertTrue(os.path.isfile(p_noapp))
                bmod.cleanup_task_file(p_noapp)

                # Junction on tasks subdir still refused even without app.secure
                outside = os.path.join(self.tmp, f"outside-{iid}")
                os.makedirs(outside, exist_ok=True)
                shutil.rmtree(tasks_dir)
                subprocess.run([_sys32("cmd.exe"), "/d", "/c", "mklink", "/J", tasks_dir, outside],
                               capture_output=True, text=True, encoding="mbcs", errors="replace", check=True)
                try:
                    db = st_mod.connect()
                    try:
                        with self.assertRaises(bmod.LaunchError):
                            bmod.launch(ad_cls(), "submit", {"id": iid}, "SECRET", db, account="acct")
                    finally:
                        db.close()
                finally:
                    os.rmdir(tasks_dir)


# ================================================================ E5: Build Security, Manifest, No-.git ID, Script Stripping
class TestR3E5BuildSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = _load_build_mod()

    def test_ep_build_stage_requires_nonexistent_creates_and_protects_with_icacls_sid_mbcs(self):
        tmp = tempfile.mkdtemp(prefix="ep-r3e-stage-")
        self.addCleanup(shutil.rmtree, tmp, True)

        # Pre-existing folder refused
        existing = os.path.join(tmp, "existing")
        os.makedirs(existing)
        with mock.patch.dict(os.environ, {"EP_BUILD_STAGE": existing}):
            with self.assertRaises(SystemExit):
                self.build.create_stage_dir()

        # Non-existent EP_BUILD_STAGE created and ACL-protected via System32\icacls.exe + SID + mbcs
        fresh = os.path.join(tmp, "fresh-stage")
        real_run = subprocess.run
        icacls_calls = []

        def spy_run(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and "icacls" in str(cmd[0]).lower():
                icacls_calls.append((list(cmd), dict(kw)))
            return real_run(cmd, *a, **kw)

        with mock.patch.dict(os.environ, {"EP_BUILD_STAGE": fresh}), \
                mock.patch("subprocess.run", side_effect=spy_run):
            got = self.build.create_stage_dir()

        self.assertEqual(os.path.normcase(got), os.path.normcase(fresh))
        self.assertTrue(os.path.isdir(fresh))
        self.assertGreaterEqual(len(icacls_calls), 1)
        cmd0, kw0 = icacls_calls[0]
        self.assertEqual(os.path.normcase(cmd0[0]), os.path.normcase(_sys32("icacls.exe")))
        self.assertEqual(kw0.get("encoding"), "mbcs")
        self.assertIn("/inheritance:r", cmd0)
        self.assertTrue(any(arg.startswith("*S-1-") and arg.endswith(":(OI)(CI)F") for arg in cmd0), cmd0)

        # Verify actual NTFS ACL has no Everyone / BUILTIN\Users / inherited ACEs
        chk = subprocess.run([_sys32("icacls.exe"), fresh], capture_output=True, text=True, encoding="mbcs", errors="replace")
        self.assertEqual(chk.returncode, 0)
        aces = app_secure._parse_icacls_aces(chk.stdout, fresh)
        self.assertTrue(app_secure._ace_is_sole_user_full(aces, app_secure.current_sid()), aces)

    def test_source_checkout_without_dot_git_uses_deterministic_source_hash(self):
        tmp = tempfile.mkdtemp(prefix="ep-r3e-nogit-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = os.path.join(tmp, "EntryPilot-main")
        dist_dir = os.path.join(repo, "build", "dist")
        app_dir = os.path.join(dist_dir, "EntryPilot")
        for d in ("ep", "prompts", "cp", "prompts_contest", "ui", "app", "assets", "build"):
            os.makedirs(os.path.join(repo, d), exist_ok=True)
        Path(repo, "ep.py").write_text("x = 1\n", encoding="utf-8")
        Path(repo, "cp.py").write_text("y = 2\n", encoding="utf-8")
        Path(repo, "app", "main.py").write_text("z = 3\n", encoding="utf-8")
        shutil.copy2(os.path.join(ROOT, "build", "entrypilot.spec"), os.path.join(repo, "build", "entrypilot.spec"))

        self.assertFalse(os.path.exists(os.path.join(repo, ".git")))
        bid1 = self.build.current_commit(repo)
        bid2 = self.build.current_commit(repo)
        self.assertEqual(bid1, bid2)
        self.assertRegex(bid1, r"^[0-9a-f]{64}$")

        # Stamp writing and verification succeed without .git and ignore build/dist outputs
        os.makedirs(app_dir, exist_ok=True)
        Path(app_dir, "EntryPilot.exe").write_bytes(b"MZ-app")
        self.build.write_dist_stamp(repo, dist_dir)
        verified = self.build.verify_dist_stamp(repo, dist_dir)
        self.assertEqual(verified["commit"], bid1)

        # Changing any source file content or file list changes the build id
        Path(repo, "app", "main.py").write_text("z = 4\n", encoding="utf-8")
        bid3 = self.build.current_commit(repo)
        self.assertNotEqual(bid3, bid1)
        with self.assertRaises(SystemExit):
            self.build.verify_dist_stamp(repo, dist_dir)

    def test_co_filename_rewritten_for_a_scripts_main_py_and_setup_main_py(self):
        tmp = tempfile.mkdtemp(prefix="ep-r3e-scripts-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake_user_root = os.path.join(tmp, "Users", "samplebuilder", "proj")
        os.makedirs(os.path.join(fake_user_root, "app"), exist_ok=True)
        os.makedirs(os.path.join(fake_user_root, "build"), exist_ok=True)
        Path(fake_user_root, "app", "main.py").write_text("def entry():\n    return 'hi'\n", encoding="utf-8")
        Path(fake_user_root, "app", "setup_main.py").write_text("def setup_entry():\n    return 'ok'\n", encoding="utf-8")
        shutil.copy2(os.path.join(ROOT, "build", "entrypilot.spec"), os.path.join(fake_user_root, "build", "entrypilot.spec"))

        def _replace_co_fn(co, new_fn):
            consts = tuple(_replace_co_fn(c, new_fn) if isinstance(c, types.CodeType) else c for c in co.co_consts)
            return co.replace(co_filename=new_fn, co_consts=consts)

        def eval_spec(spec_path: str, work_dir: str):
            stub_conf = {"workpath": work_dir, "code_cache": {}}

            class StubAnalysis:
                def __init__(self, scripts, **kwargs):
                    self.scripts = [(os.path.splitext(os.path.basename(s))[0], s, "PYSOURCE") for s in scripts]
                    self.pure = []
                    self.binaries = []
                    self.datas = []

            fake_pyi = types.ModuleType("PyInstaller")
            fake_building = types.ModuleType("PyInstaller.building")
            fake_b_utils = types.ModuleType("PyInstaller.building.utils")
            fake_b_utils.replace_filename_in_code_object = _replace_co_fn
            fake_config = types.ModuleType("PyInstaller.config")
            fake_config.CONF = stub_conf
            fake_utils = types.ModuleType("PyInstaller.utils")
            fake_hooks = types.ModuleType("PyInstaller.utils.hooks")
            fake_hooks.collect_submodules = lambda pkg: []
            fake_hooks.collect_data_files = lambda pkg: []

            ns = {
                "__file__": spec_path,
                "SPECPATH": os.path.dirname(spec_path),
                "workpath": work_dir,
                "Analysis": StubAnalysis,
                "PYZ": lambda pure: pure,
                "EXE": lambda *a, **k: (a, k),
                "COLLECT": lambda *a, **k: (a, k),
            }
            with mock.patch.dict(sys.modules, {
                "PyInstaller": fake_pyi,
                "PyInstaller.building": fake_building,
                "PyInstaller.building.utils": fake_b_utils,
                "PyInstaller.config": fake_config,
                "PyInstaller.utils": fake_utils,
                "PyInstaller.utils.hooks": fake_hooks,
            }):
                exec(compile(Path(spec_path).read_text(encoding="utf-8"), spec_path, "exec"), ns)
            return ns["a"]

        # 1) entrypilot.spec -> main.py stripped
        a_main = eval_spec(os.path.join(fake_user_root, "build", "entrypilot.spec"), os.path.join(tmp, "w_app"))
        self.assertEqual(len(a_main.scripts), 1)
        dest_m, pyc_m, _ = a_main.scripts[0]
        self.assertEqual(dest_m, "main")
        co_m = marshal.loads(Path(pyc_m).read_bytes()[16:])
        for fn in self.build._code_filenames(co_m):
            self.assertEqual(fn, "main.py")
            self.assertFalse(self.build._has_local_user_path(fn))

        # 2) EntryPilot-Setup.spec -> setup_main.py stripped
        setup_spec = self.build.write_setup_spec(
            os.path.join(tmp, "setup_spec"),
            fake_user_root,
            os.path.join(tmp, "app.zip"),
            os.path.join(tmp, "icon.ico"),
        )
        a_setup = eval_spec(setup_spec, os.path.join(tmp, "w_setup"))
        self.assertEqual(len(a_setup.scripts), 1)
        dest_s, pyc_s, _ = a_setup.scripts[0]
        self.assertEqual(dest_s, "setup_main")
        co_s = marshal.loads(Path(pyc_s).read_bytes()[16:])
        for fn in self.build._code_filenames(co_s):
            self.assertEqual(fn, "setup_main.py")
            self.assertFalse(self.build._has_local_user_path(fn))

    def test_scans_entrypilot_exe_setup_exe_and_app_zip_for_c_users_and_profile_path_and_writes_manifest(self):
        tmp = tempfile.mkdtemp(prefix="ep-r3e-bundle-")
        self.addCleanup(shutil.rmtree, tmp, True)

        app_dir = os.path.join(tmp, "EntryPilot")
        os.makedirs(os.path.join(app_dir, "_internal", "ep"), exist_ok=True)
        Path(app_dir, "EntryPilot.exe").write_bytes(b"MZ\x90\x00clean-app-exe")
        Path(app_dir, "python.exe").write_bytes(b"MZ\x90\x00clean-py-exe")
        Path(app_dir, "_internal", "ep", "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

        # write_app_zip writes manifest.json (relative path -> sha256 for every file of app_dir)
        zpath = os.path.join(tmp, "app.zip")
        manifest = self.build.write_app_zip(app_dir, zpath)
        verified_manifest = self.build.verify_app_zip_manifest(zpath)
        self.assertEqual(manifest, verified_manifest)
        self.assertEqual(set(manifest.keys()), {"EntryPilot.exe", "python.exe", "_internal/ep/mod.py"})
        for rel, digest in manifest.items():
            full = os.path.join(app_dir, *rel.split("/"))
            self.assertEqual(digest, hashlib.sha256(Path(full).read_bytes()).hexdigest())

        # Tampering with a file inside app.zip fails verify_app_zip_manifest
        tampered_zip = os.path.join(tmp, "tampered.zip")
        with zipfile.ZipFile(zpath, "r") as zin, zipfile.ZipFile(tampered_zip, "w") as zout:
            for info in zin.infolist():
                data = b"MZ-TAMPERED" if info.filename == "EntryPilot.exe" else zin.read(info.filename)
                zout.writestr(info, data)
        with self.assertRaises(SystemExit):
            self.build.verify_app_zip_manifest(tampered_zip)

        # Clean app_dir, app.zip, and Setup.exe pass
        self.build.verify_clean_bundle(app_dir)
        self.build.verify_clean_bundle(zpath)
        clean_setup = os.path.join(tmp, "EntryPilot-Setup.exe")
        outer_buf = io.BytesIO()
        with zipfile.ZipFile(outer_buf, "w") as zf:
            zf.writestr("app.zip", Path(zpath).read_bytes())
        Path(clean_setup).write_bytes(b"MZ\x90\x00setup-clean-stub" + outer_buf.getvalue())
        self.build.verify_setup_exe(clean_setup)

        # 1) Windows Users prefix inside EntryPilot.exe bytes fails verify_clean_bundle on both app_dir and app.zip
        c_users_bytes = b"MZ\x90\x00" + ("C:" + os.sep + "Users" + os.sep + "someone").encode("ascii")
        Path(app_dir, "EntryPilot.exe").write_bytes(c_users_bytes)
        with self.assertRaises(SystemExit):
            self.build.verify_clean_bundle(app_dir)
        bad_zip = os.path.join(tmp, "bad-app.zip")
        self.build.write_app_zip(app_dir, bad_zip)
        with self.assertRaises(SystemExit):
            self.build.verify_clean_bundle(bad_zip)
        Path(app_dir, "EntryPilot.exe").write_bytes(b"MZ\x90\x00clean-app-exe")

        # 2) Custom drive build user's profile path (e.g. D:\CustomProfiles\buildacct) in Setup.exe outer bytes or UTF-16LE fails
        custom_prof = r"D:\CustomProfiles\buildacct"
        with mock.patch.dict(os.environ, {"USERPROFILE": custom_prof}):
            bad_setup_ascii = os.path.join(tmp, "BadSetupAscii.exe")
            Path(bad_setup_ascii).write_bytes(
                b"MZ\x90\x00" + custom_prof.encode("utf-8") + outer_buf.getvalue()
            )
            with self.assertRaises(SystemExit):
                self.build.verify_setup_exe(bad_setup_ascii)

            bad_setup_u16 = os.path.join(tmp, "BadSetupUtf16.exe")
            Path(bad_setup_u16).write_bytes(
                b"MZ\x90\x00" + custom_prof.encode("utf-16le") + outer_buf.getvalue()
            )
            with self.assertRaises(SystemExit):
                self.build.verify_setup_exe(bad_setup_u16)


if __name__ == "__main__":
    unittest.main()
