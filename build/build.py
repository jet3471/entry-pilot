"""Build EntryPilot-Setup.exe.

  1. PyInstaller (entrypilot.spec): dist\\EntryPilot\\ with EntryPilot.exe, python.exe, _internal\\
  2. zip that folder (plus manifest.json) into work\\app.zip
  3. PyInstaller one-file setup (app\\setup_main.py + app.zip): dist\\EntryPilot-Setup.exe
  4. dist\\EntryPilot-Portable\\: the same app folder plus portable.txt (no setup; data stays in that folder)

usage: python build\\build.py [--skip-app]
"""
import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import marshal
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(HERE, "dist")
WORK = os.path.join(HERE, "work")
STAMP_FILE = "build-stamp.json"
MANIFEST_FILE = "manifest.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
TOKEN_QUERY = 0x0008

SKIP_EXTS = (".pyc", ".pyo", ".pdb")
BUNDLED_DATA_DIRS = ("ep", "prompts", "cp", "prompts_contest", "ui", "assets")
SETUP_EXCLUDES = (
    "torch", "torchvision", "torchaudio", "scipy", "pandas", "numpy", "PIL", "matplotlib",
    "jedi", "IPython", "sklearn", "tensorflow", "cv2", "onnxruntime", "transformers",
    "pymupdf", "fitz", "playwright",
)
_USERS_BYTES_RE = re.compile(rb"c:(?:\\+|/+)users(?:\\+|/+)", re.I)
_USERS_UTF16_RE = re.compile(rb"c\x00:\x00(?:\\\x00|/\x00)+u\x00s\x00e\x00r\x00s\x00(?:\\\x00|/\x00)+", re.I)
_MEI_COOKIE = b"MEI\x0c\x0b\x0a\x0b\x0e"


def _sys32_exe(name: str) -> str:
    sysroot = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(sysroot, "System32", name)


def _current_sid() -> str:
    """Return the current Windows user SID (S-1-5-...) via token APIs, with System32\\whoami.exe fallback."""
    with contextlib.suppress(Exception):
        k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
        ]
        adv.GetTokenInformation.restype = wintypes.BOOL
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

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
                                if sid and sid.upper().startswith("S-1-"):
                                    return sid
                            finally:
                                k32.LocalFree(sptr)
            finally:
                k32.CloseHandle(tok)

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run([_sys32_exe("whoami.exe"), "/user", "/fo", "csv", "/nh"],
                           capture_output=True, text=True, encoding="mbcs", errors="replace",
                           creationflags=NO_WINDOW, timeout=15)
        if r.returncode == 0:
            for part in (r.stdout or "").strip().split(","):
                val = part.strip().strip('"')
                if val.upper().startswith("S-1-"):
                    return val
    return ""


def protect_stage_dir(path: str) -> None:
    """Apply user-only ACL via System32\\icacls.exe with current user SID, decoding output with mbcs."""
    sid = _current_sid()
    if not sid or not sid.upper().startswith("S-1-"):
        shutil.rmtree(path, ignore_errors=True)
        raise SystemExit(f"cannot determine current user SID to protect stage folder: {path}")
    icacls = _sys32_exe("icacls.exe")
    r = subprocess.run(
        [icacls, path, "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
        capture_output=True, text=True, encoding="mbcs", errors="replace",
        creationflags=NO_WINDOW, timeout=30,
    )
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:200]
        shutil.rmtree(path, ignore_errors=True)
        raise SystemExit(f"icacls failed on stage folder {path} (exit {r.returncode}): {detail}")


def run(cmd, cwd=HERE, env=None):
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=cwd, env=env)


REQUIRED = ["ep", "ep.py", "prompts", "cp", "cp.py", "prompts_contest", "ui", "app"]  # what the app bundle needs


def is_bytecode(path: str) -> bool:
    norm = os.path.normcase(os.path.normpath(path))
    if "__pycache__" in norm.split(os.sep):
        return True
    return norm.endswith(SKIP_EXTS)


def _is_reparse(path: str) -> bool:
    if not path:
        return False
    if os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path):
        return True
    try:
        st = os.lstat(path)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400 or getattr(st, "st_reparse_tag", 0))
    except OSError:
        return False


