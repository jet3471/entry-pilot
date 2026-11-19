"""URL policy and DNS-pinned HTTP(S) client for outbound browser and crawler requests."""
from __future__ import annotations

import errno
import gzip
import http.client
import ipaddress
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_BROWSER_METHODS = ("GET", "HEAD")
CGNAT_REFUSAL_MSG = "내부망 주소(100.64/10)라 열지 않았습니다. 프록시나 Tail" "scale을 쓰면 끄고 다시 시도하세요"

FORBIDDEN_HOSTS = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "broadcasthost",
    "local",
    "internal",
})
FORBIDDEN_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
)

_IPV4_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_IPV4_BENCHMARK_NET = ipaddress.ip_network("198.18.0.0/15")
_IPV4_PRIVATE_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_IPV6_ULA_NET = ipaddress.ip_network("fc00::/7")
_IPV6_LINK_LOCAL_NET = ipaddress.ip_network("fe80::/10")
_IPV6_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_IPV6_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_IPV6_NAT64_NETS = (
    _IPV6_NAT64_WELL_KNOWN,
    _IPV6_NAT64_LOCAL,
)
_NUMERIC_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)$")

_HOP_BY_HOP_REQUEST_HEADERS = frozenset({
    "host",
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
})
_STRIP_RESPONSE_HEADERS = frozenset({
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "proxy-authenticate",
    "proxy-authorization",
    "trailer",
    "upgrade",
})


class PolicyBlockedError(RuntimeError):
    """Raised when a URL or redirect target violates outbound URL policy."""

    def __init__(self, reason: str, url: str = ""):
        self.reason = reason
        self.url = url
        super().__init__(f"{reason}: {url}" if url else reason)


