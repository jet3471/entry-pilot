"""Install, shortcuts, Windows "Apps" entry and uninstall. Per user (HKCU, %LOCALAPPDATA%): no admin rights."""
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import winreg
import zipfile

from app import paths

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\EntryPilot"
INSTALL_RECORD_NAME = "install.json"
MANIFEST_NAME = "manifest.json"
UNINSTALL_WAIT_SEC = 20.0

APP_ZIP_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
NODE_ZIP_MAX_BYTES = 200 * 1024 * 1024      # 200 MB
MAX_ZIP_ENTRIES = 20_000
MAX_ZIP_RATIO = 200.0

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_STILL_ACTIVE = 259
_EPOCH_AS_FILETIME = 116444736000000000  # 100-ns ticks between 1601-01-01 and 1970-01-01
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_VALID_PE_MACHINES = (0x014C, 0x8664, 0xAA64)
_VALID_PE_OPT_MAGICS = (0x010B, 0x020B)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
_kernel32.GetProcessTimes.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


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


def _is_reparse(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _no_reparse_in_chain(path: str) -> bool:
    norm = os.path.normpath(path)
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


def _forbidden_roots() -> set[str]:
    out: set[str] = set()
    raw = [os.path.expanduser("~"), tempfile.gettempdir()]
    for env_name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)",
                     "ProgramData", "SystemRoot", "windir"):
        v = os.environ.get(env_name)
        if v:
            raw.append(v)
    for p in raw:
        if p:
            try:
                out.add(os.path.normcase(os.path.normpath(os.path.realpath(p))))
            except OSError:
                out.add(os.path.normcase(os.path.normpath(p)))
    return out


def _is_safe_dir_path(path: str) -> bool:
    """Ensure `path` is a local drive path of depth >= 2 and neither equal to nor an ancestor of any system/profile root."""
    if not _is_local_drive_path(path):
        return False
    norm = os.path.normpath(path)
    drive, tail = os.path.splitdrive(norm)
    parts = [p for p in tail.split("\\") if p]
    if not drive or len(parts) < 2:
        return False
    nc = os.path.normcase(norm)
    nc_prefix = nc.rstrip("\\") + "\\"
    for fb in _forbidden_roots():
        if nc == fb or fb.startswith(nc_prefix):
            return False
    return True


def _safe_remove_tree(path: str):
    if not os.path.lexists(path):
        return
    if _is_reparse(path):
        try:
            os.rmdir(path)
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                pass
        return
    shutil.rmtree(path, ignore_errors=True)


def _special(name: str) -> str:
    if not isinstance(name, str) or not name.isalpha():
        return ""
    r = subprocess.run([paths.powershell_exe(), "-NoProfile", "-NonInteractive", "-Command",
                        f"[Environment]::GetFolderPath('{name}')"],
                       capture_output=True, text=True, encoding="mbcs", errors="replace",
                       creationflags=NO_WINDOW, timeout=30)
    return (r.stdout or "").strip()


def shortcut_paths() -> list:
    out = []
    for folder in (_special("Programs"), _special("DesktopDirectory")):
        if folder:
            out.append(os.path.join(folder, paths.APP_TITLE + ".lnk"))
    return out


def make_shortcuts(exe: str):
    for lnk in shortcut_paths():
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:EP_LNK);"
              "$s.TargetPath=$env:EP_EXE;$s.WorkingDirectory=$env:EP_WD;$s.IconLocation=$env:EP_EXE+',0';"
              "$s.Description='Entry Pilot';$s.Save()")
        env = dict(os.environ, EP_LNK=lnk, EP_EXE=exe, EP_WD=os.path.dirname(exe))
        subprocess.run([paths.powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", ps],
                       env=env, creationflags=NO_WINDOW, timeout=60, capture_output=True)


def _write_install_record(inst_root: str, exe: str):
    rec_path = os.path.join(inst_root, INSTALL_RECORD_NAME)
    data = {
        "install_location": os.path.realpath(inst_root),
        "exe": os.path.realpath(exe),
        "version": paths.VERSION,
    }
    tmp = rec_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, rec_path)


def register(exe: str, size_kb: int = 0, inst_root: str | None = None):
    loc = os.path.realpath(inst_root or paths.install_root())
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, val in (("DisplayName", paths.APP_TITLE), ("DisplayVersion", paths.VERSION),
                          ("Publisher", "Entry Pilot"), ("InstallLocation", loc),
                          ("DisplayIcon", exe), ("UninstallString", f'"{exe}" --uninstall')):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        if size_kb:
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)