def _check_no_reparse_ancestors(path: str) -> None:
    drive, tail = os.path.splitdrive(os.path.abspath(path))
    cur = (drive + os.sep) if drive else os.sep
    for part in [p for p in tail.split(os.sep) if p]:
        cur = os.path.join(cur, part)
        if os.path.lexists(cur) and _is_reparse(cur):
            raise SystemExit(f"stage folder path contains a reparse point: {cur}")


def create_stage_dir(stage_dir: str | None = None) -> str:
    """Create a fresh user-only ACL-protected stage directory and refuse pre-existing folders."""
    explicit = stage_dir if stage_dir is not None else os.environ.get("EP_BUILD_STAGE")
    if explicit:
        path = os.path.abspath(explicit)
        if os.path.lexists(path):
            raise SystemExit(f"refusing pre-existing stage folder: {path}")
        _check_no_reparse_ancestors(path)
        try:
            os.makedirs(path, exist_ok=False)
        except FileExistsError:
            raise SystemExit(f"refusing pre-existing stage folder: {path}") from None
        if _is_reparse(path):
            raise SystemExit(f"stage folder is a reparse point: {path}")
        protect_stage_dir(path)
        return path
    path = tempfile.mkdtemp(prefix="ep-build-stage-")
    if _is_reparse(path):
        shutil.rmtree(path, ignore_errors=True)
        raise SystemExit(f"stage folder is a reparse point: {path}")
    protect_stage_dir(path)
    return path


def _ignore_bytecode(_dir, names):
    return {n for n in names if n.lower() == "__pycache__" or n.lower().endswith(SKIP_EXTS)}


