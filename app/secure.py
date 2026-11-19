"""Windows DPAPI (CryptProtectData) through ctypes: a secret saved here can only be read back by the same Windows user on
the same PC. Used for the Telegram bot token, start code, and API keys in settings.json."""
import base64
import contextlib
import ctypes
import datetime as dt
import os
import subprocess
import sys
import threading
from ctypes import wintypes

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
_warn_lock = threading.Lock()


class _AclSizeInfo(ctypes.Structure):
    _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD), ("AclBytesFree", wintypes.DWORD)]


class _AceHeader(ctypes.Structure):
    _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE), ("AceSize", wintypes.WORD)]


class _AccessAllowedAceHead(ctypes.Structure):
    _fields_ = [("Header", _AceHeader), ("Mask", wintypes.DWORD)]


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


_ENTROPY = b"EntryPilot/v1"


def protect(text: str) -> str:
    if not text:
        return ""
    (src, _sbuf), (ent, _ebuf), out = _blob(text.encode("utf-8")), _blob(_ENTROPY), _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1,
                                                  ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return "dpapi:" + base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(value: str) -> str:
    if not value:
        return ""
    if not isinstance(value, str) or not value.startswith("dpapi:"):
        raise ValueError("not a protected value")
    raw = base64.b64decode(value[6:], validate=True)
    if not raw:
        raise ValueError("empty protected value")
    (src, _sbuf), (ent, _ebuf), out = _blob(raw), _blob(_ENTROPY), _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1,
                                                    ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed (saved by another Windows user or PC)")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _system32_exe(name: str) -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", name)


def current_user() -> str:
    buf = ctypes.create_unicode_buffer(257)
    n = wintypes.DWORD(len(buf))
    if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(n)) and buf.value:
        return buf.value
    return os.environ.get("USERNAME") or ""


def _bind(dll, spec: dict):
    """Set argtypes (and restype when given) on each named function of `dll`; looked up at call time."""
    for name, (argtypes, restype) in spec.items():
        fn = getattr(dll, name)
        fn.argtypes = argtypes
        if restype is not None:
            fn.restype = restype


_V, _B, _D = ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD
_PV, _PD = ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)


def _bind_acl_win32(adv, k32, adv_names, k32_names=()):
    """Bind the requested advapi32/kernel32 ACL functions (ctypes.windll passed in by the caller at run time)."""
    adv_spec = {
        "OpenProcessToken": ([wintypes.HANDLE, _D, ctypes.POINTER(wintypes.HANDLE)], _B),
        "GetTokenInformation": ([wintypes.HANDLE, ctypes.c_int, _V, _D, _PD], _B),
        "ConvertSidToStringSidW": ([_V, _PV], _B),
        "ConvertStringSidToSidW": ([wintypes.LPCWSTR, _PV], _B),
        "LookupAccountSidW": ([wintypes.LPCWSTR, _V, wintypes.LPWSTR, _PD, wintypes.LPWSTR, _PD,
                               ctypes.POINTER(ctypes.c_int)], _B),
        "IsValidSid": ([_V], _B),
        "GetLengthSid": ([_V], _D),
        "InitializeAcl": ([_V, _D, _D], _B),
        "AddAccessAllowedAceEx": ([_V, _D, _D, _D, _V], _B),
        "SetNamedSecurityInfoW": ([wintypes.LPWSTR, ctypes.c_int, _D, _V, _V, _V, _V], _D),
        "GetNamedSecurityInfoW": ([wintypes.LPCWSTR, ctypes.c_int, _D, _V, _V, _PV, _V, _PV], _D),
        "GetSecurityDescriptorControl": ([_V, ctypes.POINTER(wintypes.WORD), _PD], _B),
        "GetAclInformation": ([_V, _V, _D, ctypes.c_int], _B),
        "GetAce": ([_V, _D, _PV], _B),
        "EqualSid": ([_V, _V], _B),
    }
    k32_spec = {
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "LocalFree": ([_V], None),
        "CloseHandle": ([wintypes.HANDLE], None),
        "GetVolumePathNameW": ([wintypes.LPCWSTR, wintypes.LPWSTR, _D], _B),
        "GetVolumeInformationW": ([wintypes.LPCWSTR, wintypes.LPWSTR, _D, _PD, _PD, _PD, wintypes.LPWSTR, _D], _B),
    }
    _bind(adv, {n: adv_spec[n] for n in adv_names})
    _bind(k32, {n: k32_spec[n] for n in k32_names})


def current_sid() -> str:
    """Return the current process user SID (S-1-5-...) via Win32 token APIs, with System32\\whoami.exe fallback."""
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    _bind_acl_win32(adv, k32, ("OpenProcessToken", "GetTokenInformation", "ConvertSidToStringSidW"),
                    ("GetCurrentProcess", "LocalFree", "CloseHandle"))

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
    """Resolve SID to (account_name, domain_name) via LookupAccountSidW; ('', '') if unresolvable."""
    if not sid:
        return "", ""
    adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
    _bind_acl_win32(adv, k32, ("ConvertStringSidToSidW", "LookupAccountSidW"), ("LocalFree",))

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
    _bind_acl_win32(ctypes.windll.advapi32, k32, (), ("GetVolumePathNameW", "GetVolumeInformationW"))

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


def _warn_no_acl(path: str):
    folder = os.path.dirname(os.path.abspath(path)) or os.path.abspath(path)
    key = os.path.normcase(folder)
    with _warn_lock:
        if key in _ACL_WARNED:
            return
        _ACL_WARNED.add(key)
        msg = f"WARNING: folder {folder} does not support persistent ACLs (FAT/exFAT); files cannot be ACL-protected"
        ACL_WARNINGS.append(msg)
    srv = sys.modules.get("app.server")
    if srv is not None and hasattr(srv, "log"):
        with contextlib.suppress(Exception):
            srv.log(msg)
            return
    with contextlib.suppress(Exception):
        from app import paths
        os.makedirs(paths.logs_dir(), exist_ok=True)
        with open(os.path.join(paths.logs_dir(), "app.log"), "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%m-%d %H:%M:%S} {msg}\n")


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


_BROAD_PRINCIPALS = frozenset({
    "everyone", "s-1-1-0", "users", "builtin\\users", "s-1-5-32-545",
    "authenticated users", "nt authority\\authenticated users", "s-1-5-11",
    "nt authority\\system", "s-1-5-18", "builtin\\administrators", "s-1-5-32-544",
})


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
    user_m = _mbcs_norm(current_user())
    if user_m and (pl == user_m or pl.endswith("\\" + user_m)):
        return True
    return False


def _set_and_verify_protected_dacl(path: str, sid: str) -> None:
    adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
    _bind_acl_win32(adv, k32, ("ConvertStringSidToSidW", "IsValidSid", "GetLengthSid", "InitializeAcl",
                               "AddAccessAllowedAceEx", "SetNamedSecurityInfoW", "GetNamedSecurityInfoW",
                               "GetSecurityDescriptorControl", "GetAclInformation", "GetAce", "EqualSid"),
                    ("LocalFree",))

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
    """Set a protected user-only DACL for current user SID via Win32 SetNamedSecurityInfoW and verify in-process.
    On volumes without persistent ACLs (FAT/exFAT), record a one-time warning and return False without failing."""
    if not volume_supports_acls(path):
        _warn_no_acl(path)
        return False
    sid = current_sid()
    if not sid or not sid.upper().startswith("S-1-"):
        raise OSError(f"cannot determine Windows user SID for ACL on {path}")
    _set_and_verify_protected_dacl(path, sid)
    return True
