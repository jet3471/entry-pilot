"""Shared browser executable validator (Chrome / Edge, including Beta / Dev / Canary).

Enforces:
- Standard Chrome/Edge install path (Stable, Beta, Dev, Canary/SxS) under %ProgramFiles%, %ProgramFiles(x86)%, or %LOCALAPPDATA%
- Local drive path only (no UNC, no device paths, no ':' ADS names, no '..' or trailing dot/space segments)
- No reparse point (symlink / junction / mount point) on the executable or any parent directory
- Valid Windows Authenticode signature with exact certificate subject O and CN equal to "Google LLC" (chrome.exe)
  or "Microsoft Corporation" (msedge.exe), verified offline via WinVerifyTrust, cached per
  (volume_serial, file_id, size, mtime), and re-verified if older than 10 minutes.
"""
import ctypes
from ctypes import wintypes
import os
import stat
import time

SIG_CACHE_TTL_SEC = 600.0

CHROME_REL_PATHS = (
    os.path.join("Google", "Chrome", "Application", "chrome.exe"),
    os.path.join("Google", "Chrome Beta", "Application", "chrome.exe"),
    os.path.join("Google", "Chrome Dev", "Application", "chrome.exe"),
    os.path.join("Google", "Chrome SxS", "Application", "chrome.exe"),
)
EDGE_REL_PATHS = (
    os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join("Microsoft", "Edge Beta", "Application", "msedge.exe"),
    os.path.join("Microsoft", "Edge Dev", "Application", "msedge.exe"),
    os.path.join("Microsoft", "Edge SxS", "Application", "msedge.exe"),
)

ALLOWED_BROWSERS = {
    "chrome.exe": (CHROME_REL_PATHS, "Google LLC"),
    "msedge.exe": (EDGE_REL_PATHS, "Microsoft Corporation"),
}
_ROOT_ENV_VARS = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
_SIG_CACHE: dict[tuple[int, int, int, float], tuple[bool, str, float]] = {}
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pcwszFilePath", wintypes.LPCWSTR),
        ("hFile", wintypes.HANDLE),
        ("pgKnownSubject", ctypes.POINTER(_GUID)),
    ]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD),
        ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
        ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPCWSTR),
        ("dwProvFlags", wintypes.DWORD),
        ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


class _CRYPT_PROVIDER_CERT(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pCert", ctypes.c_void_p),
    ]


_WINTRUST_ACTION_GENERIC_VERIFY_V2 = _GUID(
    0x00AAC56B, 0xCD44, 0x11D0,
    (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
)
_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_REVOCATION_CHECK_NONE = 0x00000010
_WTD_CACHE_ONLY_URL_RETRIEVAL = 0x00001000
_CERT_NAME_RDN_TYPE = 2
_CERT_X500_NAME_STR = 3
_INVALID_HWND = wintypes.HWND(ctypes.c_void_p(-1).value)

_wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

_wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(_WINTRUST_DATA)]
_wintrust.WinVerifyTrust.restype = wintypes.LONG
_wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
_wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
_wintrust.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.c_void_p
_wintrust.WTHelperGetProvCertFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_wintrust.WTHelperGetProvCertFromChain.restype = ctypes.POINTER(_CRYPT_PROVIDER_CERT)

_crypt32.CertGetNameStringW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD,
]
_crypt32.CertGetNameStringW.restype = wintypes.DWORD


def _is_local_drive_path(p: str) -> bool:
    if not isinstance(p, str) or not p or any(c in p for c in ("\x00", "\r", "\n")):
        return False
    if p.startswith(("\\\\", "//")):
        return False
    drive, tail = os.path.splitdrive(p)
    if len(drive) != 2 or not drive[0].isascii() or not drive[0].isalpha() or drive[1] != ":":
        return False
    if not tail.startswith(("\\", "/")) or ":" in tail:
        return False
    parts = [seg for seg in tail.replace("/", "\\").split("\\") if seg]
    if not parts or any(seg in (".", "..") or seg.endswith((".", " ")) for seg in parts):
        return False
    return True


def _is_reparse(p: str) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return True
    if os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _no_reparse_in_chain(p: str) -> bool:
    norm = os.path.normpath(p)
    drive, tail = os.path.splitdrive(norm)
    if len(drive) != 2:
        return False
    cur = drive + "\\"
    if _is_reparse(cur):
        return False
    for seg in (s for s in tail.split("\\") if s):
        cur = os.path.join(cur, seg)
        if _is_reparse(cur):
            return False
    return True