def stage_source(src_root: str, stage_dir: str) -> str:
    """Copy app sources and spec into a fresh stage folder without __pycache__ or .pyc files; refuse pre-existing stage_dir."""
    stage_dir = os.path.abspath(stage_dir)
    if os.path.lexists(stage_dir):
        raise SystemExit(f"refusing pre-existing stage folder: {stage_dir}")
    try:
        os.makedirs(stage_dir, exist_ok=False)
    except FileExistsError:
        raise SystemExit(f"refusing pre-existing stage folder: {stage_dir}") from None
    if _is_reparse(stage_dir):
        raise SystemExit(f"stage folder is a reparse point: {stage_dir}")
    stage_src = os.path.join(stage_dir, "src")
    os.makedirs(os.path.join(stage_src, "build"), exist_ok=False)
    for name in REQUIRED + ["assets"]:
        src = os.path.join(src_root, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(stage_src, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=_ignore_bytecode)
        elif not is_bytecode(src):
            shutil.copy2(src, dst)
    shutil.copy2(os.path.join(src_root, "build", "entrypilot.spec"), os.path.join(stage_src, "build", "entrypilot.spec"))
    return stage_src


def _in_bundled_data_dir(rel_path: str) -> bool:
    parts = [p.lower() for p in os.path.normpath(rel_path).replace("/", os.sep).split(os.sep) if p and p != "."]
    if parts and parts[0] == "_internal":
        parts = parts[1:]
    return bool(parts) and parts[0] in BUNDLED_DATA_DIRS


def prune_bundled_bytecode(app_dir: str) -> None:
    """Remove any __pycache__ folders and any .pyc/.pyo/.pdb files in bundled data directories."""
    if not os.path.isdir(app_dir):
        return
    for dp, dirs, files in os.walk(app_dir, topdown=True):
        for d in list(dirs):
            if d.lower() == "__pycache__":
                shutil.rmtree(os.path.join(dp, d), ignore_errors=True)
                dirs.remove(d)
        rel_dp = os.path.relpath(dp, app_dir)
        if _in_bundled_data_dir(rel_dp):
            for f in files:
                if f.lower().endswith(SKIP_EXTS):
                    os.remove(os.path.join(dp, f))


def _code_filenames(co):
    yield co.co_filename
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from _code_filenames(c)


def _build_user_profiles() -> list[str]:
    out: list[str] = []
    for raw in (os.environ.get("USERPROFILE") or "", os.path.expanduser("~")):
        p = raw.replace("/", "\\").rstrip("\\")
        if p and len(p) > 3 and p.lower() not in {x.lower() for x in out}:
            out.append(p)
    return out


def _has_local_user_path(filename: str) -> bool:
    s = filename.replace("/", "\\").lower()
    for prof in _build_user_profiles():
        if prof.lower() in s:
            return True
    return "\\users\\" in s


def _utf16le_ci_regex(text: str) -> re.Pattern[bytes]:
    parts: list[bytes] = []
    for ch in text:
        lo, up = ch.lower().encode("utf-16le"), ch.upper().encode("utf-16le")
        if lo == up:
            parts.append(re.escape(lo))
        else:
            parts.append(b"(?:" + re.escape(lo) + b"|" + re.escape(up) + b")")
    return re.compile(b"".join(parts))


def _check_no_users_string(data: bytes, label: str) -> None:
    if not data:
        return
    if _USERS_BYTES_RE.search(data) or _USERS_UTF16_RE.search(data):
        raise SystemExit(f"local user profile path string leaked in {label}")
    low_data = data.lower()
    for prof in _build_user_profiles():
        variants = {
            prof.lower().encode("utf-8"),
            prof.lower().replace("\\", "\\\\").encode("utf-8"),
            prof.lower().replace("\\", "/").encode("utf-8"),
        }
        with contextlib.suppress(LookupError, UnicodeError):
            variants.add(prof.lower().encode("mbcs"))
        for needle in variants:
            if needle and needle in low_data:
                raise SystemExit(f"build user profile path leaked in {label}")
        for u16_pat in (_utf16le_ci_regex(prof), _utf16le_ci_regex(prof.replace("\\", "/"))):
            if u16_pat.search(data):
                raise SystemExit(f"build user profile path (utf-16) leaked in {label}")


def _check_code_obj(co, label: str) -> None:
    if isinstance(co, types.CodeType):
        for fn in _code_filenames(co):
            if _has_local_user_path(fn):
                raise SystemExit(f"local user path leaked in {label}: {fn}")


def _check_pyc_bytes(data: bytes, label: str) -> None:
    _check_no_users_string(data, label)
    if len(data) <= 16:
        return
    co = marshal.loads(data[16:])
    _check_code_obj(co, label)


def _scan_carchive_file(exe_path: str, label: str) -> None:
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except Exception:
        return
    try:
        reader = CArchiveReader(exe_path)
    except Exception:
        return
    for key, entry in reader.toc.items():
        typecode = entry[4] if len(entry) > 4 else ""
        entry_label = f"{label}!{key}"
        if os.path.basename(key.replace("\\", "/")).lower() == "app.zip":
            continue  # scanned thoroughly by _verify_zip_bytes
        try:
            blob = reader.extract(key)
        except Exception:
            continue
        _check_no_users_string(blob, entry_label)
        if typecode in ("s", "s1", "s2", "m", "M"):
            with contextlib.suppress(Exception):
                _check_code_obj(marshal.loads(blob), entry_label)
        elif typecode == "z":
            with contextlib.suppress(Exception):
                zreader = reader.open_embedded_archive(key)
                if isinstance(zreader, ZlibArchiveReader):
                    for mod_name in zreader.toc:
                        co = zreader.extract(mod_name)
                        _check_code_obj(co, f"{entry_label}!{mod_name}")


def _scan_exe_bytes(raw_exe: bytes, label: str, exe_path: str | None = None) -> None:
    _check_no_users_string(raw_exe, label)
    if _MEI_COOKIE not in raw_exe:
        return
    if exe_path and os.path.isfile(exe_path):
        _scan_carchive_file(exe_path, label)
        return
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="ep-exe-scan-", suffix=".exe")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(raw_exe)
        _scan_carchive_file(tmp_path, label)
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)