def _decode_nat64_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Decode RFC 6052 / RFC 8215 NAT64 IPv6 (64:ff9b::/96 and 64:ff9b:1::/48) to embedded IPv4."""
    if ip in _IPV6_NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(ip.packed[12:16])
    if ip in _IPV6_NAT64_LOCAL:
        b = ip.packed
        if b[6:12] == b"\x00" * 6:
            return ipaddress.IPv4Address(b[12:16])
        if b[8] == 0 and b[12:16] == b"\x00" * 4:
            if b[11] == 0:
                return ipaddress.IPv4Address(b[6:8] + b[9:11])
            return ipaddress.IPv4Address(b[7:8] + b[9:12])
        if b[8] == 0 and b[13:16] == b"\x00" * 3 and b[9:12] != b"\x00" * 3:
            return ipaddress.IPv4Address(b[9:13])
        return ipaddress.IPv4Address(b[12:16])
    return None


def _embedded_reason(kind: str, inner: ipaddress.IPv4Address) -> str:
    """Blocked reason of an IPv4 embedded in an IPv6 form, prefixed with `kind` (CGNAT message stays unprefixed)."""
    sub = _ip_blocked_reason(inner)
    if sub and sub != CGNAT_REFUSAL_MSG:
        return f"{kind} {sub}"
    return sub


def _ip_blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _embedded_reason("ipv4-mapped", ip.ipv4_mapped)
        nat64_v4 = _decode_nat64_ipv4(ip)
        if nat64_v4 is not None:
            return _embedded_reason("nat64", nat64_v4)
        if ip.sixtofour is not None:
            sub = _embedded_reason("6to4", ip.sixtofour)
            if sub:
                return sub
        if ip.teredo is not None:
            for part in ip.teredo:
                sub = _embedded_reason("teredo", part)
                if sub:
                    return sub
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local or (isinstance(ip, ipaddress.IPv6Address) and ip in _IPV6_LINK_LOCAL_NET):
        return "link-local address"
    if isinstance(ip, ipaddress.IPv6Address) and ip in _IPV6_ULA_NET:
        return "unique-local address"
    if ip.is_multicast:
        return "multicast address"
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in _IPV4_CGNAT_NET:
            return CGNAT_REFUSAL_MSG
        if ip in _IPV4_BENCHMARK_NET:
            return ""
        if any(ip in net for net in _IPV4_PRIVATE_NETS):
            return "private address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        return "non-global address"
    return ""


def _format_ip_reason(reason: str, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if reason == CGNAT_REFUSAL_MSG:
        return CGNAT_REFUSAL_MSG
    return f"{reason}: {ip}"


def _parse_canonical_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _parse_nonstandard_ipv4(host: str) -> ipaddress.IPv4Address | None:
    parts = host.split(".")
    if 1 <= len(parts) <= 4 and all(_NUMERIC_LABEL_RE.match(p) for p in parts):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def _host_name_blocked(host: str) -> str:
    if host in FORBIDDEN_HOSTS:
        return f"disallowed host: {host}"
    if any(host.endswith(sfx) for sfx in FORBIDDEN_SUFFIXES):
        return f"disallowed host suffix: {host}"
    return ""


def resolve_public_http_url(url: str) -> tuple[bool, str, list[str]]:
    """Validate `url`, resolve once, check every address, and return `(ok, reason, pinned_ips)`."""
    if not isinstance(url, str):
        return False, "url must be a string", []
    s = url.strip()
    if not s:
        return False, "empty url", []
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in s):
        return False, "whitespace or control character in url", []
    try:
        sp = urllib.parse.urlsplit(s)
    except ValueError as e:
        return False, f"invalid url: {e}", []
    scheme = (sp.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return False, f"disallowed scheme: {scheme or 'missing'}", []
    if sp.username is not None or sp.password is not None or "@" in (sp.netloc or ""):
        return False, "userinfo not allowed in url", []
    try:
        port = sp.port
    except ValueError:
        return False, "invalid port", []
    raw_host = sp.hostname
    if not raw_host:
        return False, "missing host", []
    if "%" in raw_host or "%" in (sp.netloc or ""):
        return False, "ipv6 zone id not allowed", []
    norm_host = raw_host.rstrip(".").lower()
    if not norm_host:
        return False, "missing host", []
    reason = _host_name_blocked(norm_host)
    if reason:
        return False, reason, []
    try:
        ascii_host = norm_host.encode("idna").decode("ascii").lower() if not norm_host.isascii() else norm_host
    except UnicodeError:
        return False, "invalid host encoding", []
    reason = _host_name_blocked(ascii_host)
    if reason:
        return False, reason, []
    canon_ip = _parse_canonical_ip(ascii_host)
    if canon_ip is not None:
        reason = _ip_blocked_reason(canon_ip)
        if reason:
            return False, _format_ip_reason(reason, canon_ip), []
        return True, "", [str(canon_ip)]
    alt_v4 = _parse_nonstandard_ipv4(ascii_host)
    if alt_v4 is not None:
        reason = _ip_blocked_reason(alt_v4)
        if reason:
            return False, _format_ip_reason(reason, alt_v4), []
        return False, f"non-standard ipv4 literal not allowed: {ascii_host}", []
    if "." not in ascii_host:
        return False, f"single-label host not allowed: {ascii_host}", []
    try:
        infos = socket.getaddrinfo(ascii_host, port or (443 if scheme == "https" else 80))
    except OSError as e:
        return False, f"dns resolution failed: {e}", []
    if not infos:
        return False, "dns returned no addresses", []
    addrs: list[str] = []
    for info in infos:
        sockaddr = info[4] if len(info) > 4 else None
        addr = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else ""
        if "%" in str(addr):
            addr = str(addr).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, f"unparseable resolved address: {addr!r}", []
        reason = _ip_blocked_reason(ip)
        if reason:
            return False, _format_ip_reason(reason, ip), []
        ip_s = str(ip)
        if ip_s not in addrs:
            addrs.append(ip_s)
    if not addrs:
        return False, "dns returned no usable addresses", []
    return True, "", addrs


def is_public_http_url(url: str) -> tuple[bool, str]:
    """Return `(ok, reason)` for public http/https URLs whose resolved IPs are all allowed."""
    ok, reason, _ = resolve_public_http_url(url)
    return ok, reason


def _connect_pinned_socket(pinned_ips: tuple[str, ...], port: int, timeout, source_address=None) -> socket.socket:
    last_err: OSError | None = None
    for ip_str in pinned_ips:
        ip_obj = ipaddress.ip_address(ip_str)
        af = socket.AF_INET6 if isinstance(ip_obj, ipaddress.IPv6Address) else socket.AF_INET
        sock = socket.socket(af, socket.SOCK_STREAM)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sockaddr = (ip_str, port, 0, 0) if af == socket.AF_INET6 else (ip_str, port)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            last_err = e
            try:
                sock.close()
            except OSError:
                pass
    if last_err is not None:
        raise last_err
    raise OSError("no pinned IP addresses to connect")


def _normalize_conn_host(host: str) -> str:
    if not host.isascii():
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    return host


def _init_pinned(conn, pinned_ips) -> None:
    conn.host = _normalize_conn_host(conn.host)
    conn.pinned_ips = (pinned_ips,) if isinstance(pinned_ips, str) else tuple(pinned_ips)
    if not conn.pinned_ips:
        raise ValueError("pinned_ips must not be empty")


def _open_pinned(conn) -> None:
    sys.audit("http.client.connect", conn, conn.host, conn.port)
    conn.sock = _connect_pinned_socket(conn.pinned_ips, conn.port, conn.timeout, conn.source_address)
    try:
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as e:
        if e.errno != errno.ENOPROTOOPT:
            raise


class PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects directly to pre-validated IP(s) while preserving the original Host header."""

    def __init__(self, host: str, port: int | None = None, *, pinned_ips: tuple[str, ...] | list[str] | str,
                 timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, blocksize: int = 8192):
        super().__init__(host, port=port, timeout=timeout, source_address=source_address, blocksize=blocksize)
        _init_pinned(self, pinned_ips)

    def connect(self):
        _open_pinned(self)
        if self._tunnel_host:
            self._tunnel()


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects directly to pre-validated IP(s) while keeping original Host, SNI, and cert check."""

    def __init__(self, host: str, port: int | None = None, *, pinned_ips: tuple[str, ...] | list[str] | str,
                 context: ssl.SSLContext | None = None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                 source_address=None, blocksize: int = 8192):
        if context is None:
            context = ssl.create_default_context()
        super().__init__(host, port=port, context=context, timeout=timeout,
                         source_address=source_address, blocksize=blocksize)
        _init_pinned(self, pinned_ips)

    def connect(self):
        _open_pinned(self)
        if self._tunnel_host:
            self._tunnel()
            server_hostname = _normalize_conn_host(self._tunnel_host)
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        ips = getattr(req, "_pinned_ips", None)
        if not ips:
            ok, reason, ips = resolve_public_http_url(req.full_url)
            if not ok:
                raise PolicyBlockedError(reason, req.full_url)
        return self.do_open(PinnedHTTPConnection, req, pinned_ips=ips)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        ips = getattr(req, "_pinned_ips", None)
        if not ips:
            ok, reason, ips = resolve_public_http_url(req.full_url)
            if not ok:
                raise PolicyBlockedError(reason, req.full_url)
        return self.do_open(PinnedHTTPSConnection, req, context=self._context, pinned_ips=ips)


def build_pinned_opener(*extra_handlers) -> urllib.request.OpenerDirector:
    """Build a urllib OpenerDirector that ignores environment proxies and always connects to pinned IPs."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        PinnedHTTPHandler,
        PinnedHTTPSHandler,
        *extra_handlers,
    )