def _win_verify_trust(wtd: "_WINTRUST_DATA") -> int:
    return _wintrust.WinVerifyTrust(_INVALID_HWND, ctypes.byref(_WINTRUST_ACTION_GENERIC_VERIFY_V2), ctypes.byref(wtd))


def _read_authenticode_subject(path: str) -> tuple[bool, str]:
    fi = _WINTRUST_FILE_INFO(ctypes.sizeof(_WINTRUST_FILE_INFO), path, None, None)
    wtd = _WINTRUST_DATA()
    wtd.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
    wtd.dwUIChoice = _WTD_UI_NONE
    wtd.fdwRevocationChecks = _WTD_REVOKE_NONE
    wtd.dwUnionChoice = _WTD_CHOICE_FILE
    wtd.pFile = ctypes.pointer(fi)
    wtd.dwStateAction = _WTD_STATEACTION_VERIFY
    wtd.dwProvFlags = _WTD_CACHE_ONLY_URL_RETRIEVAL | _WTD_REVOCATION_CHECK_NONE
    try:
        status = _win_verify_trust(wtd)
        if status != 0 or not wtd.hWVTStateData:
            return False, ""
        prov = _wintrust.WTHelperProvDataFromStateData(wtd.hWVTStateData)
        sgnr = _wintrust.WTHelperGetProvSignerFromChain(prov, 0, False, 0) if prov else None
        pcert = _wintrust.WTHelperGetProvCertFromChain(sgnr, 0) if sgnr else None
        if not pcert or not pcert.contents.pCert:
            return False, ""
        str_type = wintypes.DWORD(_CERT_X500_NAME_STR)
        cert = pcert.contents.pCert
        needed = _crypt32.CertGetNameStringW(cert, _CERT_NAME_RDN_TYPE, 0, ctypes.byref(str_type), None, 0)
        if needed <= 1:
            return False, ""
        buf = ctypes.create_unicode_buffer(needed)
        _crypt32.CertGetNameStringW(cert, _CERT_NAME_RDN_TYPE, 0, ctypes.byref(str_type), buf, needed)
        return True, buf.value
    except OSError:
        return False, ""
    finally:
        wtd.dwStateAction = _WTD_STATEACTION_CLOSE
        try:
            _win_verify_trust(wtd)
        except OSError:
            pass


_HEX_CHARS = frozenset("0123456789abcdefABCDEF")
_OID_KEY_MAP = {
    "2.5.4.10": "O",
    "OID.2.5.4.10": "O",
    "2.5.4.3": "CN",
    "OID.2.5.4.3": "CN",
}


def _ends_with_odd_backslashes(s: str) -> bool:
    n = 0
    i = len(s) - 1
    while i >= 0 and s[i] == "\\":
        n += 1
        i -= 1
    return (n % 2) == 1


def _split_x500_tokens(subject: str) -> list[str] | None:
    tokens: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    n = len(subject)
    while i < n:
        ch = subject[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and subject[i + 1] == '"':
                    buf.append('""')
                    i += 2
                    continue
                in_quotes = False
                buf.append(ch)
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_quotes = True
            buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= n:
                return None
            buf.append(subject[i:i + 2])
            i += 2
            continue
        if ch in (",", ";", "+"):
            tokens.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if in_quotes:
        return None
    tokens.append("".join(buf))
    return tokens


def _unescape_x500_unquoted(v: str) -> str | None:
    out: list[str] = []
    i = 0
    n = len(v)
    while i < n:
        if v[i] != "\\":
            out.append(v[i])
            i += 1
            continue
        if i + 1 >= n:
            return None
        if i + 2 < n and v[i + 1] in _HEX_CHARS and v[i + 2] in _HEX_CHARS:
            raw_bytes = bytearray()
            while i + 2 < n and v[i] == "\\" and v[i + 1] in _HEX_CHARS and v[i + 2] in _HEX_CHARS:
                raw_bytes.append(int(v[i + 1:i + 3], 16))
                i += 3
            try:
                out.append(raw_bytes.decode("utf-8"))
            except UnicodeDecodeError:
                return None
            continue
        out.append(v[i + 1])
        i += 2
    return "".join(out)


def _parse_x500_rdns(subject: str) -> list[tuple[str, str]] | None:
    if not isinstance(subject, str) or not subject.strip() or "\x00" in subject:
        return None
    tokens = _split_x500_tokens(subject)
    if not tokens:
        return None
    pairs: list[tuple[str, str]] = []
    for raw_tok in tokens:
        tok = raw_tok.lstrip()
        if not tok or "=" not in tok:
            return None
        raw_k, raw_v = tok.split("=", 1)
        key = raw_k.strip().upper()
        if not key:
            return None
        key = _OID_KEY_MAP.get(key, key)
        val_str = raw_v.lstrip()
        while val_str.endswith((" ", "\t")) and not _ends_with_odd_backslashes(val_str[:-1]):
            val_str = val_str[:-1]
        if val_str.startswith('"'):
            if len(val_str) < 2 or not val_str.endswith('"'):
                return None
            inner = val_str[1:-1]
            if '"' in inner.replace('""', ""):
                return None
            val = inner.replace('""', '"')
        else:
            if val_str.startswith("#"):
                return None
            val = _unescape_x500_unquoted(val_str)
            if val is None:
                return None
        pairs.append((key, val))
    return pairs


def _subject_matches_signer(subject: str, expected_signer: str) -> bool:
    if not expected_signer:
        return False
    pairs = _parse_x500_rdns(subject)
    if not pairs:
        return False
    o_vals = [v for k, v in pairs if k == "O"]
    cn_vals = [v for k, v in pairs if k == "CN"]
    return o_vals == [expected_signer] and cn_vals == [expected_signer]


def _cache_key(st: os.stat_result) -> tuple[int, int, int, float] | None:
    if not st.st_dev or not st.st_ino:
        return None
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), st.st_mtime)