def _verify_zip_bytes(raw_zip: bytes, label: str) -> None:
    _check_no_users_string(raw_zip, label)
    with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
        for info in zf.infolist():
            norm_entry = info.filename.replace("\\", "/")
            parts = [p.lower() for p in norm_entry.split("/") if p]
            if "__pycache__" in parts:
                raise SystemExit(f"__pycache__ present in archive {label}!{info.filename}")
            if info.is_dir():
                continue
            rel_parent = "/".join(norm_entry.split("/")[:-1])
            low = norm_entry.lower()
            if _in_bundled_data_dir(rel_parent) and low.endswith((".pyc", ".pyo")):
                raise SystemExit(f"bytecode in bundled data: {label}!{info.filename}")
            entry_label = f"{label}!{info.filename}"
            entry_data = zf.read(info.filename)
            if low.endswith(".pyc"):
                _check_pyc_bytes(entry_data, entry_label)
            elif low.endswith(".zip"):
                _verify_zip_bytes(entry_data, entry_label)
            elif low.endswith(".exe"):
                _scan_exe_bytes(entry_data, entry_label)
            else:
                _check_no_users_string(entry_data, entry_label)


def extract_embedded_app_zip(setup_exe: str) -> bytes:
    """Extract embedded app.zip bytes from PyInstaller one-file Setup exe (or a zip-appended test stub exe)."""
    if not os.path.isfile(setup_exe):
        raise SystemExit(f"setup executable missing: {setup_exe}")
    try:
        from PyInstaller.archive.readers import CArchiveReader
        reader = CArchiveReader(setup_exe)
        if "app.zip" in reader.toc:
            return reader.extract("app.zip")
        for key in reader.toc:
            if os.path.basename(key.replace("\\", "/")).lower() == "app.zip":
                return reader.extract(key)
    except Exception:
        pass
    if zipfile.is_zipfile(setup_exe):
        with zipfile.ZipFile(setup_exe, "r") as zf:
            for name in zf.namelist():
                if os.path.basename(name.replace("\\", "/")).lower() == "app.zip":
                    return zf.read(name)
        with open(setup_exe, "rb") as fh:
            return fh.read()
    raise SystemExit(f"embedded app.zip not found in setup executable: {setup_exe}")


def verify_setup_exe(setup_exe: str) -> None:
    """Scan Setup exe bytes (and CArchive scripts) plus embedded app.zip for user profile path or bytecode leaks."""
    if not os.path.isfile(setup_exe):
        raise SystemExit(f"setup executable missing: {setup_exe}")
    with open(setup_exe, "rb") as fh:
        raw_setup = fh.read()
    _scan_exe_bytes(raw_setup, os.path.basename(setup_exe), exe_path=setup_exe)
    app_zip_bytes = extract_embedded_app_zip(setup_exe)
    _verify_zip_bytes(app_zip_bytes, f"{os.path.basename(setup_exe)}!app.zip")


def verify_clean_bundle(target: str) -> None:
    """Fail if __pycache__, bundled .pyc, co_filename user paths, or user profile strings in exe/zip/files exist."""
    if os.path.isfile(target):
        low_target = target.lower()
        if low_target.endswith(".exe"):
            verify_setup_exe(target)
            return
        if low_target.endswith(".zip"):
            with open(target, "rb") as fh:
                _verify_zip_bytes(fh.read(), os.path.basename(target))
            return
    if not os.path.isdir(target):
        raise SystemExit(f"bundle target missing: {target}")
    for dp, dirs, files in os.walk(target):
        if any(d.lower() == "__pycache__" for d in dirs):
            raise SystemExit(f"__pycache__ present in bundle: {dp}")
        rel_dp = os.path.relpath(dp, target)
        for f in files:
            full = os.path.join(dp, f)
            low = f.lower()
            rel_label = os.path.join(rel_dp, f)
            if _in_bundled_data_dir(rel_dp) and low.endswith((".pyc", ".pyo")):
                raise SystemExit(f"bytecode in bundled data: {rel_label}")
            with open(full, "rb") as fh:
                raw_bytes = fh.read()
            if low.endswith(".pyc"):
                _check_pyc_bytes(raw_bytes, rel_label)
            elif low.endswith(".zip"):
                _verify_zip_bytes(raw_bytes, rel_label)
            elif low.endswith(".exe"):
                _scan_exe_bytes(raw_bytes, rel_label, exe_path=full)
            else:
                _check_no_users_string(raw_bytes, rel_label)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def source_checkout_id(src_root: str = ROOT) -> str:
    """Compute a deterministic SHA-256 build ID from sorted source paths and file contents (for checkouts without .git)."""
    src_abs = os.path.abspath(src_root)
    if not os.path.isdir(src_abs):
        raise SystemExit(f"source root missing: {src_root}")
    skip_rel_dirs = {".git", "dist", "build/dist", "build/work"}
    entries: list[tuple[str, str]] = []
    for dp, dirs, files in os.walk(src_abs):
        rel_dp = os.path.relpath(dp, src_abs).replace("\\", "/")
        if rel_dp == ".":
            rel_dp = ""
        kept_dirs = []
        for d in sorted(dirs):
            if d.lower() in (".git", "__pycache__"):
                continue
            cand_rel = f"{rel_dp}/{d}" if rel_dp else d
            if cand_rel.lower() in skip_rel_dirs:
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs
        for f in sorted(files):
            fp = os.path.join(dp, f)
            if is_bytecode(fp):
                continue
            rel = os.path.relpath(fp, src_abs).replace("\\", "/")
            entries.append((rel, _sha256_file(fp)))
    entries.sort(key=lambda item: item[0])
    if not entries:
        raise SystemExit(f"cannot compute source build id: no source files in {src_root}")
    h = hashlib.sha256()
    for rel, digest in entries:
        h.update(f"{rel}\0{digest}\n".encode("utf-8"))
    return h.hexdigest()


