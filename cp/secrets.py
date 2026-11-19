"""Let agents type login passwords for entries without ever seeing the values.

The user stores KEY=value in secrets.env; on engine initialisation or use of this tool each value
is moved into secrets-auto.env encrypted with Windows DPAPI (only this Windows user can decrypt it) and the line in
secrets.env is emptied, so no password stays on disk in plain text. Agents never read either file (their runs deny
it); they only call this tool.
This tool never prints values; it types them directly into an input element of the entry Chrome (CDP).

  python -m cp.secrets list
      key names and whether each is set (never values or lengths)
  python -m cp.secrets get KEY
      whether one key is set (never the value)
  python -m cp.secrets fill KEY --selector "#pw" --url-contains nid.naver.com [--clear]
      --url-contains is required (https URL or login domain); https is required on both tab and frame,
      the frame containing the element must pass host_ok, and typing goes only into the target input element.
  python -m cp.secrets set KEY --domain example.co.kr [--generate 16]
      store a new encrypted secret in secrets-auto.env for keys that do not exist yet (custom keys require --domain).
  python -m cp.secrets bind KEY --domain example.co.kr
      refused on CLI for existing secrets; domain binding of existing secrets is done by the user in the app settings.
"""
import argparse
import base64
import contextlib
import ctypes
import datetime as dt
import json
import msvcrt
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from urllib.parse import urlparse

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FILE_PERSISTENT_ACLS = 0x00000008
TOKEN_QUERY = 0x0008
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SE_DACL_PRESENT = 0x0004
SE_DACL_PROTECTED = 0x1000
ACL_REVISION = 2
ACL_SIZE_INFORMATION_CLASS = 2
ACCESS_ALLOWED_ACE_TYPE = 0
OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02
INHERITED_ACE = 0x10
FILE_ALL_ACCESS = 0x001F01FF

ACL_WARNINGS: list[str] = []
_ACL_WARNED: set[str] = set()
UNDECRYPTABLE_WARNINGS: list[str] = []
_UNDECRYPTABLE_WARNED: set[tuple[str, str]] = set()
_warn_lock = threading.Lock()


class _AclSizeInfo(ctypes.Structure):
    _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD), ("AclBytesFree", wintypes.DWORD)]


class _AceHeader(ctypes.Structure):
    _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE), ("AceSize", wintypes.WORD)]


class _AccessAllowedAceHead(ctypes.Structure):
    _fields_ = [("Header", _AceHeader), ("Mask", wintypes.DWORD)]


def _data_dir() -> str:
    try:
        from cp import config
        return config.data_dir()
    except Exception:
        return os.environ.get("CP_DATA_DIR") or (
            (os.environ.get("CP_APP_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EntryPilot"))
            + r"\data\contest"
        )


DATA_DIR = _data_dir()
SECRETS = os.path.join(DATA_DIR, "secrets.env")
AUTO = os.path.join(DATA_DIR, "secrets-auto.env")
_INIT_SECRETS = SECRETS
_INIT_AUTO = AUTO
CDP = "http://127.0.0.1:9223"

ENC = "dpapi:"
DOMAIN_TAG = "\x00domain="
NOTE_LINE = "# Values typed here are moved, encrypted, into secrets-auto.env the next time they are used."
SITE_DOMAINS = {
    "NAVER_": ("nid.naver.com",),
    "KAKAO_": ("accounts.kakao.com", "kauth.kakao.com", "logins.kakao.com"),
    "GOOGLE_": ("accounts.google.com",),
    "GMAIL_": ("accounts.google.com",),
    "YOUTUBE_": ("accounts.google.com",),
    "INSTAGRAM_": ("www.instagram.com", "instagram.com"),
    "INSTA_": ("www.instagram.com", "instagram.com"),
    "X_": ("x.com", "twitter.com", "api.x.com"),
    "TWITTER_": ("x.com", "twitter.com", "api.x.com"),
}
KNOWN_2LEVEL_SUFFIXES = frozenset({
    "co.kr", "or.kr", "go.kr", "ac.kr", "ne.kr", "re.kr", "pe.kr",
    "ms.kr", "hs.kr", "es.kr", "sc.kr", "kg.kr", "mil.kr",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "ed.jp", "gr.jp", "ad.jp", "lg.jp",
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "ltd.uk", "plc.uk",
    "co.nz", "org.nz", "net.nz", "govt.nz", "ac.nz",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
    "com.tw", "org.tw", "gov.tw", "edu.tw", "net.tw",
    "com.hk", "org.hk", "gov.hk", "edu.hk", "net.hk",
    "com.sg", "org.sg", "gov.sg", "edu.sg", "net.sg",
    "com.br", "org.br", "net.br", "gov.br", "edu.br",
    "co.in", "org.in", "net.in", "gov.in", "ac.in",
    "co.za", "org.za", "gov.za", "ac.za", "net.za",
})
_SECOND_LEVEL_CATS = frozenset({
    "co", "com", "or", "org", "go", "gov", "govt", "ac", "edu", "ne", "net", "re", "pe", "mil",
})
_USER_KEY_SUFFIXES = ("_ID", "_EMAIL", "_USER", "_USERNAME", "_LOGIN")
_PW_ATTR_TOKENS = ("pw", "pass", "password")
_PW_AUTOCOMPLETE = frozenset({"current-password", "new-password"})
_BROAD_PRINCIPALS = frozenset({
    "everyone", "s-1-1-0", "users", "builtin\\users", "s-1-5-32-545",
    "authenticated users", "nt authority\\authenticated users", "s-1-5-11",
    "nt authority\\system", "s-1-5-18", "builtin\\administrators", "s-1-5-32-544",
})
_mem_lock = threading.Lock()


def _secrets_path() -> str:
    return SECRETS if SECRETS != _INIT_SECRETS else os.path.join(_data_dir(), "secrets.env")


def _auto_path() -> str:
    return AUTO if AUTO != _INIT_AUTO else os.path.join(_data_dir(), "secrets-auto.env")


def _system32_exe(name: str) -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", name)


def _current_user() -> str:
    buf = ctypes.create_unicode_buffer(257)
    n = wintypes.DWORD(len(buf))
    if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(n)) and buf.value:
        return buf.value
    return os.environ.get("USERNAME") or ""


def _bind_win32_acl_apis(adv, k32) -> None:
    """Set argtypes/restype on the advapi32/kernel32 functions used here (callers pass the live ctypes.windll DLLs)."""
    _vp, _pvp, _dw, _b, _i = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD, wintypes.BOOL, ctypes.c_int
    _pdw, _wstr, _cwstr = ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.LPCWSTR
    for lib, name, argtypes, restype in (
        (adv, "OpenProcessToken", [wintypes.HANDLE, _dw, ctypes.POINTER(wintypes.HANDLE)], _b),
        (adv, "GetTokenInformation", [wintypes.HANDLE, _i, _vp, _dw, _pdw], _b),
        (adv, "ConvertSidToStringSidW", [_vp, _pvp], _b),
        (adv, "ConvertStringSidToSidW", [_cwstr, _pvp], _b),
        (adv, "LookupAccountSidW", [_cwstr, _vp, _wstr, _pdw, _wstr, _pdw, ctypes.POINTER(_i)], _b),
        (adv, "IsValidSid", [_vp], _b),
        (adv, "GetLengthSid", [_vp], _dw),
        (adv, "InitializeAcl", [_vp, _dw, _dw], _b),
        (adv, "AddAccessAllowedAceEx", [_vp, _dw, _dw, _dw, _vp], _b),
        (adv, "SetNamedSecurityInfoW", [_wstr, _i, _dw, _vp, _vp, _vp, _vp], _dw),
        (adv, "GetNamedSecurityInfoW", [_cwstr, _i, _dw, _vp, _vp, _pvp, _vp, _pvp], _dw),
        (adv, "GetSecurityDescriptorControl", [_vp, ctypes.POINTER(wintypes.WORD), _pdw], _b),
        (adv, "GetAclInformation", [_vp, _vp, _dw, _i], _b),
        (adv, "GetAce", [_vp, _dw, _pvp], _b),
        (adv, "EqualSid", [_vp, _vp], _b),
        (k32, "GetCurrentProcess", None, wintypes.HANDLE),
        (k32, "CloseHandle", [wintypes.HANDLE], None),
        (k32, "LocalFree", [_vp], None),
        (k32, "GetVolumePathNameW", [_cwstr, _wstr, _dw], _b),
        (k32, "GetVolumeInformationW", [_cwstr, _wstr, _dw, _pdw, _pdw, _pdw, _wstr, _dw], _b),
    ):
        fn = getattr(lib, name)
        if argtypes is not None:
            fn.argtypes = argtypes
        if restype is not None:
            fn.restype = restype