def verify_authenticode(path: str, expected_signer: str, max_age_sec: float | None = SIG_CACHE_TTL_SEC) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    now = time.time()
    key = _cache_key(st)
    cached = _SIG_CACHE.get(key) if key is not None else None
    if cached is not None:
        _, _, verified_at = cached
        age = now - verified_at
        if age < 0 or (max_age_sec is not None and age > max_age_sec):
            cached = None
    if cached is None:
        ok, subject = _read_authenticode_subject(path)
        if key is not None:
            if len(_SIG_CACHE) >= 256:
                _SIG_CACHE.clear()
            _SIG_CACHE[key] = (ok, subject, now)
    else:
        ok, subject, _ = cached
    return bool(ok and _subject_matches_signer(subject, expected_signer))


def _allowed_targets(exe_name: str) -> set[str]:
    rel_subpaths, _ = ALLOWED_BROWSERS[exe_name]
    allowed: set[str] = set()
    for env_name in _ROOT_ENV_VARS:
        raw_base = (os.environ.get(env_name) or "").strip()
        if not _is_local_drive_path(raw_base):
            continue
        base_norm = os.path.normpath(raw_base)
        if not os.path.isdir(base_norm) or not _no_reparse_in_chain(base_norm):
            continue
        base_real = os.path.realpath(base_norm)
        if not _is_local_drive_path(base_real) or os.path.splitdrive(base_norm)[0].lower() != os.path.splitdrive(base_real)[0].lower():
            continue
        for rel_subpath in rel_subpaths:
            allowed.add(os.path.normcase(os.path.normpath(os.path.join(base_real, rel_subpath))))
    return allowed


def valid_browser(path: str, max_age_sec: float | None = SIG_CACHE_TTL_SEC) -> bool:
    """Validate that `path` is a genuine, non-reparse, signed Chrome or Edge in a standard install location."""
    if not isinstance(path, str):
        return False
    p = path.strip()
    if not _is_local_drive_path(p):
        return False
    exe_name = os.path.basename(p).lower()
    if exe_name not in ALLOWED_BROWSERS:
        return False
    norm = os.path.normpath(p)
    if not _no_reparse_in_chain(norm):
        return False
    real = os.path.realpath(norm)
    if not _is_local_drive_path(real) or os.path.splitdrive(norm)[0].lower() != os.path.splitdrive(real)[0].lower():
        return False
    if not _no_reparse_in_chain(real):
        return False
    try:
        st = os.lstat(real)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if os.path.normcase(real) not in _allowed_targets(exe_name):
        return False
    _, expected_signer = ALLOWED_BROWSERS[exe_name]
    return verify_authenticode(real, expected_signer, max_age_sec=max_age_sec)


def candidate_browser_paths(extra_first: str = "") -> list[str]:
    cands = [extra_first] if extra_first else []
    for rel in CHROME_REL_PATHS:
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                cands.append(os.path.join(base, rel))
    for rel in EDGE_REL_PATHS:
        for env_name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                cands.append(os.path.join(base, rel))
    return cands


def find_browser(extra_first: str = "") -> str:
    for c in candidate_browser_paths(extra_first):
        if c and valid_browser(c):
            return os.path.normpath(c)
    return ""