def current_commit(src_root: str = ROOT) -> str:
    git_marker = os.path.join(src_root, ".git")
    if not os.path.lexists(git_marker):
        return source_checkout_id(src_root)
    git_exe = shutil.which("git") or "git"
    try:
        r = subprocess.run([git_exe, "rev-parse", "HEAD"], cwd=src_root, capture_output=True, text=True,
                           encoding="mbcs", errors="replace", timeout=15)
    except FileNotFoundError:
        return source_checkout_id(src_root)
    except OSError as e:
        raise SystemExit(f"cannot determine git commit: {e}") from None
    sha = (r.stdout or "").strip().lower()
    if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"cannot determine git commit (exit {r.returncode}): {(r.stderr or '').strip()[:160]}")
    return sha


def compute_source_hashes(src_root: str = ROOT) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in REQUIRED + ["assets", os.path.join("build", "entrypilot.spec")]:
        src = os.path.join(src_root, name)
        if not os.path.exists(src):
            continue
        if os.path.isfile(src):
            if not is_bytecode(src):
                rel = os.path.relpath(src, src_root).replace("\\", "/")
                hashes[rel] = _sha256_file(src)
        elif os.path.isdir(src):
            for dp, dirs, files in os.walk(src):
                dirs[:] = sorted(d for d in dirs if d.lower() != "__pycache__")
                for f in sorted(files):
                    fp = os.path.join(dp, f)
                    if not is_bytecode(fp):
                        rel = os.path.relpath(fp, src_root).replace("\\", "/")
                        hashes[rel] = _sha256_file(fp)
    return dict(sorted(hashes.items()))


def compute_tree_hashes(folder: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not os.path.isdir(folder):
        return hashes
    for dp, dirs, files in os.walk(folder):
        dirs.sort()
        for f in sorted(files):
            fp = os.path.join(dp, f)
            rel = os.path.relpath(fp, folder).replace("\\", "/")
            hashes[rel] = _sha256_file(fp)
    return dict(sorted(hashes.items()))


def write_dist_stamp(src_root: str = ROOT, dist_dir: str = DIST) -> str:
    app_dir = os.path.join(dist_dir, "EntryPilot")
    if not os.path.isdir(app_dir):
        raise SystemExit(f"cannot write build stamp: missing {app_dir}")
    os.makedirs(dist_dir, exist_ok=True)
    stamp = {
        "commit": current_commit(src_root),
        "files": compute_source_hashes(src_root),
        "dist_files": compute_tree_hashes(app_dir),
    }
    if not stamp["files"] or not stamp["dist_files"]:
        raise SystemExit("cannot write build stamp: empty file hash list")
    stamp_path = os.path.join(dist_dir, STAMP_FILE)
    with open(stamp_path, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2, sort_keys=True)
    return stamp_path


def verify_dist_stamp(src_root: str = ROOT, dist_dir: str = DIST) -> dict:
    app_dir = os.path.join(dist_dir, "EntryPilot")
    if not os.path.isdir(app_dir):
        raise SystemExit(f"--skip-app refused: missing existing app bundle at {app_dir}")
    stamp_path = os.path.join(dist_dir, STAMP_FILE)
    if not os.path.isfile(stamp_path):
        raise SystemExit(f"--skip-app refused: missing build stamp at {stamp_path}")
    try:
        with open(stamp_path, encoding="utf-8") as f:
            stamp = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"--skip-app refused: unreadable build stamp ({e})") from None
    if not isinstance(stamp, dict):
        raise SystemExit("--skip-app refused: invalid build stamp")
    want_commit = current_commit(src_root)
    if stamp.get("commit") != want_commit:
        raise SystemExit(f"--skip-app refused: dist commit {stamp.get('commit')!r} != HEAD {want_commit!r}")
    want_src = compute_source_hashes(src_root)
    if not stamp.get("files") or stamp.get("files") != want_src:
        raise SystemExit("--skip-app refused: source file hashes do not match existing dist")
    want_dist = compute_tree_hashes(app_dir)
    if not stamp.get("dist_files") or stamp.get("dist_files") != want_dist:
        raise SystemExit("--skip-app refused: existing dist files do not match recorded hashes")
    return stamp