def current_sid() -> str:
    """Return the current process user SID (S-1-5-...) via Win32 token APIs, with System32\\whoami.exe fallback."""
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    _bind_win32_acl_apis(adv, k32)

    tok = wintypes.HANDLE()
    if adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(tok)):
        try:
            needed = wintypes.DWORD(0)
            adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(needed))
            if needed.value:
                buf = ctypes.create_string_buffer(needed.value)
                if adv.GetTokenInformation(tok, 1, buf, needed.value, ctypes.byref(needed)):
                    psid = ctypes.c_void_p.from_buffer(buf).value
                    sptr = ctypes.c_void_p()
                    if psid and adv.ConvertSidToStringSidW(psid, ctypes.byref(sptr)):
                        try:
                            sid = ctypes.wstring_at(sptr)
                            if sid:
                                return sid
                        finally:
                            k32.LocalFree(sptr)
        finally:
            k32.CloseHandle(tok)

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run([_system32_exe("whoami.exe"), "/user", "/fo", "csv", "/nh"],
                           capture_output=True, text=True, encoding="mbcs", errors="replace",
                           creationflags=NO_WINDOW, timeout=15)
        if r.returncode == 0:
            for part in (r.stdout or "").strip().split(","):
                val = part.strip().strip('"')
                if val.upper().startswith("S-1-"):
                    return val
    return ""


def _sid_account_names(sid: str) -> tuple[str, str]:
    if not sid:
        return "", ""
    adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
    _bind_win32_acl_apis(adv, k32)

    psid = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        return "", ""
    try:
        cch_name = wintypes.DWORD(256)
        cch_dom = wintypes.DWORD(256)
        name_buf = ctypes.create_unicode_buffer(cch_name.value)
        dom_buf = ctypes.create_unicode_buffer(cch_dom.value)
        use = ctypes.c_int(0)
        if adv.LookupAccountSidW(None, psid, name_buf, ctypes.byref(cch_name),
                                 dom_buf, ctypes.byref(cch_dom), ctypes.byref(use)):
            return name_buf.value, dom_buf.value
    finally:
        k32.LocalFree(psid)
    return "", ""


def volume_supports_acls(path: str) -> bool:
    """Return False when the volume lacks FILE_PERSISTENT_ACLS (e.g. FAT/FAT32/exFAT); True otherwise."""
    k32 = ctypes.windll.kernel32
    _bind_win32_acl_apis(ctypes.windll.advapi32, k32)

    absp = os.path.abspath(path)
    probe = absp if os.path.exists(absp) else (os.path.dirname(absp) or absp)
    vbuf = ctypes.create_unicode_buffer(1024)
    vol_root = ""
    if k32.GetVolumePathNameW(probe, vbuf, len(vbuf)) and vbuf.value:
        vol_root = vbuf.value
    else:
        drive, _ = os.path.splitdrive(absp)
        if drive:
            vol_root = drive + "\\"
    if not vol_root:
        return True
    flags = wintypes.DWORD(0)
    if not k32.GetVolumeInformationW(vol_root, None, 0, None, None, ctypes.byref(flags), None, 0):
        return True
    return bool(flags.value & FILE_PERSISTENT_ACLS)


def _log_warning_line(msg: str):
    with contextlib.suppress(Exception):
        ldir = os.path.join(_data_dir(), "logs")
        os.makedirs(ldir, exist_ok=True)
        with open(os.path.join(ldir, "scheduler.log"), "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%m-%d %H:%M:%S} {msg}\n")


def _warn_no_acl(path: str):
    folder = os.path.dirname(os.path.abspath(path)) or os.path.abspath(path)
    key = os.path.normcase(folder)
    with _warn_lock:
        if key in _ACL_WARNED:
            return
        _ACL_WARNED.add(key)
        msg = f"WARNING: folder {folder} does not support persistent ACLs (FAT/exFAT); files cannot be ACL-protected"
        ACL_WARNINGS.append(msg)
    _log_warning_line(msg)


def _warn_undecryptable_once(bad_items: list[tuple[str, str]]):
    if not bad_items:
        return
    newly_reported: list[str] = []
    with _warn_lock:
        for src_path, key_name in bad_items:
            token = (os.path.normcase(os.path.abspath(src_path)), key_name)
            if token not in _UNDECRYPTABLE_WARNED:
                _UNDECRYPTABLE_WARNED.add(token)
                newly_reported.append(key_name)
        if not newly_reported:
            return
        names = ", ".join(sorted(set(newly_reported)))
        msg = f"WARNING: cannot decrypt dpapi value for: {names} (kept as-is)"
        UNDECRYPTABLE_WARNINGS.append(msg)
    _log_warning_line(msg)


def _parse_icacls_aces(stdout: str, path: str) -> list[str]:
    aces = []
    for i, raw in enumerate((stdout or "").splitlines()):
        if not raw.strip():
            break
        if i == 0:
            cand = ""
            for prefix in (path, os.path.abspath(path), os.path.normpath(path)):
                if prefix and raw[:len(prefix)].lower() == prefix.lower() and (
                    len(raw) == len(prefix) or raw[len(prefix)] == " "
                ):
                    cand = raw[len(prefix):].strip()
                    break
            if not cand and ":" in raw:
                _, _, after = raw.partition(" ")
                cand = after.strip()
            if cand and ":" in cand:
                aces.append(cand)
        else:
            s = raw.strip()
            if ":" in s:
                aces.append(s)
    return aces


def _mbcs_norm(s: str) -> str:
    return (s or "").encode("mbcs", errors="replace").decode("mbcs", errors="replace").lower()


def _ace_is_sole_user_full(aces: list[str], sid: str) -> bool:
    if len(aces) != 1:
        return False
    principal, sep, perms = aces[0].rpartition(":")
    if sep != ":" or not principal.strip():
        return False
    pu = perms.upper()
    if "(F)" not in pu or "(I)" in pu or "(N)" in pu or "DENY" in pu:
        return False
    pl = principal.strip().lstrip("*").lower()
    if pl in _BROAD_PRINCIPALS:
        return False
    if sid and sid.lower() in pl:
        return True
    acct, dom = _sid_account_names(sid)
    acct_m, dom_m = _mbcs_norm(acct), _mbcs_norm(dom)
    if acct_m and (pl == acct_m or (dom_m and pl == f"{dom_m}\\{acct_m}") or pl.endswith("\\" + acct_m)):
        return True
    user_m = _mbcs_norm(_current_user())
    if user_m and (pl == user_m or pl.endswith("\\" + user_m)):
        return True
    return False