def _decompress_body(raw: bytes, encoding_header: str) -> bytes:
    if not raw or not encoding_header:
        return raw
    enc = encoding_header.strip().lower()
    if enc in ("gzip", "x-gzip"):
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if enc == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return raw
    return raw


def _url_host_key(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()


def fetch_pinned(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 20,
    max_redirects: int = 6,
    max_bytes: int = 8_000_000,
    pre_resolved: tuple[str, list[str]] | None = None,
    cookie_provider=None,
) -> tuple[int, dict[str, str], bytes, str]:
    """Resolve & check every hop, fetch via PinnedHTTP(S)Connection, and return `(status, headers, body, final_url)`."""
    method_u = (method or "GET").upper()
    if method_u not in ALLOWED_BROWSER_METHODS:
        raise PolicyBlockedError(f"disallowed method: {method_u}", url)

    cur = url
    start_host = _url_host_key(url)
    first_hop = True

    for _ in range(max_redirects + 1):
        if first_hop and pre_resolved is not None and pre_resolved[0] == cur and pre_resolved[1]:
            pinned_ips = list(pre_resolved[1])
        else:
            ok, reason, pinned_ips = resolve_public_http_url(cur)
            if not ok:
                raise PolicyBlockedError(reason, cur)
        first_hop = False

        sp = urllib.parse.urlsplit(cur)
        scheme = (sp.scheme or "").lower()
        raw_host = (sp.hostname or "").rstrip(".").lower()
        ascii_host = raw_host.encode("idna").decode("ascii").lower() if not raw_host.isascii() else raw_host
        conn_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
        port = sp.port
        target = urllib.parse.urlunsplit(("", "", sp.path or "/", sp.query, ""))

        req_headers: dict[str, str] = {}
        same_host = _url_host_key(cur) == start_host
        if headers:
            for k, v in headers.items():
                if not isinstance(k, str) or v is None:
                    continue
                kl = k.strip().lower()
                if not kl or kl in _HOP_BY_HOP_REQUEST_HEADERS:
                    continue
                if kl == "cookie" and not same_host:
                    continue
                req_headers[k] = str(v)
        if cookie_provider is not None and not any(k.lower() == "cookie" for k in req_headers):
            ck = cookie_provider(cur)
            if ck:
                req_headers["Cookie"] = ck
        if not any(k.lower() == "user-agent" for k in req_headers):
            req_headers["User-Agent"] = "Mozilla/5.0"
        req_headers["Accept-Encoding"] = "gzip, deflate"
        req_headers["Connection"] = "close"

        conn_cls = PinnedHTTPSConnection if scheme == "https" else PinnedHTTPConnection
        conn = conn_cls(conn_host, port=port, pinned_ips=pinned_ips, timeout=timeout)
        try:
            conn.request(method_u, target, headers=req_headers)
            resp = conn.getresponse()
            raw = b"" if method_u == "HEAD" else resp.read(max_bytes)
            status = int(resp.status)
            raw_items = list(resp.getheaders())
            loc = resp.getheader("Location")
            content_enc = resp.getheader("Content-Encoding") or ""
        finally:
            conn.close()

        if status in (301, 302, 303, 307, 308) and loc is not None:
            if not loc.strip():
                raise PolicyBlockedError("empty redirect location", cur)
            cur = urllib.parse.urljoin(cur, loc.strip())
            if status == 303:
                method_u = "GET"
            continue

        body = b"" if method_u == "HEAD" else _decompress_body(raw, content_enc)
        out_headers: dict[str, str] = {}
        for hk, hv in raw_items:
            kl = hk.strip().lower()
            if not kl or kl in _STRIP_RESPONSE_HEADERS:
                continue
            if kl == "set-cookie":
                out_headers[kl] = f"{out_headers[kl]}\n{hv}" if kl in out_headers else str(hv)
            elif kl in out_headers:
                out_headers[kl] = f"{out_headers[kl]}, {hv}"
            else:
                out_headers[kl] = str(hv)
        return status, out_headers, body, cur

    raise PolicyBlockedError("too many redirects", url)