def build_app_manifest(app_dir: str) -> dict[str, str]:
    """Return `{rel_posix_path: sha256}` for every bundled file of `app_dir`."""
    manifest: dict[str, str] = {}
    for dp, dirs, fs in os.walk(app_dir):
        dirs[:] = sorted(d for d in dirs if d.lower() != "__pycache__")
        rel_dp = os.path.relpath(dp, app_dir)
        for f in sorted(fs):
            if _in_bundled_data_dir(rel_dp) and f.lower().endswith(SKIP_EXTS):
                continue
            if rel_dp == "." and f.lower() == MANIFEST_FILE:
                continue
            full = os.path.join(dp, f)
            rel_posix = os.path.relpath(full, app_dir).replace("\\", "/")
            manifest[rel_posix] = _sha256_file(full)
    return dict(sorted(manifest.items()))


def write_app_zip(app_dir: str, zpath: str) -> dict[str, str]:
    """Write every bundled file of `app_dir` plus `manifest.json` (rel path -> sha256) into `zpath`."""
    os.makedirs(os.path.dirname(zpath), exist_ok=True)
    manifest: dict[str, str] = {}
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dp, dirs, fs in os.walk(app_dir):
            dirs[:] = sorted(d for d in dirs if d.lower() != "__pycache__")
            rel_dp = os.path.relpath(dp, app_dir)
            for f in sorted(fs):
                if _in_bundled_data_dir(rel_dp) and f.lower().endswith(SKIP_EXTS):
                    continue
                if rel_dp == "." and f.lower() == MANIFEST_FILE:
                    continue
                full = os.path.join(dp, f)
                rel_posix = os.path.relpath(full, app_dir).replace("\\", "/")
                zf.write(full, rel_posix)
                manifest[rel_posix] = _sha256_file(full)
        ordered = dict(sorted(manifest.items()))
        zf.writestr(MANIFEST_FILE, (json.dumps(ordered, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return ordered


def verify_app_zip_manifest(zpath: str) -> dict[str, str]:
    """Verify `manifest.json` exists in `zpath` and matches the SHA-256 of every file in the archive."""
    with zipfile.ZipFile(zpath, "r") as zf:
        names = [i.filename.replace("\\", "/") for i in zf.infolist() if not i.is_dir()]
        if MANIFEST_FILE not in names:
            raise SystemExit(f"missing {MANIFEST_FILE} in {zpath}")
        manifest = json.loads(zf.read(MANIFEST_FILE).decode("utf-8"))
        if not isinstance(manifest, dict) or not manifest:
            raise SystemExit(f"invalid {MANIFEST_FILE} in {zpath}")
        actual: dict[str, str] = {}
        for info in zf.infolist():
            if info.is_dir():
                continue
            norm = info.filename.replace("\\", "/")
            if norm == MANIFEST_FILE:
                continue
            actual[norm] = hashlib.sha256(zf.read(info.filename)).hexdigest()
        if manifest != actual:
            raise SystemExit(f"{MANIFEST_FILE} hash mismatch in {zpath}")
        return manifest


def write_setup_spec(spec_dir: str, stage_src: str, stage_zip: str, icon_src: str) -> str:
    os.makedirs(spec_dir, exist_ok=True)
    spec_path = os.path.join(spec_dir, "EntryPilot-Setup.spec")
    spec_code = f"""import importlib.util
import marshal
import os
import struct
from PyInstaller.building.utils import replace_filename_in_code_object
from PyInstaller.config import CONF

def _strip_code_paths(pure_toc, code_cache):
    if not code_cache:
        return
    for name, src_path, _ in pure_toc:
        co = code_cache.get(name)
        if co is None:
            continue
        base = os.path.splitext(os.path.basename(src_path or ""))[0]
        rel = os.path.join(*name.split("."), "__init__.py") if base == "__init__" else os.path.join(*name.split(".")) + ".py"
        code_cache[name] = replace_filename_in_code_object(co, rel)

def _strip_script_paths(scripts_toc, code_cache, work_dir):
    out = []
    out_dir = os.path.join(work_dir, "localscripts")
    for idx, (dest_name, src_path, typecode) in enumerate(scripts_toc):
        src_base = os.path.basename(src_path or dest_name or "script.py")
        if not os.path.splitext(src_base)[1]:
            src_base += ".py"
        clean_dest = os.path.splitext(os.path.basename(dest_name or src_base))[0]
        if code_cache and dest_name in code_cache and code_cache[dest_name] is not None:
            code_cache[dest_name] = replace_filename_in_code_object(code_cache[dest_name], src_base)
        if src_path and os.path.isfile(src_path):
            if src_path.lower().endswith(".pyc"):
                with open(src_path, "rb") as fh:
                    raw_pyc = fh.read()
                hdr = raw_pyc[:16]
                co = marshal.loads(raw_pyc[16:])
            else:
                with open(src_path, "rb") as fh:
                    src_bytes = fh.read()
                co = compile(src_bytes, src_base, "exec")
                hdr = importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, 0, len(src_bytes) & 0xFFFFFFFF)
            co = replace_filename_in_code_object(co, src_base)
            os.makedirs(out_dir, exist_ok=True)
            pyc_out = os.path.join(out_dir, f"{{idx}}_{{clean_dest}}.pyc")
            with open(pyc_out, "wb") as fh:
                fh.write(hdr + marshal.dumps(co))
            out.append((clean_dest, pyc_out, typecode))
        else:
            out.append((clean_dest, src_path, typecode))
    return out

datas = [({stage_zip!r}, "."), ({icon_src!r}, ".")]
a = Analysis([{os.path.join(stage_src, "app", "setup_main.py")!r}], pathex=[{stage_src!r}],
             hiddenimports=["app.installer", "app.paths"], datas=datas,
             excludes={list(SETUP_EXCLUDES)!r}, noarchive=False)
_workpath = workpath if "workpath" in globals() else CONF["workpath"]
_strip_code_paths(a.pure, CONF.get("code_cache", {{}}).get(id(a.pure)))
a.scripts = _strip_script_paths(a.scripts, CONF.get("code_cache", {{}}).get(id(a.scripts)), _workpath)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="EntryPilot-Setup", console=False,
          icon={icon_src!r} if os.path.exists({icon_src!r}) else None, upx=False)
"""
    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(spec_code)
    return spec_path


def check_bundle():
    """Stop before a long build when a folder the app needs is missing or the spec does not bundle the contest engine."""
    missing = [n for n in REQUIRED if not os.path.exists(os.path.join(ROOT, n))]
    if missing:
        raise SystemExit("missing in the source tree: " + ", ".join(missing))
    with open(os.path.join(HERE, "entrypilot.spec"), encoding="utf-8") as f:
        spec = f.read()
    lacking = [n for n in ("cp", "cp.py", "prompts_contest", "pymupdf") if f'"{n}"' not in spec]
    if lacking:
        raise SystemExit("entrypilot.spec does not bundle: " + ", ".join(lacking))
    for guard in ("__pycache__", ".pyc", "replace_filename_in_code_object", "_strip_script_paths"):
        if guard not in spec:
            raise SystemExit("entrypilot.spec missing privacy guard: " + guard)


def main():
    check_bundle()
    if "--check" in sys.argv:
        print("bundle ok")
        return
    skip_app = "--skip-app" in sys.argv
    if skip_app:
        verify_dist_stamp(ROOT, DIST)
    stage_root = create_stage_dir()
    try:
        stage_src = stage_source(ROOT, os.path.join(stage_root, "stage"))
        stage_work = os.path.join(stage_root, "work")
        stage_tmp = os.path.join(stage_root, "tmp")
        os.makedirs(stage_work, exist_ok=False)
        os.makedirs(stage_tmp, exist_ok=False)
        build_env = dict(os.environ, TEMP=stage_tmp, TMP=stage_tmp, PYTHONDONTWRITEBYTECODE="1")
        app = os.path.join(DIST, "EntryPilot")
        if not skip_app:
            spec_dir = os.path.join(stage_src, "build")
            run([sys.executable, "-m", "PyInstaller", "entrypilot.spec", "--clean", "--noconfirm", "--distpath", DIST,
                 "--workpath", os.path.join(stage_work, "app"), "--log-level", "WARN"], cwd=spec_dir, env=build_env)
            prune_bundled_bytecode(app)
            verify_clean_bundle(app)
            write_dist_stamp(ROOT, DIST)
        else:
            prune_bundled_bytecode(app)
            verify_clean_bundle(app)
        zpath = os.path.join(WORK, "app.zip")
        write_app_zip(app, zpath)
        verify_app_zip_manifest(zpath)
        verify_clean_bundle(zpath)
        print("app.zip", os.path.getsize(zpath) >> 20, "MB")
        stage_zip = os.path.join(stage_work, "app.zip")
        shutil.copy2(zpath, stage_zip)
        icon_src = os.path.join(stage_src, "assets", "icon.ico")
        setup_spec_dir = os.path.join(stage_work, "setup-spec")
        setup_spec = write_setup_spec(setup_spec_dir, stage_src, stage_zip, icon_src)
        run([sys.executable, "-m", "PyInstaller", setup_spec, "--clean", "--noconfirm", "--distpath", DIST,
             "--workpath", os.path.join(stage_work, "setup"), "--log-level", "WARN"],
            cwd=setup_spec_dir, env=build_env)
        out = os.path.join(DIST, "EntryPilot-Setup.exe")
        verify_setup_exe(out)
        print("built", out, os.path.getsize(out) >> 20, "MB")
        portable_dir = portable(app)
        verify_clean_bundle(portable_dir)
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


PORTABLE_README = """Entry Pilot 포터블판

EntryPilot.exe 를 실행하세요. 설치 과정은 없습니다.
처음 실행하면 설정 화면에서 필요한 도구(Node.js, AI 도구)를 이 폴더 안에 받고, AI 로그인도 이 폴더 안에 저장합니다.
지우려면 앱을 닫고 이 폴더를 통째로 지우면 됩니다.

portable.txt 를 지우면 포터블이 아니라 설치판처럼 %LOCALAPPDATA%\\EntryPilot 에 저장합니다.
텔레그램 토큰과 API 키는 Windows 암호화로 저장해서, 폴더를 다른 PC나 다른 Windows 계정으로 옮기면 다시 넣어야 합니다.
"""


def portable(app, dist_dir: str | None = None) -> str:
    """dist\\EntryPilot-Portable\\: the app folder plus portable.txt, so everything stays in that folder."""
    out = os.path.join(dist_dir or DIST, "EntryPilot-Portable")
    if os.path.isdir(out):
        shutil.rmtree(out)
    shutil.copytree(app, out, ignore=_ignore_bytecode)
    with open(os.path.join(out, "portable.txt"), "w", encoding="utf-8") as f:
        f.write("이 파일이 있으면 Entry Pilot 은 모든 데이터를 이 폴더 안에 저장합니다.\n")
    with open(os.path.join(out, "읽어보기.txt"), "w", encoding="utf-8-sig") as f:
        f.write(PORTABLE_README)
    print("portable", out)
    return out


if __name__ == "__main__":
    main()