def _set_and_verify_protected_dacl(path: str, sid: str) -> None:
    adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
    _bind_win32_acl_apis(adv, k32)

    absp = os.path.abspath(path)
    if not os.path.exists(absp):
        raise OSError(f"path does not exist for ACL restriction: {path}")
    is_dir = os.path.isdir(absp)

    psid = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(sid, ctypes.byref(psid)) or not psid.value:
        raise OSError(f"ConvertStringSidToSidW failed for {sid}")
    try:
        if not adv.IsValidSid(psid):
            raise OSError(f"invalid SID for {path}: {sid}")
        sid_len = adv.GetLengthSid(psid)
        if not sid_len:
            raise OSError(f"GetLengthSid failed for {path}")
        acl_size = (16 + int(sid_len) + 15) & ~3
        acl_buf = ctypes.create_string_buffer(acl_size)
        if not adv.InitializeAcl(acl_buf, acl_size, ACL_REVISION):
            raise OSError(f"InitializeAcl failed for {path} ({ctypes.GetLastError()})")
        ace_flags = (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE) if is_dir else 0
        if not adv.AddAccessAllowedAceEx(acl_buf, ACL_REVISION, ace_flags, FILE_ALL_ACCESS, psid):
            raise OSError(f"AddAccessAllowedAceEx failed for {path} ({ctypes.GetLastError()})")

        name_buf = ctypes.create_unicode_buffer(absp)
        rc = adv.SetNamedSecurityInfoW(
            name_buf,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl_buf, None,
        )
        if rc != 0:
            raise OSError(rc, f"SetNamedSecurityInfoW failed for {path} (error {rc})")

        pdacl = ctypes.c_void_p()
        psd = ctypes.c_void_p()
        rc_v = adv.GetNamedSecurityInfoW(
            absp, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(pdacl), None, ctypes.byref(psd),
        )
        if rc_v != 0:
            raise OSError(rc_v, f"GetNamedSecurityInfoW verify failed for {path} (error {rc_v})")
        try:
            ctrl = wintypes.WORD(0)
            rev = wintypes.DWORD(0)
            if not adv.GetSecurityDescriptorControl(psd, ctypes.byref(ctrl), ctypes.byref(rev)):
                raise OSError(f"GetSecurityDescriptorControl failed for {path}")
            req_ctrl = SE_DACL_PRESENT | SE_DACL_PROTECTED
            if (ctrl.value & req_ctrl) != req_ctrl or not pdacl.value:
                raise OSError(f"DACL is not protected or missing on {path} (control=0x{ctrl.value:04x})")
            ai = _AclSizeInfo()
            if not adv.GetAclInformation(pdacl, ctypes.byref(ai), ctypes.sizeof(ai), ACL_SIZE_INFORMATION_CLASS):
                raise OSError(f"GetAclInformation failed for {path}")
            if ai.AceCount != 1:
                raise OSError(f"DACL verification failed for {path}: expected 1 ACE, got {ai.AceCount}")
            pace = ctypes.c_void_p()
            if not adv.GetAce(pdacl, 0, ctypes.byref(pace)) or not pace.value:
                raise OSError(f"GetAce failed for {path}")
            head = _AccessAllowedAceHead.from_address(pace.value)
            if head.Header.AceType != ACCESS_ALLOWED_ACE_TYPE or (head.Header.AceFlags & INHERITED_ACE) != 0:
                raise OSError(f"unexpected ACE type/flags on {path}")
            if is_dir and (head.Header.AceFlags & ace_flags) != ace_flags:
                raise OSError(f"directory inheritance flags missing on {path}")
            if (head.Mask & FILE_ALL_ACCESS) != FILE_ALL_ACCESS:
                raise OSError(f"unexpected ACE mask 0x{head.Mask:08x} on {path}")
            ace_sid = ctypes.c_void_p(pace.value + ctypes.sizeof(_AccessAllowedAceHead))
            if not adv.EqualSid(ace_sid, psid):
                raise OSError(f"ACE SID does not match current user SID on {path}")
        finally:
            if psd.value:
                k32.LocalFree(psd)
    finally:
        k32.LocalFree(psid)


def restrict_acl(path: str) -> bool:
    """Apply a protected user-only DACL for current user SID via Win32 SetNamedSecurityInfoW and verify in-process.
    On volumes without persistent ACLs (FAT/exFAT), record a one-time warning and return False without failing."""
    if not volume_supports_acls(path):
        _warn_no_acl(path)
        return False
    sid = current_sid()
    if not sid or not sid.upper().startswith("S-1-"):
        raise OSError(f"cannot determine Windows user SID for ACL on {path}")
    _set_and_verify_protected_dacl(path, sid)
    return True


@contextlib.contextmanager
def _secrets_lock(wait_sec: float = 30.0):
    lp = _auto_path() + ".lock"
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    with _mem_lock:
        t0 = time.monotonic()
        fd = -1
        locked = False
        try:
            while True:
                try:
                    if fd < 0:
                        fd = os.open(lp, os.O_CREAT | os.O_RDWR)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() - t0 > wait_sec:
                        raise OSError(f"secrets lock timed out: {lp}")
                    time.sleep(0.02)
            yield
        finally:
            if fd >= 0:
                if locked:
                    with contextlib.suppress(OSError):
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                with contextlib.suppress(OSError):
                    os.close(fd)