def _process_info(pid: int) -> tuple[str, float] | None:
    """Return (executable_path, creation_epoch_seconds) for an active process, or None."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    h = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        sz = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(32768)
        if not _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)) or not buf.value:
            return None
        ct, et, kt, ut = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not _kernel32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
            return None
        ticks = (ct.dwHighDateTime << 32) | ct.dwLowDateTime
        create_sec = (ticks - _EPOCH_AS_FILETIME) / 10_000_000.0
        return buf.value, create_sec
    finally:
        _kernel32.CloseHandle(h)


def _run_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, paths.APP_NAME)


def run_json_name_for_folder(folder_path: str) -> str:
    """run-<first 8 hex of sha256(lowercased app folder path)>.json."""
    digest = hashlib.sha256(folder_path.lower().encode("utf-8")).hexdigest()[:8]
    return f"run-{digest}.json"


def _run_json_candidates(inst_root: str | None = None) -> list[str]:
    """Candidate run files under %LOCALAPPDATA%\\EntryPilot: run-<8hex>.json first, then fallback run.json."""
    run_base = _run_dir()
    root = inst_root or paths.install_root()
    raw_folders = [os.path.join(root, "app"), root]
    if inst_root is None:
        raw_folders.extend([paths.app_dir(), paths.root()])
    seen: set[str] = set()
    out: list[str] = []
    for folder in raw_folders:
        if not folder:
            continue
        variants = [os.path.normpath(folder), folder]
        try:
            variants.insert(0, os.path.normpath(os.path.realpath(folder)))
        except OSError:
            pass
        for v in variants:
            cand = os.path.join(run_base, run_json_name_for_folder(v))
            nc = os.path.normcase(cand)
            if nc not in seen:
                seen.add(nc)
                out.append(cand)
    fallback = os.path.join(run_base, "run.json")
    if os.path.normcase(fallback) not in seen:
        out.append(fallback)
    return out


def _run_json_path(inst_root: str | None = None) -> str:
    """Read run-<8hex>.json under %LOCALAPPDATA%\\EntryPilot if present, falling back to run.json."""
    cands = _run_json_candidates(inst_root)
    for p in cands:
        if os.path.isfile(p) and not _is_reparse(p):
            return p
    return cands[0]


def running_instance(inst_root: str | None = None) -> dict:
    for p in _run_json_candidates(inst_root):
        if not os.path.isfile(p) or _is_reparse(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            continue
    return {}


def _is_active_app_process(pid: int, inst_root: str, run_mtime: float) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        return False
    pinfo = _process_info(pid)
    if not pinfo:
        return False
    proc_exe, proc_ctime = pinfo
    if not _is_local_drive_path(proc_exe) or not _no_reparse_in_chain(proc_exe):
        return False
    real_root = os.path.realpath(inst_root)
    real_exe = os.path.realpath(proc_exe)
    try:
        common = os.path.commonpath([os.path.normcase(real_root), os.path.normcase(real_exe)])
    except ValueError:
        return False
    if common != os.path.normcase(real_root) or os.path.normcase(real_exe) == os.path.normcase(real_root):
        return False
    return proc_ctime <= run_mtime


def _find_active_app_pid(inst_root: str) -> tuple[int, float] | None:
    if not _is_safe_dir_path(inst_root) or not os.path.isdir(inst_root) or not _no_reparse_in_chain(inst_root):
        return None
    for run_file in _run_json_candidates(inst_root):
        if not os.path.isfile(run_file) or _is_reparse(run_file):
            continue
        try:
            run_mtime = os.stat(run_file).st_mtime
            with open(run_file, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict):
            continue
        pid = info.get("pid")
        if _is_active_app_process(pid, inst_root, run_mtime):
            return pid, run_mtime
    return None


def wait_for_running_app_exit(inst_root: str, timeout_sec: float = UNINSTALL_WAIT_SEC) -> bool:
    """Confirm no other Entry Pilot instance in `inst_root` is running, waiting up to `timeout_sec` seconds."""
    found = _find_active_app_pid(inst_root)
    if not found:
        return True
    pid, run_mtime = found
    elapsed = 0.0
    t0 = time.monotonic()
    step = 0.5
    while True:
        if not _is_active_app_process(pid, inst_root, run_mtime):
            return True
        real_elapsed = time.monotonic() - t0
        effective_elapsed = max(elapsed, real_elapsed)
        if effective_elapsed >= timeout_sec:
            return False
        wait_s = min(step, timeout_sec - effective_elapsed)
        time.sleep(wait_s)
        elapsed += wait_s


def stop_running_app() -> bool:
    """Force-kill the PID in run-<8hex>.json / run.json ONLY when its exe is inside the install folder and started before mtime."""
    inst_root = paths.install_root()
    found = _find_active_app_pid(inst_root)
    if not found:
        return False
    pid, _ = found
    subprocess.run([paths.system32_exe("taskkill.exe"), "/PID", str(pid), "/T", "/F"],
                   capture_output=True, text=True, encoding="mbcs", errors="replace",
                   creationflags=NO_WINDOW, timeout=30)
    return True


def safe_extract_zip(
    zf: zipfile.ZipFile,
    dest_dir: str,
    progress=None,
    *,
    max_total_bytes: int = APP_ZIP_MAX_BYTES,
    max_entries: int = MAX_ZIP_ENTRIES,
    max_ratio: float = MAX_ZIP_RATIO,
):
    """Extract `zf` into `dest_dir`, rejecting traversal ('..'), absolute/UNC paths, drive letters, ':' ADS names,
    symlinks, excessive entry counts, excessive total uncompressed size, and zip-bomb compression ratios."""
    if not _is_local_drive_path(dest_dir):
        raise ValueError(f"invalid extraction target directory: {dest_dir!r}")
    os.makedirs(dest_dir, exist_ok=True)
    if not _no_reparse_in_chain(dest_dir):
        raise ValueError(f"reparse point in extraction target path: {dest_dir!r}")
    dest_real = os.path.realpath(dest_dir)
    dest_nc = os.path.normcase(dest_real)

    infos = zf.infolist()
    if max_entries > 0 and len(infos) > max_entries:
        raise ValueError(f"unsafe zip: entry count {len(infos)} exceeds limit {max_entries}")

    declared_total = 0
    plan: list[tuple[zipfile.ZipInfo, str, bool]] = []
    for info in infos:
        raw = info.filename
        if not raw or any(c in raw for c in ("\x00", "\r", "\n")):
            raise ValueError(f"unsafe zip entry (empty or control char): {raw!r}")
        if raw.startswith(("/", "\\")):
            raise ValueError(f"unsafe zip entry (absolute or UNC path): {raw!r}")
        if ":" in raw:
            raise ValueError(f"unsafe zip entry (drive letter or ADS ':'): {raw!r}")
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise ValueError(f"unsafe zip entry (symlink): {raw!r}")
        norm_sep = raw.replace("\\", "/")
        is_dir = info.is_dir() or norm_sep.endswith("/")
        parts = [p for p in norm_sep.split("/") if p]
        if not parts or any(p in (".", "..") or p.endswith((".", " ")) for p in parts):
            raise ValueError(f"unsafe zip entry (traversal or invalid segment): {raw!r}")
        candidate = os.path.normpath(os.path.join(dest_real, *parts))
        resolved = os.path.realpath(candidate)
        try:
            common = os.path.commonpath([dest_nc, os.path.normcase(resolved)])
        except ValueError:
            raise ValueError(f"unsafe zip entry (cross-drive): {raw!r}")
        if common != dest_nc or os.path.normcase(resolved) == dest_nc:
            raise ValueError(f"unsafe zip entry (leaves target folder): {raw!r}")
        if not is_dir:
            if info.file_size < 0 or info.compress_size < 0:
                raise ValueError(f"unsafe zip entry (negative size): {raw!r}")
            if max_ratio > 0 and info.file_size > 0:
                if info.compress_size <= 0 or info.file_size > info.compress_size * max_ratio:
                    raise ValueError(f"unsafe zip entry (compression ratio exceeds {max_ratio:g}:1): {raw!r}")
            declared_total += info.file_size
            if max_total_bytes > 0 and declared_total > max_total_bytes:
                raise ValueError(f"unsafe zip: total uncompressed size exceeds limit ({max_total_bytes} bytes)")
        plan.append((info, resolved, is_dir))

    total = max(1, len(plan))
    actual_total = 0
    for i, (info, resolved, is_dir) in enumerate(plan):
        if is_dir:
            os.makedirs(resolved, exist_ok=True)
            if not _no_reparse_in_chain(resolved):
                raise ValueError(f"reparse point encountered during zip extract: {resolved!r}")
        else:
            parent = os.path.dirname(resolved)
            os.makedirs(parent, exist_ok=True)
            if not _no_reparse_in_chain(parent):
                raise ValueError(f"reparse point in parent during zip extract: {parent!r}")
            if os.path.lexists(resolved) and _is_reparse(resolved):
                raise ValueError(f"reparse point target during zip extract: {resolved!r}")
            entry_written = 0
            try:
                with zf.open(info, "r") as src, open(resolved, "wb") as dst:
                    while True:
                        chunk = src.read(1 << 16)
                        if not chunk:
                            break
                        entry_written += len(chunk)
                        actual_total += len(chunk)
                        if max_total_bytes > 0 and actual_total > max_total_bytes:
                            raise ValueError(f"unsafe zip: extracted size exceeds limit ({max_total_bytes} bytes)")
                        if max_ratio > 0 and entry_written > 0:
                            if info.compress_size <= 0 or entry_written > info.compress_size * max_ratio:
                                raise ValueError(
                                    f"unsafe zip entry (stream ratio exceeds {max_ratio:g}:1): {info.filename!r}"
                                )
                        dst.write(chunk)
                if entry_written != info.file_size:
                    raise ValueError(
                        f"unsafe zip entry (declared size {info.file_size} != actual {entry_written}): {info.filename!r}"
                    )
            except BaseException:
                try:
                    if os.path.lexists(resolved) and not _is_reparse(resolved):
                        os.remove(resolved)
                except OSError:
                    pass
                raise
        if progress is not None and i % 50 == 0:
            progress(i, total)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_valid_pe(path: str) -> bool:
    """Verify that `path` is a non-reparse regular file with valid DOS MZ and PE\\0\\0 COFF headers."""
    if not os.path.isfile(path) or _is_reparse(path):
        return False
    try:
        size = os.path.getsize(path)
        if size < 0x40 + 24:
            return False
        with open(path, "rb") as f:
            dos = f.read(0x40)
            if len(dos) < 0x40 or dos[:2] != b"MZ":
                return False
            e_lfanew = struct.unpack_from("<I", dos, 0x3C)[0]
            if e_lfanew < 0x40 or e_lfanew > min(size - 24, 16 * 1024 * 1024):
                return False
            f.seek(e_lfanew)
            pe = f.read(26)
        if len(pe) < 24 or pe[:4] != b"PE\x00\x00":
            return False
        machine = struct.unpack_from("<H", pe, 4)[0]
        if machine not in _VALID_PE_MACHINES:
            return False
        opt_size = struct.unpack_from("<H", pe, 20)[0]
        if opt_size > 0:
            if opt_size < 2 or e_lfanew + 24 + opt_size > size or len(pe) < 26:
                return False
            opt_magic = struct.unpack_from("<H", pe, 24)[0]
            if opt_magic not in _VALID_PE_OPT_MAGICS:
                return False
        return True
    except OSError:
        return False


def _normalize_sha256(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.lower().startswith("sha256:"):
        s = s[7:].strip()
    if not _SHA256_RE.fullmatch(s):
        return None
    return s.lower()


def _parse_manifest_entries(data) -> dict[str, str] | None:
    if not isinstance(data, dict) or not data:
        return None
    if "files" in data:
        container = data["files"]
    elif "hashes" in data:
        container = data["hashes"]
    elif "entries" in data:
        container = data["entries"]
    else:
        container = data

    entries: dict[str, str] = {}
    if isinstance(container, dict):
        if not container:
            return None
        for rel_path, val in container.items():
            if not isinstance(rel_path, str):
                return None
            if isinstance(val, dict):
                digest = _normalize_sha256(val.get("sha256") or val.get("hash"))
            else:
                digest = _normalize_sha256(val)
            if not digest:
                return None
            entries[rel_path] = digest
    elif isinstance(container, list):
        if not container:
            return None
        for item in container:
            if not isinstance(item, dict):
                return None
            rel_path = item.get("path") or item.get("file") or item.get("name")
            digest = _normalize_sha256(item.get("sha256") or item.get("hash"))
            if not isinstance(rel_path, str) or not digest:
                return None
            entries[rel_path] = digest
    else:
        return None
    return entries if entries else None


def _verify_manifest_file(dest: str, manifest_path: str) -> bool:
    if _is_reparse(manifest_path) or not os.path.isfile(manifest_path):
        return False
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    entries = _parse_manifest_entries(data)
    if not entries:
        return False

    dest_real = os.path.realpath(dest)
    dest_nc = os.path.normcase(dest_real)
    want_exe_nc = (paths.APP_NAME + ".exe").lower()
    saw_exe = False

    for raw_rel, want_sha in entries.items():
        if not raw_rel or any(c in raw_rel for c in ("\x00", "\r", "\n")) or ":" in raw_rel:
            return False
        if raw_rel.startswith(("/", "\\")):
            return False
        parts = [p for p in raw_rel.replace("\\", "/").split("/") if p]
        if not parts or any(p in (".", "..") or p.endswith((".", " ")) for p in parts):
            return False
        if len(parts) == 1 and parts[0].lower() == want_exe_nc:
            saw_exe = True
        full = os.path.normpath(os.path.join(dest_real, *parts))
        real = os.path.realpath(full)
        try:
            common = os.path.commonpath([dest_nc, os.path.normcase(real)])
        except ValueError:
            return False
        if common != dest_nc or os.path.normcase(real) == dest_nc:
            return False
        if not os.path.isfile(real) or not _no_reparse_in_chain(real):
            return False
        try:
            got_sha = _sha256_file(real)
        except OSError:
            return False
        if not hmac.compare_digest(got_sha, want_sha):
            return False
    return saw_exe


def _verify_app_bundle(dest: str, exe: str) -> bool:
    if not os.path.isdir(dest) or not _no_reparse_in_chain(dest):
        return False
    internal_dir = os.path.join(dest, "_internal")
    if not os.path.isdir(internal_dir) or _is_reparse(internal_dir):
        return False
    if not _is_valid_pe(exe):
        return False
    for mpath in (os.path.join(dest, MANIFEST_NAME), os.path.join(internal_dir, MANIFEST_NAME)):
        if os.path.lexists(mpath):
            return _verify_manifest_file(dest, mpath)
    return True


def _verify_installed_app(dest: str, exe: str) -> bool:
    return _verify_app_bundle(dest, exe)


def _ensure_install_dirs(inst_root: str):
    if not _is_safe_dir_path(inst_root):
        raise RuntimeError("설치 대상 경로가 안전하지 않아 설치를 중단합니다.")
    os.makedirs(inst_root, exist_ok=True)
    if not _no_reparse_in_chain(inst_root):
        raise RuntimeError("설치 경로에 심볼릭 링크/분기점(reparse point)이 있어 설치를 중단합니다.")
    for rel in (
        ("runtime",),
        ("data",),
        ("data", "logs"),
        ("logins", "claude"),
        ("logins", "codex"),
        ("data", "work"),
        ("data", "board"),
        ("data", "captures"),
    ):
        d = os.path.join(inst_root, *rel)
        os.makedirs(d, exist_ok=True)


def install_zip(zip_path: str, report=lambda pct, text: None) -> str:
    """Unpack the app zip into %LOCALAPPDATA%\\EntryPilot\\app (verifying before swap and keeping .old until verified)."""
    inst_root = paths.install_root()
    _ensure_install_dirs(inst_root)
    stop_running_app()
    dest = os.path.join(inst_root, "app")
    new = dest + ".new"
    old = dest + ".old"
    _safe_remove_tree(new)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            safe_extract_zip(
                zf,
                new,
                progress=lambda i, n: report(5 + 85 * i // n, "파일 복사 중"),
                max_total_bytes=APP_ZIP_MAX_BYTES,
            )
        new_exe = os.path.join(new, paths.APP_NAME + ".exe")
        if not _verify_app_bundle(new, new_exe):
            raise RuntimeError("새 버전 설치 검증에 실패했습니다 (매니페스트 또는 실행 파일 확인 실패).")
    except BaseException:
        _safe_remove_tree(new)
        raise

    _safe_remove_tree(old)
    had_old = False
    try:
        if os.path.lexists(dest):
            if _is_reparse(dest):
                raise RuntimeError("기존 설치 폴더가 비정상 링크(reparse point)여서 설치를 중단합니다.")
            try:
                os.replace(dest, old)
                had_old = True
            except OSError:
                raise RuntimeError("이전 버전이 아직 실행 중입니다. Entry Pilot을 닫고 다시 실행하세요.")
        os.replace(new, dest)
        exe = os.path.join(dest, paths.APP_NAME + ".exe")
        if not _verify_installed_app(dest, exe):
            raise RuntimeError("새 버전 설치 검증에 실패했습니다 (실행 파일 확인 실패).")
        _write_install_record(inst_root, exe)
        if not os.environ.get("EP_TEST_NO_SHELL"):  # tests install into a temp folder without touching the user's desktop
            report(92, "바로가기 만드는 중")
            make_shortcuts(exe)
            size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(dest) for f in fs) // 1024
            register(exe, size, inst_root=inst_root)
    except BaseException:
        _safe_remove_tree(new)
        if had_old and os.path.lexists(old):
            _safe_remove_tree(dest)
            os.replace(old, dest)
        raise
    if had_old and os.path.lexists(old):
        _safe_remove_tree(old)
    report(100, "설치 완료")
    return exe


def recorded_install_root() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            val, _ = winreg.QueryValueEx(k, "InstallLocation")
            return str(val or "").strip()
    except OSError:
        return ""


def _recorded_file_location(rec_dir: str) -> str | None:
    """Return install_location from install.json if present; None if absent; '' if malformed/reparse."""
    p = os.path.join(rec_dir, INSTALL_RECORD_NAME)
    if not os.path.lexists(p):
        return None
    if _is_reparse(p) or not os.path.isfile(p):
        return ""
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        loc = str((d or {}).get("install_location") or "").strip() if isinstance(d, dict) else ""
        return loc
    except (OSError, ValueError):
        return ""


def check_uninstall_target(target: str | None = None, keep_data: bool = False) -> tuple[str, str]:
    """Validate uninstall target without trusting EP_APP_DIR.

    Checks both the recorded install location (registry + install.json if present) and, when frozen,
    the folder of the running executable (sys.executable). Returns (real_target, error_message).
    """
    if paths.portable():
        return "", "포터블판은 자동 삭제 대상이 아닙니다. 폴더를 직접 삭제하세요."
    rec = recorded_install_root()
    if not rec:
        return "", "등록된 설치 경로를 찾을 수 없어 삭제를 중단했습니다."
    if not _is_safe_dir_path(rec) or not os.path.isdir(rec) or not _no_reparse_in_chain(rec):
        return "", "등록된 설치 경로가 유효하지 않아 삭제를 중단했습니다."
    rec_real = os.path.realpath(rec)
    if os.path.normcase(os.path.abspath(rec)) != os.path.normcase(rec_real) or not _is_safe_dir_path(rec_real):
        return "", "등록된 설치 경로 검증에 실패하여 삭제를 중단했습니다."

    file_loc = _recorded_file_location(rec_real)
    if file_loc is not None:
        if not file_loc or not _is_local_drive_path(file_loc):
            return "", "설치 기록 파일(install.json)이 손상되어 삭제를 중단했습니다."
        if os.path.normcase(os.path.realpath(file_loc)) != os.path.normcase(rec_real):
            return "", "설치 기록 파일과 등록된 설치 경로가 일치하지 않아 삭제를 중단했습니다."

    if paths.frozen():
        exe_path = sys.executable or ""
        if not _is_local_drive_path(exe_path) or not _no_reparse_in_chain(exe_path):
            return "", "실행 중인 프로그램 경로를 확인할 수 없어 삭제를 중단했습니다."
        exe_dir = os.path.dirname(os.path.realpath(exe_path))
        if os.path.basename(exe_dir).lower() != "app":
            return "", "실행 중인 프로그램이 설치 폴더(app) 안에 있지 않아 삭제를 중단했습니다."
        exe_root = os.path.dirname(exe_dir)
        if os.path.normcase(exe_root) != os.path.normcase(rec_real):
            return "", "실행 중인 프로그램 위치와 등록된 설치 경로가 일치하지 않아 삭제를 중단했습니다."

    expected = os.path.join(rec_real, "app") if keep_data else rec_real
    if not _is_safe_dir_path(expected) or not os.path.isdir(expected) or not _no_reparse_in_chain(expected):
        return "", "삭제할 설치 폴더가 없거나 안전하지 않아 삭제를 중단했습니다."

    cand = expected if target is None else target
    if not isinstance(cand, str) or not _is_safe_dir_path(cand):
        return "", "삭제 대상 경로 형식이 올바르지 않아 삭제를 중단했습니다."
    if not os.path.isdir(cand) or not _no_reparse_in_chain(cand):
        return "", "삭제 대상 폴더가 없거나 심볼릭 링크/분기점(reparse point)이어서 삭제를 중단했습니다."
    real = os.path.realpath(cand)
    if os.path.normcase(os.path.abspath(cand)) != os.path.normcase(real):
        return "", "삭제 대상 경로가 실제 경로와 일치하지 않아 삭제를 중단했습니다."
    if os.path.normcase(real) != os.path.normcase(expected):
        return "", "삭제 대상 경로가 등록된 설치 경로와 일치하지 않아 삭제를 중단했습니다."
    return real, ""


def valid_uninstall_target(target: str, keep_data: bool = False) -> str:
    """Return validated canonical uninstall target path, or '' when refused."""
    real, _ = check_uninstall_target(target=target, keep_data=keep_data)
    return real


_UNINSTALL_HELPER_PY = '''"""Detached Entry Pilot uninstall helper: waits for parent exit, re-checks path, deletes tree, then removes registry key."""
import ctypes
import os
import shutil
import sys
import tempfile
import time
import winreg

REPARSE_ATTR = 0x400
DEFAULT_UNINSTALL_KEY = r"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\EntryPilot"


def is_local_drive_path(p: str) -> bool:
    if not isinstance(p, str) or not p or any(c in p for c in ("\\x00", "\\r", "\\n")):
        return False
    if p.startswith(("\\\\\\\\", "//")):
        return False
    drive, tail = os.path.splitdrive(p)
    if len(drive) != 2 or not drive[0].isascii() or not drive[0].isalpha() or drive[1] != ":":
        return False
    if not tail.startswith(("\\\\", "/")) or ":" in tail:
        return False
    parts = [seg for seg in tail.replace("/", "\\\\").split("\\\\") if seg]
    if len(parts) < 2 or any(seg in (".", "..") or seg.endswith((".", " ")) for seg in parts):
        return False
    return True


def is_reparse(p: str) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return True
    if os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & REPARSE_ATTR)


def no_reparse_in_chain(p: str) -> bool:
    norm = os.path.normpath(p)
    drive, tail = os.path.splitdrive(norm)
    cur = drive + "\\\\"
    if is_reparse(cur):
        return False
    for seg in (s for s in tail.split("\\\\") if s):
        cur = os.path.join(cur, seg)
        if is_reparse(cur):
            return False
    return True


def not_forbidden(p: str) -> bool:
    nc = os.path.normcase(os.path.normpath(p))
    prefix = nc.rstrip("\\\\") + "\\\\"
    raw = [os.path.expanduser("~"), tempfile.gettempdir()]
    for k in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)",
              "ProgramData", "SystemRoot", "windir"):
        v = os.environ.get(k)
        if v:
            raw.append(v)
    for item in raw:
        if not item:
            continue
        fb = os.path.normcase(os.path.normpath(os.path.realpath(item)))
        if nc == fb or fb.startswith(prefix):
            return False
    return True


def wait_parent(pid: int, timeout_ms: int = 10000):
    if pid <= 0:
        return
    h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        try:
            ctypes.windll.kernel32.WaitForSingleObject(h, timeout_ms)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)


def remove_uninstall_registry_entry() -> bool:
    reg_key = os.environ.get("EP_UNINSTALL_REG_KEY") or (
        "" if os.environ.get("EP_TEST_NO_SHELL") else DEFAULT_UNINSTALL_KEY
    )
    if not reg_key:
        return True
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_key)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def recheck_and_delete(target: str) -> int:
    if not is_local_drive_path(target) or not os.path.isdir(target) or not no_reparse_in_chain(target):
        return 2
    real = os.path.realpath(target)
    if os.path.normcase(os.path.abspath(target)) != os.path.normcase(real) or not not_forbidden(real):
        return 3
    for _ in range(20):
        if not os.path.lexists(real):
            return 0 if remove_uninstall_registry_entry() else 5
        if not no_reparse_in_chain(real):
            return 4
        shutil.rmtree(real, ignore_errors=True)
        if not os.path.lexists(real):
            return 0 if remove_uninstall_registry_entry() else 5
        time.sleep(0.3)
    if os.path.lexists(real):
        return 1
    return 0 if remove_uninstall_registry_entry() else 5


if __name__ == "__main__":
    parent_pid = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
    target_dir = sys.argv[2] if len(sys.argv) > 2 else ""
    wait_parent(parent_pid)
    sys.exit(recheck_and_delete(target_dir))
'''

_UNINSTALL_HELPER_PS1 = r'''param([int]$ParentPid, [string]$Target)
$ErrorActionPreference = "Stop"
if ($ParentPid -gt 0) {
    try { Wait-Process -Id $ParentPid -Timeout 10 -ErrorAction SilentlyContinue } catch {}
}
function Test-SafeUninstallTarget([string]$PathToCheck) {
    if ([string]::IsNullOrWhiteSpace($PathToCheck)) { return $false }
    if ($PathToCheck.StartsWith("\\") -or $PathToCheck.StartsWith("//")) { return $false }
    if ($PathToCheck -notmatch '^[A-Za-z]:\\[^:]+$') { return $false }
    $norm = [System.IO.Path]::GetFullPath($PathToCheck).TrimEnd('\')
    $parts = $norm.Substring(3).Split('\', [System.StringSplitOptions]::RemoveEmptyEntries)
    if ($parts.Count -lt 2) { return $false }
    $forbidden = @($env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA, $env:ProgramFiles,
                   ${env:ProgramFiles(x86)}, $env:ProgramData, $env:SystemRoot, $env:windir,
                   [System.IO.Path]::GetTempPath())
    foreach ($fb in $forbidden) {
        if ([string]::IsNullOrWhiteSpace($fb)) { continue }
        $fbn = [System.IO.Path]::GetFullPath($fb).TrimEnd('\')
        if ($norm.Equals($fbn, [System.StringComparison]::OrdinalIgnoreCase) -or
            $fbn.StartsWith($norm + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
    }
    if (-not (Test-Path -LiteralPath $norm -PathType Container)) { return $false }
    $cur = $norm.Substring(0, 3)
    $rootItem = Get-Item -LiteralPath $cur -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    foreach ($seg in $parts) {
        $cur = Join-Path $cur $seg
        $it = Get-Item -LiteralPath $cur -Force
        if (($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    }
    return $true
}
function Remove-UninstallRegistryEntry {
    $regKey = $env:EP_UNINSTALL_REG_KEY
    if ([string]::IsNullOrWhiteSpace($regKey)) {
        if (-not [string]::IsNullOrWhiteSpace($env:EP_TEST_NO_SHELL)) { return $true }
        $regKey = "Software\Microsoft\Windows\CurrentVersion\Uninstall\EntryPilot"
    }
    $regPath = "HKCU:\$regKey"
    try {
        if (Test-Path -LiteralPath $regPath) {
            Remove-Item -LiteralPath $regPath -Recurse -Force -ErrorAction Stop
        }
        return $true
    } catch {
        return $false
    }
}
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Test-Path -LiteralPath $Target)) {
        if (Remove-UninstallRegistryEntry) { exit 0 } else { exit 5 }
    }
    if (-not (Test-SafeUninstallTarget $Target)) { exit 2 }
    try {
        Get-ChildItem -LiteralPath $Target -Recurse -Force -ErrorAction SilentlyContinue |
            Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
            ForEach-Object { [System.IO.Directory]::Delete($_.FullName) }
        Remove-Item -LiteralPath $Target -Recurse -Force -ErrorAction Stop
        if (-not (Test-Path -LiteralPath $Target)) {
            if (Remove-UninstallRegistryEntry) { exit 0 } else { exit 5 }
        }
    } catch {}
    Start-Sleep -Milliseconds 300
}
if (-not (Test-Path -LiteralPath $Target)) {
    if (Remove-UninstallRegistryEntry) { exit 0 } else { exit 5 }
}
exit 1
'''


def _restrict_temp_dir(path: str):
    """Restrict `path` to the current user only (protected DACL); best effort."""
    from app.secure import restrict_acl
    try:
        restrict_acl(path)
    except OSError:
        pass


def _exe_inside_target(exe_path: str, real_target: str) -> bool:
    try:
        exe_real = os.path.realpath(exe_path)
        common = os.path.commonpath([os.path.normcase(real_target), os.path.normcase(exe_real)])
        return common == os.path.normcase(real_target)
    except (OSError, ValueError):
        return True


def _spawn_uninstall_helper(real_target: str):
    helper_dir = tempfile.mkdtemp(prefix="ep-uninstall-")
    _restrict_temp_dir(helper_dir)
    parent_pid = str(os.getpid())
    if not _exe_inside_target(sys.executable, real_target):
        helper_py = os.path.join(helper_dir, "uninstall_helper.py")
        with open(helper_py, "w", encoding="utf-8") as f:
            f.write(_UNINSTALL_HELPER_PY)
        cmd = [sys.executable, "-X", "utf8", helper_py, parent_pid, real_target]
    else:
        helper_ps1 = os.path.join(helper_dir, "uninstall_helper.ps1")
        with open(helper_ps1, "w", encoding="utf-8-sig") as f:
            f.write(_UNINSTALL_HELPER_PS1)
        cmd = [paths.powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", helper_ps1, parent_pid, real_target]
    return subprocess.Popen(cmd, cwd=helper_dir, creationflags=NO_WINDOW | DETACHED, close_fds=True)


def uninstall(keep_data: bool = False, target: str | None = None, wait_timeout: float = UNINSTALL_WAIT_SEC) -> dict:
    """Confirm running app exited (waiting up to 20 s), remove shortcuts, and spawn detached helper that deletes
    the install folder and removes the HKCU Uninstall registry entry only after the files are really gone."""
    real_target, err = check_uninstall_target(target=target, keep_data=keep_data)
    if not real_target:
        msg = err or "설치 폴더 확인에 실패하여 삭제를 중단했습니다."
        if not os.environ.get("EP_TEST_NO_SHELL") and "--uninstall" in sys.argv[1:]:
            message(msg, title="Entry Pilot 삭제", error=True)
        return {"ok": False, "error": msg}

    inst_root_real = os.path.dirname(real_target) if keep_data else real_target
    if not wait_for_running_app_exit(inst_root_real, timeout_sec=wait_timeout):
        msg = "Entry Pilot이 아직 실행 중입니다. 프로그램을 먼저 종료한 뒤 다시 삭제하세요."
        if not os.environ.get("EP_TEST_NO_SHELL"):
            message(msg, title="Entry Pilot 삭제", error=True)
        return {"ok": False, "error": msg}

    try:
        from app import engine
        if engine.running():
            engine.stop()
        if engine.contest_running():
            engine.stop_contest()
    except Exception:
        pass
    if not os.environ.get("EP_TEST_NO_SHELL"):
        for lnk in shortcut_paths():
            try:
                os.remove(lnk)
            except OSError:
                pass
    _spawn_uninstall_helper(real_target)
    threading_exit()
    return {"ok": True, "removed": real_target}


def threading_exit():
    import threading
    threading.Timer(1.0, lambda: os._exit(0)).start()


def confirm_uninstall() -> bool:
    MB_YESNO, MB_ICONQUESTION, IDYES = 0x4, 0x20, 6
    r = ctypes.windll.user32.MessageBoxW(None, "Entry Pilot과 설치된 도구, 로그인 정보, 기록을 모두 지울까요?",
                                         "Entry Pilot 삭제", MB_YESNO | MB_ICONQUESTION)
    return r == IDYES


def message(text: str, title: str = "Entry Pilot", error: bool = False):
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)


if __name__ == "__main__":
    print(install_zip(sys.argv[1]))