def _write_atomic(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        restrict_acl(tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _dpapi(data: bytes, protect_mode: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData for the current Windows user. Raises OSError on failure."""
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect_mode else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst))
    if not ok:
        raise OSError(f"DPAPI {'protect' if protect_mode else 'unprotect'} failed ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _is_ascii_alnum(c: str) -> bool:
    return ("a" <= c <= "z") or ("0" <= c <= "9")


def _validate_ascii_host(host: str) -> str:
    """Validate an ASCII domain name (punycode xn-- required for IDN; no IP literals or single-label hosts).
    Returns '' when valid, else an error message."""
    if not host or len(host) > 253 or not host.isascii():
        return "host must be ASCII (use punycode xn-- for IDN)"
    if host.startswith(".") or host.endswith(".") or ".." in host:
        return "invalid host syntax"
    h = host.lower()
    labels = h.split(".")
    if len(labels) < 2:
        return "single-label hosts are not allowed"
    for lbl in labels:
        if not lbl or len(lbl) > 63:
            return "invalid host label length"
        if lbl.startswith("-") or lbl.endswith("-"):
            return "host label cannot start or end with '-'"
        if not all(_is_ascii_alnum(c) or c == "-" for c in lbl):
            return "host must contain only ASCII letters, digits, and '-'"
        if lbl.startswith("xn--") and len(lbl) < 5:
            return "invalid punycode label"
    tld = labels[-1]
    if all("0" <= c <= "9" for c in tld):
        return "host must be a domain name, not an IP address"
    if not ((len(tld) >= 2 and all("a" <= c <= "z" for c in tld)) or (tld.startswith("xn--") and len(tld) >= 5)):
        return "invalid top-level domain"
    return ""


def _parse_host_port(raw_hp: str) -> tuple[str, int, str]:
    """Parse an ASCII 'host' or 'host:port' authority into (lower_host, port_or_443, err)."""
    s = (raw_hp or "").strip()
    if not s or not s.isascii():
        return "", 0, "ASCII host is required (use punycode xn-- for IDN)"
    if any(c in s for c in "/?#@\\ \t\r\n*[]"):
        return "", 0, "invalid host authority"
    if ":" in s:
        if s.count(":") > 1:
            return "", 0, "IPv6 literals and multiple colons are not allowed"
        h_part, p_part = s.split(":", 1)
        if not p_part or not all("0" <= c <= "9" for c in p_part):
            return "", 0, "invalid port"
        port = int(p_part)
        if port < 1 or port > 65535:
            return "", 0, "port out of range"
    else:
        h_part, port = s, 443
    herr = _validate_ascii_host(h_part)
    if herr:
        return "", 0, herr
    return h_part.lower(), port, ""


def _is_two_level_suffix(sfx2: str) -> bool:
    if sfx2 in KNOWN_2LEVEL_SUFFIXES:
        return True
    parts = sfx2.split(".")
    return (
        len(parts) == 2
        and len(parts[1]) == 2
        and all("a" <= c <= "z" for c in parts[1])
        and parts[0] in _SECOND_LEVEL_CATS
    )


def _is_public_suffix(host: str) -> bool:
    labels = host.lower().split(".")
    if len(labels) < 2:
        return True
    if len(labels) == 2 and _is_two_level_suffix(f"{labels[0]}.{labels[1]}"):
        return True
    return False


def check_bound_domain(domain: str) -> tuple[str, str]:
    """Validate an exact ASCII https login host[:port] for custom domain binding.
    Bare registrable domains (e.g. example.co.kr) are allowed; bare public suffixes (e.g. co.kr, com) are refused.
    Returns (normalized_authority, error_message) where default port 443 is normalized to bare host."""
    raw = (domain or "").strip()
    if not raw:
        return "", "--domain is required (exact ASCII https login host, e.g. example.co.kr)"
    if not raw.isascii() or any(c.isspace() or ord(c) < 32 for c in raw):
        return "", "--domain must be an ASCII host (give IDN as punycode xn--)"
    if "://" in raw:
        p = urlparse(raw)
        if p.scheme.lower() != "https":
            return "", "--domain URL must use https"
        if p.username or p.password or "@" in (p.netloc or "") or "\\" in (p.netloc or ""):
            return "", "invalid --domain"
        h, port, perr = _parse_host_port(p.netloc or "")
        if perr:
            return "", f"--domain invalid: {perr}"
    else:
        if any(c in raw for c in "/?#@\\*"):
            return "", "--domain must be an exact host such as example.co.kr"
        h, port, perr = _parse_host_port(raw)
        if perr:
            return "", f"--domain invalid: {perr}"
    if _is_public_suffix(h):
        return "", f"--domain {h} is a bare public suffix; give the exact login host (e.g. example.{h})"
    norm = h if port == 443 else f"{h}:{port}"
    return norm, ""


def _norm_domain(domain: str) -> str:
    d = (domain or "").strip()
    if not d:
        return ""
    checked, err = check_bound_domain(d)
    if not err:
        return checked
    if "://" in d:
        p = urlparse(d)
        h = (p.hostname or "").lower().strip(".")
        try:
            port = p.port or 443
        except ValueError:
            port = 443
        return h if port == 443 else f"{h}:{port}"
    d_low = d.lower().strip(".")
    if d_low.endswith(":443"):
        d_low = d_low[:-4]
    return d_low


def protect(value: str, domain: str = "") -> str:
    dom = _norm_domain(domain)
    raw = f"{DOMAIN_TAG}{dom}\n{value}" if dom else value
    return ENC + base64.b64encode(_dpapi(raw.encode("utf-8"), True)).decode("ascii")


def unpack(stored: str) -> tuple[str, str]:
    """Return (plaintext_value, bound_domain)."""
    if not stored:
        return "", ""
    if not stored.startswith(ENC):
        return stored, ""
    raw_bytes = base64.b64decode(stored[len(ENC):], validate=True)
    if not raw_bytes:
        raise ValueError("empty dpapi payload")
    raw = _dpapi(raw_bytes, False).decode("utf-8")
    if raw.startswith(DOMAIN_TAG) and "\n" in raw:
        head, val = raw.split("\n", 1)
        return val, head[len(DOMAIN_TAG):]
    return raw, ""


def unprotect(stored: str) -> str:
    if not stored.startswith(ENC):
        return stored
    return unpack(stored)[0]


def known_site_domains(key: str) -> tuple[str, ...] | None:
    ku = (key or "").upper()
    for prefix, domains in SITE_DOMAINS.items():
        if ku.startswith(prefix):
            return domains
    return None


def _unbound_refusal(key: str, detail: str = "") -> str:
    prefix = f"{key} {detail}" if detail else f"{key} has no bound domain"
    return f"{prefix}; ask the user to bind it in the app settings instead of guessing (manual --need login)"


def host_ok(key: str, host: str, wanted: str, bound_domain: str = "", port: int | None = None) -> str:
    """'' when the value of key may be typed on (host, port) for --url-contains wanted, else the reason."""
    h_name, parsed_port, herr = _parse_host_port(host or "")
    if herr:
        return herr
    if port is not None:
        if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
            return "invalid URL port"
        h_port = port
    else:
        h_port = parsed_port

    w = (wanted or "").strip()
    if not w or not w.isascii() or any(c.isspace() or ord(c) < 32 for c in w):
        return "--url-contains must use an ASCII host or https URL (punycode xn-- for IDN)"
    if "://" in w:
        wp = urlparse(w)
        if wp.scheme.lower() != "https":
            return "--url-contains must use https"
        if wp.username or wp.password or "@" in (wp.netloc or "") or "\\" in (wp.netloc or ""):
            return "--url-contains userinfo is not allowed"
        w_name, w_port, werr = _parse_host_port(wp.netloc or "")
        if werr:
            return f"--url-contains invalid: {werr}"
        if _is_public_suffix(w_name):
            return "--url-contains cannot be a bare public suffix"
        if h_name != w_name or h_port != w_port:
            return f"the origin https://{h_name}:{h_port} is not https://{w_name}:{w_port}"
    else:
        if any(c in w for c in "/?#@\\*"):
            return "--url-contains must be a domain or full https URL"
        w_name, w_port, werr = _parse_host_port(w)
        if werr:
            return "--url-contains must be an ASCII domain such as nid.naver.com"
        if _is_public_suffix(w_name):
            return "--url-contains cannot be a bare public suffix"
        if h_port != w_port:
            return f"the origin port {h_port} is not {w_port}"
        if not (h_name == w_name or h_name.endswith("." + w_name)):
            return f"the host {h_name} is not {w_name}"

    domains = known_site_domains(key)
    if domains is not None:
        if h_name not in domains:
            return f"{key} is typed only on {', '.join(domains)}"
        if h_port != 443:
            return f"{key} is typed only on default https port 443"
        return ""
    if not (bound_domain or "").strip():
        return _unbound_refusal(key)
    bound, berr = check_bound_domain(bound_domain)
    if berr:
        return _unbound_refusal(key, "has an invalid bound domain")
    b_name, b_port, b_perr = _parse_host_port(bound)
    if b_perr:
        return _unbound_refusal(key, "has an invalid bound domain")
    if h_name != b_name or h_port != b_port:
        disp_h = h_name if h_port == 443 else f"{h_name}:{h_port}"
        return _unbound_refusal(key, f"is bound to {bound}, not {disp_h}")
    return ""


def _is_x_login_path(path: str) -> bool:
    p = (path or "/").lower()
    if p in ("/login", "/login/", "/account/access", "/account/access/"):
        return True
    return p.startswith(("/login/", "/i/flow/login", "/i/oauth", "/oauth/", "/account/login"))


def url_ok(key: str, url: str, wanted: str, bound_domain: str = "") -> str:
    """Validate full URL (https required, ASCII host + port origin match, login path check, path match if full URL)."""
    raw_u = url or ""
    if not raw_u or any(c.isspace() or ord(c) < 32 for c in raw_u):
        return "invalid URL"
    p = urlparse(raw_u)
    if p.scheme.lower() != "https":
        return "https is required"
    if p.username or p.password or "@" in (p.netloc or "") or "\\" in (p.netloc or ""):
        return "URL userinfo is not allowed"
    u_host, u_port, uerr = _parse_host_port(p.netloc or "")
    if uerr:
        return f"URL host invalid: {uerr}"

    w = (wanted or "").strip()
    if "://" in w:
        if not w.isascii() or any(c.isspace() or ord(c) < 32 for c in w):
            return "--url-contains must use ASCII host"
        wp = urlparse(w)
        if wp.scheme.lower() != "https":
            return "--url-contains must use https"
        if wp.username or wp.password or "@" in (wp.netloc or "") or "\\" in (wp.netloc or ""):
            return "--url-contains userinfo is not allowed"
        w_host, w_port, werr = _parse_host_port(wp.netloc or "")
        if werr:
            return f"--url-contains invalid: {werr}"
        if u_host != w_host or u_port != w_port or (p.path or "/") != (wp.path or "/"):
            return "URL does not match --url-contains"

    err = host_ok(key, u_host, w, bound_domain, port=u_port)
    if err:
        return err
    ku = (key or "").upper()
    path = (p.path or "/").lower()
    if ku.startswith(("INSTAGRAM_", "INSTA_")):
        if path != "/" and "login" not in path and not path.startswith("/accounts/"):
            return "INSTAGRAM secrets are typed only on Instagram login pages"
    elif ku.startswith(("X_", "TWITTER_")):
        if not _is_x_login_path(path):
            return "X/TWITTER secrets are typed only on X/Twitter login pages"
    return ""


def _parse_kv_line(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    k, v = s.split("=", 1)
    k, v = k.strip(), v.strip()
    if not k:
        return None
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return k, v


def read_file(path):
    d = {}
    if not os.path.exists(path):
        return d
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            parsed = _parse_kv_line(line)
            if parsed is None:
                continue
            k, v = parsed
            if v or k not in d:
                d[k] = v
    return d


def load():
    """Stored values as written (encrypted ones keep the dpapi: prefix; decrypt with unprotect() only to type)."""
    sp, ap = _secrets_path(), _auto_path()
    d = read_file(ap)
    for k, v in read_file(sp).items():
        base_k = k.split("@", 1)[0].strip() if "@" in k else k
        if v or base_k not in d:
            d[base_k] = v
    return d


def ensure_secrets_file():
    """Ensure secrets.env exists with user-only ACL and any plaintext values are migrated."""
    sp = _secrets_path()
    with _secrets_lock():
        if not os.path.exists(sp):
            _write_atomic(sp, NOTE_LINE + "\n")
        else:
            restrict_acl(sp)
    return migrate()


def _verify_dpapi(stored: str) -> bool:
    try:
        unpack(stored)
        return True
    except (OSError, ValueError):
        return False


def _build_auto_content(d: dict, preserve_raw: dict[str, str] | None = None) -> str:
    """Validate and format the entire secrets-auto.env content in memory before any disk write."""
    preserved = preserve_raw or {}
    lines = ["# File written by cp secrets set. Never print or copy the values."]
    for k, v in d.items():
        if not v:
            lines.append(f"{k}=")
        elif v.startswith(ENC):
            if _verify_dpapi(v):
                lines.append(f"{k}={v}")
            elif preserved.get(k) == v:
                lines.append(f"{k}={v}")
            else:
                raise OSError(f"cannot decrypt dpapi value for: {k}")
        else:
            enc = protect(v)
            if unprotect(enc) != v:
                raise OSError(f"encryption check failed for {k}")
            lines.append(f"{k}={enc}")
    return "\n".join(lines) + "\n"


def _write_auto_unlocked(ap: str, d: dict, preserve_raw: dict[str, str] | None = None):
    content = _build_auto_content(d, preserve_raw=preserve_raw)
    _write_atomic(ap, content)


def migrate():
    """Move every plain-text value of secrets.env into secrets-auto.env (encrypted) and empty its line in secrets.env.
    Undecryptable dpapi: values (e.g. copied from another PC) are kept untouched and reported once; files are written
    only after the entire new content of both files is validated in memory."""
    sp, ap = _secrets_path(), _auto_path()
    bad_items: list[tuple[str, str]] = []
    with _secrets_lock():
        auto_dict: dict[str, str] = {}
        preserved_undecryptable: dict[str, str] = {}
        auto_bad_keys: set[str] = set()
        auto_needs_rewrite = False
        if os.path.exists(ap):
            with open(ap, encoding="utf-8-sig") as f:
                auto_lines = f.read().splitlines()
            seen_auto_keys: set[str] = set()
            for ln in auto_lines:
                parsed = _parse_kv_line(ln)
                if parsed is None:
                    continue
                k, v = parsed
                if k in seen_auto_keys:
                    auto_needs_rewrite = True
                seen_auto_keys.add(k)
                if not v:
                    auto_dict.setdefault(k, "")
                elif v.startswith(ENC):
                    if _verify_dpapi(v):
                        auto_dict[k] = v
                        preserved_undecryptable.pop(k, None)
                        auto_bad_keys.discard(k)
                    else:
                        if k not in auto_dict or not auto_dict[k]:
                            auto_dict[k] = v
                            preserved_undecryptable[k] = v
                            auto_bad_keys.add(k)
                else:
                    enc = protect(v)
                    if unprotect(enc) != v:
                        raise OSError(f"encryption check failed for {k}")
                    auto_dict[k] = enc
                    preserved_undecryptable.pop(k, None)
                    auto_bad_keys.discard(k)
                    auto_needs_rewrite = True

        moved: dict[str, str] = {}
        sp_changed = False
        sp_out_lines: list[str] = []
        sp_exists = os.path.exists(sp)
        if sp_exists:
            with open(sp, encoding="utf-8-sig") as f:
                sp_lines = f.read().splitlines()
            parsed_lines = [(_parse_kv_line(ln), ln) for ln in sp_lines]
            domain_hints: dict[str, str] = {}
            plain_base_keys: set[str] = set()
            for parsed, _ in parsed_lines:
                if parsed is None:
                    continue
                rk, v = parsed
                if not v:
                    continue
                if rk.endswith("_DOMAIN") and not v.startswith(ENC):
                    domain_hints[rk[:-7]] = v
                elif not v.startswith(ENC):
                    base_k = rk.split("@", 1)[0].strip() if "@" in rk else rk
                    plain_base_keys.add(base_k)

            for parsed, orig_ln in parsed_lines:
                if parsed is None:
                    sp_out_lines.append(orig_ln)
                    continue
                rk, v = parsed
                if not v:
                    sp_out_lines.append(orig_ln)
                    continue
                if v.startswith(ENC):
                    base_k = rk.split("@", 1)[0].strip() if "@" in rk else rk
                    if _verify_dpapi(v):
                        moved[base_k] = v
                        auto_bad_keys.discard(base_k)
                        preserved_undecryptable.pop(base_k, None)
                        sp_out_lines.append(f"{base_k}=")
                        sp_changed = True
                    else:
                        bad_items.append((sp, base_k))
                        sp_out_lines.append(orig_ln)
                    continue
                if rk.endswith("_DOMAIN") and rk[:-7] in plain_base_keys:
                    sp_out_lines.append(f"{rk}=")
                    sp_changed = True
                    continue
                if "@" in rk:
                    k, raw_dom = rk.split("@", 1)
                    k = k.strip()
                else:
                    k, raw_dom = rk, domain_hints.get(rk, "")
                dom = ""
                if raw_dom:
                    checked_dom, derr = check_bound_domain(raw_dom)
                    dom = checked_dom if not derr else ""
                enc = protect(v, dom)
                dec_v, dec_d = unpack(enc)
                if dec_v != v or dec_d != dom:
                    raise OSError(f"encryption check failed for {k}")
                moved[k] = enc
                auto_bad_keys.discard(k)
                preserved_undecryptable.pop(k, None)
                sp_out_lines.append(f"{k}=")
                sp_changed = True

        for k in sorted(auto_bad_keys):
            bad_items.append((ap, k))

        # Build and validate full output of both files before writing either file to disk.
        auto_content = ""
        if moved or auto_needs_rewrite:
            merged_auto = dict(auto_dict)
            merged_auto.update(moved)
            auto_content = _build_auto_content(merged_auto, preserve_raw=preserved_undecryptable)

        sp_content = ""
        if sp_changed:
            out_lines = list(sp_out_lines)
            if NOTE_LINE not in out_lines:
                out_lines.insert(0, NOTE_LINE)
            sp_content = "\n".join(out_lines) + "\n"

        if auto_content:
            _write_atomic(ap, auto_content)
        if sp_content:
            _write_atomic(sp, sp_content)
        elif sp_exists:
            restrict_acl(sp)

    _warn_undecryptable_once(bad_items)
    return sorted(moved)


def _existing_keys_upper(ap: str, sp: str) -> set[str]:
    names = {k.upper() for k in read_file(ap)}
    for k in read_file(sp):
        base_k = k.split("@", 1)[0].strip() if "@" in k else k
        names.add(base_k.upper())
    return names


def save_auto(key: str, value: str, domain: str = "", *, overwrite: bool = True):
    ap, sp = _auto_path(), _secrets_path()
    dom = ""
    if (domain or "").strip():
        dom, derr = check_bound_domain(domain)
        if derr:
            raise ValueError(derr)
    enc = protect(value, dom)
    dec_v, dec_d = unpack(enc)
    if dec_v != value or dec_d != dom:
        raise OSError(f"encryption check failed for {key}")
    with _secrets_lock():
        prev = read_file(ap)
        if not overwrite and key.upper() in _existing_keys_upper(ap, sp):
            raise FileExistsError(
                f"{key} already exists; secret set cannot overwrite or re-bind an existing key "
                "(ask the user in the app, manual --need login)"
            )
        d = dict(prev)
        d[key] = enc
        _write_auto_unlocked(ap, d, preserve_raw=prev)


def unbound_keys() -> list[str]:
    """Return sorted key names (never values) of custom secrets that hold a decryptable value without a valid domain."""
    sp, ap = _secrets_path(), _auto_path()
    if os.path.exists(sp) or os.path.exists(ap):
        migrate()
    s = load()
    out_names: list[str] = []
    for k, raw in s.items():
        if not raw or known_site_domains(k) is not None:
            continue
        try:
            val, bound_dom = unpack(raw)
        except (OSError, ValueError):
            continue
        if not val:
            continue
        if not (bound_dom or "").strip() or check_bound_domain(bound_dom)[1]:
            out_names.append(k)
    return sorted(out_names)


def owner_bind_domain(key: str, domain: str) -> dict:
    """In-process app helper: bind an existing unbound secret to an exact https login host without revealing its value."""
    k = (key or "").strip()
    if not k:
        return {"ok": False, "error": "key is required"}
    dom, derr = check_bound_domain(domain)
    if derr:
        return {"ok": False, "error": derr}
    known = known_site_domains(k)
    if known is not None:
        h_only, h_port, _ = _parse_host_port(dom)
        if h_only not in known or h_port != 443:
            return {"ok": False, "error": f"{k} can only be used on {', '.join(known)}"}
    try:
        migrate()
    except OSError as e:
        return {"ok": False, "error": f"could not encrypt secrets.env: {e}"}
    ap, sp = _auto_path(), _secrets_path()
    with _secrets_lock():
        prev = read_file(ap)
        d = dict(prev)
        raw = d.get(k) or ""
        if not raw:
            raw = read_file(sp).get(k) or ""
        if not raw:
            return {"ok": False, "error": f"{k} is empty or missing"}
        try:
            val, old_dom = unpack(raw)
        except (OSError, ValueError) as e:
            return {"ok": False, "error": f"{k} cannot be decrypted by this Windows user ({type(e).__name__})"}
        if not val:
            return {"ok": False, "error": f"{k} is empty or missing"}
        if old_dom:
            old_checked, old_err = check_bound_domain(old_dom)
            if not old_err:
                if old_checked == dom:
                    return {"ok": True, "key": k, "domain": dom}
                return {"ok": False, "error": f"{k} is already bound to {old_checked}"}
        enc = protect(val, dom)
        dec_v, dec_d = unpack(enc)
        if dec_v != val or dec_d != dom:
            return {"ok": False, "error": f"encryption check failed for {k}"}
        d[k] = enc
        _write_auto_unlocked(ap, d, preserve_raw=prev)
    return {"ok": True, "key": k, "domain": dom}


def bind_domain(key: str, domain: str) -> dict:
    """CLI bind entry point: always refuses to bind existing secrets so agents cannot hijack unbound keys."""
    k = (key or "").strip()
    with contextlib.suppress(OSError):
        migrate()
    s = load()
    has_val = any(ek.upper() == k.upper() and bool(ev) for ek, ev in s.items())
    if has_val:
        return {
            "ok": False,
            "error": (f"{k} already holds a value; binding an existing secret is allowed only in the app settings "
                      "(ask the user to bind it in the app, manual --need login)"),
        }
    return {
        "ok": False,
        "error": f"{k} is empty or missing; ask the user to set and bind it in the app settings (manual --need login)",
    }


def _looks_like_password_field(info: dict | None) -> bool:
    if not isinstance(info, dict):
        return False
    ac = str(info.get("autocomplete") or "").strip().lower()
    if ac and any(tok in _PW_AUTOCOMPLETE for tok in ac.split()):
        return True
    el_id = str(info.get("id") or "").lower()
    el_name = str(info.get("name") or "").lower()
    return any(tok in el_id or tok in el_name for tok in _PW_ATTR_TOKENS)


def _input_type_ok(key: str, tag: str, input_type: str, info: dict | None = None) -> bool:
    if (tag or "").lower() != "input":
        return False
    it = (input_type or "").lower()
    if it == "password":
        return True
    if (key or "").upper().endswith(_USER_KEY_SUFFIXES) and it in ("text", "email", "tel"):
        return True
    if it == "text" and _looks_like_password_field(info):
        return True
    return False


_INSPECT_JS = (
    "el => {"
    "const tag = String(el.tagName || '').toLowerCase();"
    "const type = String(el.type || (el.getAttribute && el.getAttribute('type')) || '').toLowerCase();"
    "const ac = String((el.getAttribute && el.getAttribute('autocomplete')) || el.autocomplete || '').toLowerCase();"
    "const id = String(el.id || (el.getAttribute && el.getAttribute('id')) || '').toLowerCase();"
    "const name = String(el.name || (el.getAttribute && el.getAttribute('name')) || '').toLowerCase();"
    "let visible = type !== 'hidden' && !el.hidden;"
    "if (visible && typeof getComputedStyle === 'function') {"
    "  const st = getComputedStyle(el);"
    "  if (st && (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse')) visible = false;"
    "}"
    "if (visible && typeof el.getClientRects === 'function') {"
    "  if (el.getClientRects().length === 0) visible = false;"
    "  else if (typeof el.getBoundingClientRect === 'function') {"
    "    const r = el.getBoundingClientRect();"
    "    if (r && (r.width <= 0 || r.height <= 0)) visible = false;"
    "  }"
    "}"
    "const enabled = !el.disabled && !(el.matches && el.matches(':disabled'));"
    "const val = typeof el.value === 'string' ? el.value : String(el.value ?? '');"
    "return {"
    "tag, type, autocomplete: ac, id, name, visible, enabled, valueLen: val.length, "
    "focused: document.activeElement === el, "
    "inDoc: Boolean(el.ownerDocument && el.ownerDocument === document), "
    "href: String((typeof location !== 'undefined' && location.href) || '')"
    "};"
    "}"
)


def _inspect_element(target) -> dict | None:
    res = target.evaluate(_INSPECT_JS)
    return res if isinstance(res, dict) else None


def _observed_value_len(info: dict | None, *objs) -> int | None:
    if isinstance(info, dict) and isinstance(info.get("valueLen"), int):
        return info["valueLen"]
    for obj in objs:
        if obj is not None and isinstance(getattr(obj, "value", None), str):
            return len(obj.value)
    return None


def _best_effort_clear(*targets):
    seen = set()
    for t in targets:
        if t is None or id(t) in seen:
            continue
        seen.add(id(t))
        cleared = False
        if hasattr(t, "fill"):
            with contextlib.suppress(Exception):
                t.fill("")
                cleared = True
        if not cleared and hasattr(t, "evaluate"):
            with contextlib.suppress(Exception):
                t.evaluate(
                    "el => { try { el.value = ''; "
                    "el.dispatchEvent(new Event('input', {bubbles: true})); "
                    "el.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {} }"
                )


def _resolve_visible_candidate(fr, sel: str, key: str, url_contains: str, bound_domain: str) -> dict | None:
    """Among elements matching sel in checked frame fr, find the first visible and enabled one."""
    loc = fr.locator(sel)
    n = loc.count()
    if n == 0:
        return None
    has_nth = hasattr(loc, "nth")
    limit = n if has_nth else 1
    saw_hidden_or_disabled = False
    for i in range(limit):
        cand = loc.nth(i) if has_nth else loc.first
        if hasattr(cand, "frame") and getattr(cand, "frame") is not None and cand.frame is not fr:
            return {"fatal": "resolved element does not belong to checked frame"}
        if hasattr(cand, "is_visible"):
            try:
                if not cand.is_visible():
                    saw_hidden_or_disabled = True
                    continue
            except Exception:
                saw_hidden_or_disabled = True
                continue
        if hasattr(cand, "is_enabled"):
            try:
                if not cand.is_enabled():
                    saw_hidden_or_disabled = True
                    continue
            except Exception:
                saw_hidden_or_disabled = True
                continue
        handle = None
        if hasattr(cand, "element_handle"):
            handle = cand.element_handle(timeout=2000)
            if handle is None:
                continue
            if hasattr(handle, "owner_frame"):
                of = handle.owner_frame()
                if of is not None and of != fr:
                    return {"fatal": "resolved element does not belong to checked frame"}
            if hasattr(fr, "evaluate"):
                try:
                    same_doc = fr.evaluate("el => Boolean(el && el.ownerDocument === document)", handle)
                except Exception:
                    return {"fatal": "resolved element does not belong to checked frame"}
                if not same_doc:
                    return {"fatal": "resolved element does not belong to checked frame"}

        target = handle if handle is not None else cand
        info = _inspect_element(target)
        if info is None:
            return {"fatal": "cannot inspect target element"}
        if not info.get("inDoc", True):
            return {"fatal": "resolved element does not belong to checked frame"}
        if info.get("href") and url_ok(key, info["href"], url_contains, bound_domain):
            return {"fatal": "resolved element frame URL is not allowed"}

        if "visible" in info and not info["visible"]:
            saw_hidden_or_disabled = True
            continue
        if "enabled" in info and not info["enabled"]:
            saw_hidden_or_disabled = True
            continue
        if "visible" not in info and (info.get("type") or "").lower() == "hidden" and limit > 1:
            saw_hidden_or_disabled = True
            continue
        return {"el": cand, "handle": handle, "target": target, "info": info}

    if saw_hidden_or_disabled:
        return {"hidden": True}
    return None


def _full_url_norm(u: str) -> tuple:
    p = urlparse(u or "")
    h, port, err = _parse_host_port(p.netloc or "")
    if err:
        h = (p.hostname or "").lower()
        port = 443
    return (p.scheme.lower(), h, port, p.path or "/", p.params, p.query, p.fragment)


def _post_url_allowed(key: str, url: str, url_contains: str, bound_domain: str) -> bool:
    """After typing, verify the URL is still https on the allowed login host/origin (tolerating SPA sub-path/query updates)."""
    w = (url_contains or "").strip()
    if "://" in w:
        wp = urlparse(w)
        wanted_auth = wp.netloc or ""
    else:
        wanted_auth = w
    return url_ok(key, url, wanted_auth, bound_domain) == ""


def _page_has_visible_selector(page, key: str, sel: str, url_contains: str, bound_domain: str) -> bool:
    for fr in page.frames:
        if url_ok(key, fr.url, url_contains, bound_domain):
            continue
        with contextlib.suppress(Exception):
            res = _resolve_visible_candidate(fr, sel, key, url_contains, bound_domain)
            if isinstance(res, dict) and "el" in res:
                info = res["info"]
                if _input_type_ok(key, info.get("tag", ""), info.get("type", ""), info):
                    return True
    return False


def fill_on_browser(browser, key: str, value: str, bound_domain: str, selector: str,
                    url_contains: str, clear: bool = False) -> dict:
    """Locate the target input on an allowed https login tab/frame and type key-by-key into that element only."""
    sel = (selector or "").strip()
    if not sel:
        return {"ok": False, "error": "empty selector"}
    if ">>" in sel or "internal:" in sel.lower():
        return {"ok": False, "error": "frame-piercing selectors are not allowed"}
    if known_site_domains(key) is None:
        if not (bound_domain or "").strip():
            return {"ok": False, "error": _unbound_refusal(key)}
        _, berr = check_bound_domain(bound_domain)
        if berr:
            return {"ok": False, "error": _unbound_refusal(key, "has an invalid bound domain")}

    pages = [p for c in browser.contexts for p in c.pages]
    why = {id(p): url_ok(key, p.url, url_contains, bound_domain) for p in pages}
    matching = [p for p in pages if not why[id(p)]]
    if not matching:
        reasons = sorted({r for r in why.values() if r}) if why else ["no open tabs"]
        return {"ok": False, "error": "no matching tab: " + "; ".join(reasons)[:300]}

    w = (url_contains or "").strip()
    if "://" in w:
        wp = urlparse(w)
        wanted_host, wanted_port, _ = _parse_host_port(wp.netloc or "")
        w_norm = _full_url_norm(w)
        exact_full = [p for p in matching if p.url == w or _full_url_norm(p.url) == w_norm]
        if len(exact_full) == 1:
            cands = exact_full
        elif len(exact_full) > 1:
            cands = exact_full
        else:
            same_base = [
                p for p in matching
                if _parse_host_port(urlparse(p.url).netloc or "")[:2] == (wanted_host, wanted_port)
                and (urlparse(p.url).path or "/") == (wp.path or "/")
            ]
            cands = same_base or matching
    else:
        wanted_host, wanted_port, _ = _parse_host_port(w)
        exact_host = [
            p for p in matching
            if _parse_host_port(urlparse(p.url).netloc or "")[:2] == (wanted_host, wanted_port)
        ]
        cands = exact_host or matching

    if len(cands) > 1:
        vis_tabs = [p for p in cands if _page_has_visible_selector(p, key, sel, url_contains, bound_domain)]
        if len(vis_tabs) == 1:
            cands = vis_tabs
        else:
            return {"ok": False, "error": "multiple matching tabs; specify the exact https URL in --url-contains"}

    page = cands[0]
    page_host = (urlparse(page.url).hostname or "").lower()
    saw_hidden = False
    for fr in page.frames:
        if url_ok(key, fr.url, url_contains, bound_domain):
            continue
        handle = None
        el = None
        re_target = None
        re_el = None
        try:
            resolved = _resolve_visible_candidate(fr, sel, key, url_contains, bound_domain)
            if resolved is None:
                continue
            if resolved.get("hidden"):
                saw_hidden = True
                continue
            if "fatal" in resolved:
                return {"ok": False, "error": resolved["fatal"]}

            el = resolved["el"]
            handle = resolved["handle"]
            target = resolved["target"]
            init_info = resolved["info"]
            if not _input_type_ok(key, init_info.get("tag", ""), init_info.get("type", ""), init_info):
                return {"ok": False, "error": f"{key} cannot be typed into <{init_info.get('tag') or '?'} type={init_info.get('type') or '?'}>"}

            pre_fr_url = fr.url
            pre_page_url = page.url
            if handle is not None and hasattr(handle, "click"):
                handle.click(timeout=5000)
            else:
                el.click(timeout=5000)
            if clear:
                if handle is not None and hasattr(handle, "fill"):
                    handle.fill("")
                else:
                    el.fill("")

            if (fr.url != pre_fr_url or page.url != pre_page_url
                    or url_ok(key, fr.url, url_contains, bound_domain)
                    or url_ok(key, page.url, url_contains, bound_domain)):
                return {"ok": False, "error": "frame or page URL changed before typing"}

            pre_info = _inspect_element(target)
            if pre_info is None or not pre_info.get("inDoc", True):
                return {"ok": False, "error": "target element detached before typing"}
            if pre_info.get("href") and url_ok(key, pre_info["href"], url_contains, bound_domain):
                return {"ok": False, "error": "frame URL changed before typing"}
            if not _input_type_ok(key, pre_info.get("tag", ""), pre_info.get("type", ""), pre_info):
                return {"ok": False, "error": "target input type changed before typing"}
            if not pre_info.get("focused"):
                return {"ok": False, "error": "target element lost focus before typing"}
            if handle is not None and hasattr(fr, "evaluate"):
                if not fr.evaluate("el => document.activeElement === el && el.ownerDocument === document", handle):
                    return {"ok": False, "error": "target element lost focus before typing"}

            try:
                if handle is not None and hasattr(handle, "press_sequentially"):
                    handle.press_sequentially(value, delay=70)
                elif handle is not None and hasattr(handle, "type"):
                    handle.type(value, delay=70)
                elif hasattr(el, "press_sequentially"):
                    el.press_sequentially(value, delay=70)
                else:
                    el.type(value, delay=70)
            except Exception as e:
                _best_effort_clear(handle, el)
                return {"ok": False, "error": f"typing failed: {type(e).__name__}"}

            # Post-typing verification: focus may legitimately move after the last key; verify in-process that
            # the frame/page URL host is still allowed and the target (or re-rendered element matching sel in fr)
            # holds a value whose length equals len(value).
            verified = False
            post_err = "typed value length could not be verified"
            if (not _post_url_allowed(key, fr.url, url_contains, bound_domain)
                    or not _post_url_allowed(key, page.url, url_contains, bound_domain)):
                post_err = "frame or page URL changed to disallowed origin during typing"
            else:
                try:
                    post_info = _inspect_element(target)
                except Exception:
                    post_info = None

                partial_in_target = False
                if post_info is not None and post_info.get("inDoc", True):
                    post_href = post_info.get("href") or fr.url
                    if not _post_url_allowed(key, post_href, url_contains, bound_domain):
                        post_err = "frame URL changed to disallowed origin during typing"
                    elif not _input_type_ok(key, post_info.get("tag", ""), post_info.get("type", ""), post_info):
                        post_err = "target input type changed during typing"
                    else:
                        vlen = _observed_value_len(post_info, target, el)
                        if vlen == len(value):
                            verified = True
                        elif isinstance(vlen, int) and 0 < vlen < len(value):
                            partial_in_target = True
                            post_err = "typed value length mismatch"
                        else:
                            post_err = "typed value length mismatch"

                if not verified and not partial_in_target and _post_url_allowed(key, fr.url, url_contains, bound_domain):
                    w_auth = (urlparse(url_contains.strip()).netloc or "") if "://" in (url_contains or "") else url_contains
                    re_cand = _resolve_visible_candidate(fr, sel, key, w_auth, bound_domain)
                    if isinstance(re_cand, dict) and "el" in re_cand:
                        re_el = re_cand["el"]
                        re_target = re_cand["target"]
                        re_info = re_cand["info"]
                        re_href = re_info.get("href") or fr.url
                        if (_post_url_allowed(key, re_href, url_contains, bound_domain)
                                and _input_type_ok(key, re_info.get("tag", ""), re_info.get("type", ""), re_info)):
                            re_len = _observed_value_len(re_info, re_target, re_el)
                            if re_len == len(value):
                                verified = True

            if not verified:
                _best_effort_clear(handle, el, re_target, re_el)
                return {"ok": False, "error": post_err}

            frame_host = (urlparse(fr.url).hostname or "").lower()
            return {"ok": True, "key": key, "page": frame_host, "host": frame_host, "typed": True}
        except Exception as e:
            _best_effort_clear(handle, el, re_target, re_el)
            return {"ok": False, "error": f"typing failed: {type(e).__name__}"}
    if saw_hidden:
        return {"ok": False, "error": f"no visible enabled element for selector: {sel}", "page": page_host, "host": page_host}
    return {"ok": False, "error": f"selector not found: {sel}", "page": page_host, "host": page_host}


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False), flush=True)
    sys.exit(code)


def _key_state(v: str) -> str:
    if not v:
        return "empty"
    if v.startswith(ENC) and not _verify_dpapi(v):
        return "undecryptable"
    return "set"


def _main(argv):
    ap = argparse.ArgumentParser(prog="cp secret")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    g = sp.add_parser("get")
    g.add_argument("key")
    f = sp.add_parser("fill")
    f.add_argument("key")
    f.add_argument("--selector", required=True)
    f.add_argument("--url-contains", required=True)
    f.add_argument("--clear", action="store_true", help="clear the field before typing")
    st = sp.add_parser("set")
    st.add_argument("key")
    st.add_argument("--domain", default="", help="exact https login host (required for keys without a known site prefix)")
    st.add_argument("--generate", type=int, help="create and store a new password of this length")
    bd = sp.add_parser("bind")
    bd.add_argument("key")
    bd.add_argument("--domain", required=True, help="refused on CLI for existing secrets; bind in the app settings")
    a = ap.parse_args(argv)
    if a.cmd == "bind":
        res = bind_domain(a.key, a.domain)
        out(res, 0 if res.get("ok") else 1)
    try:
        migrate()
    except OSError as e:
        out({"ok": False, "error": f"could not encrypt secrets.env: {e}"}, 1)
    if a.cmd == "set":
        known = known_site_domains(a.key)
        raw_dom = (a.domain or "").strip()
        if known is None:
            dom, derr = check_bound_domain(raw_dom)
            if derr:
                out({"ok": False, "error": derr}, 1)
        elif raw_dom:
            dom, derr = check_bound_domain(raw_dom)
            if derr:
                out({"ok": False, "error": derr}, 1)
            h_only, h_port, _ = _parse_host_port(dom)
            if h_only not in known or h_port != 443:
                out({"ok": False, "error": f"{a.key} can only be used on {', '.join(known)}"}, 1)
        else:
            dom = ""
        if a.key.upper() in _existing_keys_upper(_auto_path(), _secrets_path()):
            out({
                "ok": False,
                "error": (f"{a.key} already exists; secret set cannot overwrite or re-bind an existing key "
                          "(ask the user in the app, manual --need login)"),
            }, 1)
        if a.generate:
            import secrets as rnd
            import string
            n = max(a.generate, 12)
            while True:
                v = "".join(rnd.choice(string.ascii_letters + string.digits + "!@#$%^*") for _ in range(n))
                if any(c.isdigit() for c in v) and any(c.isalpha() for c in v) and any(c in "!@#$%^*" for c in v):
                    break
        else:
            v = sys.stdin.read().strip()
        if not v:
            out({"ok": False, "error": "empty value"}, 1)
        try:
            save_auto(a.key, v, domain=dom, overwrite=False)
        except FileExistsError as e:
            out({"ok": False, "error": str(e)}, 1)
        out({"ok": True, "key": a.key, "saved": "secrets-auto.env"})
    s = load()
    if a.cmd == "list":
        states = {k: _key_state(v) for k, v in s.items()}
        undec = sorted(k for k, st_v in states.items() if st_v == "undecryptable")
        payload = {"ok": True, "file_exists": os.path.exists(_secrets_path()), "keys": states}
        if undec:
            payload["undecryptable"] = undec
        out(payload)
    if a.cmd == "get":
        state = _key_state(s[a.key]) if a.key in s else "missing"
        out({"ok": state == "set", "key": a.key, "state": state}, 0 if state == "set" else 1)
    raw = s.get(a.key)
    try:
        v, bound_dom = unpack(raw) if raw else ("", "")
    except (OSError, ValueError) as e:
        out({"ok": False, "error": f"{a.key} cannot be decrypted by this Windows user ({type(e).__name__}); store it again"}, 1)
    if not v:
        out({"ok": False, "error": f"{a.key} is empty or missing. The user must put it in secrets.env (manual --need login)"}, 1)
    if known_site_domains(a.key) is None:
        if not (bound_dom or "").strip():
            out({"ok": False, "error": _unbound_refusal(a.key)}, 1)
        _, berr = check_bound_domain(bound_dom)
        if berr:
            out({"ok": False, "error": _unbound_refusal(a.key, "has an invalid bound domain")}, 1)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(CDP)
        res = fill_on_browser(b, a.key, v, bound_dom, a.selector, a.url_contains, clear=a.clear)
        out(res, 0 if res.get("ok") else 1)


def main(argv):
    try:
        _main(list(argv))
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
