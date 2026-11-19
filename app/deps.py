"""Everything the setup installs, into the app's own folder (no admin rights, nothing system-wide):

  node      Node.js LTS zip from nodejs.org, checked against the published SHA-256 list
  git       Git for Windows "PortableGit" (Claude Code on Windows uses its bash) unless Git is already installed
  cli       npm packages: Claude Code, Codex, Gemini CLI (per-agent lockfiles + `npm ci --ignore-scripts` + SHA-512 check)
  python    python.exe next to EntryPilot.exe (a hard link of the exe; see app/pyshim.py)
  chrome    not installed: Chrome is found if present, otherwise Edge (every Windows 11 has it) is used

Each step reports progress through report(pct, text) and raises StepError with a Korean message on failure.
"""
import base64
import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

from app import installer, paths
from ep import browser_check as _browser_check

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
UA = {"User-Agent": "EntryPilot-setup"}

# Exact pinned npm CLI versions and top-level tarball SHA-512 integrity digests.
# Full dependency trees with SHA-512 integrity hashes live in assets/runtime/locks/<agent>/package-lock.json
# and are installed per agent via `npm ci --ignore-scripts --no-audit --no-fund`.
NPM_PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "gemini": "@google/gemini-cli",
}
NPM_VERSIONS = {
    "claude": "2.1.327",
    "codex": "0.169.0",
    "gemini": "0.68.0",
}
NPM_INTEGRITY = {
    "claude": "sha512-QToNtikIupqVMsxRUdDHUP0nS/uv3Wc43vcbT8YNRo7FtQOE9MvYLqPLQPoVhJ2DQQVPOFMB5PkaBNJbnNYeVg==",
    "codex": "sha512-KhzYlkKPxoNssR8CEm45IjqnFWn7UtlF4CiD3tOJPea51xBs7MaHy0UQG9GSbCqk6Lrjn5QdJDV0QD1/FgWlGw==",
    "gemini": "sha512-eq7AWY+YCijNGKVwyiRjN+GuOdXU/djuOZhX18p+HDmNyUqFdqH7C5V6vDJLl1uffhRPhGijePV+YvY8lymrbQ==",
}
_AGENT_BY_PKG = {v: k for k, v in NPM_PACKAGES.items()}
_FALLBACK_BIN_REL = {
    "claude": os.path.join("bin", "claude.exe"),
    "codex": os.path.join("bin", "codex.js"),
    "gemini": os.path.join("bundle", "gemini.js"),
}

_CMD_SHIM_ENTRY_RE = re.compile(r'"?(?:%~dp0|%dp0%)[\\/]?([^"\r\n*?<>|]+?\.(?:c?js|mjs|exe))"?', re.I)


class StepError(Exception):
    pass


def _get(url: str, timeout=60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _download(url: str, dest: str, report, lo: int, hi: int, label: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                report(lo + (hi - lo) * done // total, f"{label} 받는 중 {done >> 20} / {total >> 20} MB")


# ---------- Node.js ----------
def node_dir() -> str:
    return os.path.join(paths.runtime_dir(), "node")


def node_exe() -> str:
    return os.path.join(node_dir(), "node.exe")


def node_ok() -> bool:
    return os.path.isfile(node_exe()) and os.path.isfile(npm_cli())


def npm_cli() -> str:
    return os.path.join(node_dir(), "node_modules", "npm", "bin", "npm-cli.js")


def install_node(report):
    if node_ok():
        report(100, "Node.js 이미 있음")
        return
    report(2, "Node.js 최신 LTS 확인 중")
    try:
        index = json.loads(_get("https://nodejs.org/dist/index.json"))
    except Exception as e:
        raise StepError(f"nodejs.org에 연결하지 못했습니다 ({type(e).__name__}). 인터넷 연결을 확인하세요.")
    rel = next((r for r in index if r.get("lts") and "win-x64-zip" in (r.get("files") or [])), None)
    if not rel:
        raise StepError("Node.js LTS 목록을 읽지 못했습니다.")
    ver = rel["version"]
    name = f"node-{ver}-win-x64.zip"
    if "/" in name or "\\" in name or ":" in name or ".." in name:
        raise StepError("Node.js 파일 이름이 올바르지 않습니다.")
    sums = _get(f"https://nodejs.org/dist/{ver}/SHASUMS256.txt").decode()
    want = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(" " + name)), None)
    if not want:
        raise StepError("Node.js 검증값을 찾지 못했습니다.")
    dest = node_dir()
    if not installer._is_safe_dir_path(dest) or os.path.basename(dest).lower() != "node":
        raise StepError("Node.js 설치 대상 경로가 안전하지 않습니다.")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not installer._no_reparse_in_chain(os.path.dirname(dest)):
        raise StepError("Node.js 설치 경로에 비정상 링크(reparse point)가 있습니다.")
    tmp = tempfile.mkdtemp(prefix="ep-node-")
    try:
        z = os.path.join(tmp, name)
        _download(f"https://nodejs.org/dist/{ver}/{name}", z, report, 5, 80, f"Node.js {ver}")
        h = hashlib.sha256()
        with open(z, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            raise StepError("받은 Node.js 파일의 검증값이 맞지 않습니다. 다시 시도하세요.")
        report(85, "Node.js 푸는 중")
        try:
            with zipfile.ZipFile(z) as zf:
                installer.safe_extract_zip(zf, tmp, max_total_bytes=installer.NODE_ZIP_MAX_BYTES)
        except (ValueError, zipfile.BadZipFile) as e:
            raise StepError(f"Node.js 압축 파일 검증에 실패했습니다: {e}")
        src = os.path.join(tmp, name[:-4])
        if not os.path.isdir(src) or installer._is_reparse(src):
            raise StepError("Node.js 압축 해제 폴더를 찾지 못했습니다.")
        old = dest + ".old"
        installer._safe_remove_tree(old)
        had_old = False
        try:
            if os.path.lexists(dest):
                if installer._is_reparse(dest):
                    raise StepError("기존 Node.js 폴더가 비정상 링크(reparse point)입니다.")
                os.replace(dest, old)
                had_old = True
            shutil.move(src, dest)
            if not node_ok():
                raise StepError("Node.js 설치 후 실행 파일이 보이지 않습니다.")
        except BaseException:
            if had_old and os.path.lexists(old):
                installer._safe_remove_tree(dest)
                os.replace(old, dest)
            raise
        if had_old and os.path.lexists(old):
            installer._safe_remove_tree(old)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    report(100, f"Node.js {ver} 설치됨")


# ---------- Git (bash for Claude Code) ----------
def _find_git_on_path() -> str:
    """Locate git.exe on PATH without consulting the current working directory ('.')."""
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        d = entry.strip().strip('"')
        if not d or d in (".", "..") or not os.path.isabs(d):
            continue
        cand = os.path.join(d, "git.exe")
        if os.path.isfile(cand):
            return cand
    return ""


def git_bash() -> str:
    """bash.exe of an installed Git for Windows, or of the app's PortableGit. "" when none."""
    cands = [os.path.join(paths.runtime_dir(), "git", "bin", "bash.exe")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands.append(os.path.join(base, "Git", "bin", "bash.exe"))
            cands.append(os.path.join(base, "Programs", "Git", "bin", "bash.exe"))
    g = _find_git_on_path()
    if g:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(g)), "bin", "bash.exe"))
    return next((c for c in cands if os.path.isfile(c)), "")


def install_git(report):
    if git_bash():
        report(100, "Git 이미 있음")
        return
    report(2, "Git for Windows 최신판 확인 중")
    try:
        rel = json.loads(_get("https://api.github.com/repos/git-for-windows/git/releases/latest"))
    except Exception as e:
        raise StepError(f"GitHub에 연결하지 못했습니다 ({type(e).__name__}).")
    asset = next((a for a in rel.get("assets", []) if a["name"].startswith("PortableGit-")
                  and a["name"].endswith("-64-bit.7z.exe")), None)
    if not asset:
        raise StepError("PortableGit 파일을 찾지 못했습니다.")
    raw_digest = str(asset.get("digest") or "").strip()
    if (not raw_digest.startswith("sha256:") or len(raw_digest) != 7 + 64
            or not all(c in "0123456789abcdefABCDEF" for c in raw_digest[7:])):
        raise StepError("PortableGit 검증값(digest)이 없어 설치를 중단합니다.")
    want = raw_digest[7:].lower()
    tmp = tempfile.mkdtemp(prefix="ep-git-")
    try:
        exe = os.path.join(tmp, asset["name"])
        _download(asset["browser_download_url"], exe, report, 5, 75, "Git")
        h = hashlib.sha256()
        with open(exe, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest().lower() != want:
            raise StepError("받은 Git 파일의 검증값이 맞지 않습니다.")
        report(80, "Git 푸는 중 (1분쯤 걸립니다)")
        dest = os.path.join(paths.runtime_dir(), "git")
        r = subprocess.run([exe, "-y", f"-o{dest}"], capture_output=True, creationflags=NO_WINDOW, timeout=900)
        if r.returncode != 0:
            raise StepError(f"Git 압축 풀기 실패 (코드 {r.returncode})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not git_bash():
        raise StepError("Git 설치 후 bash.exe가 보이지 않습니다.")
    report(100, "Git 설치됨")


# ---------- AI CLIs ----------
def cli_prefix(agent: str = "") -> str:
    """Base CLI runtime directory, or per-agent directory `<runtime>\\cli\\<agent>` when `agent` is given."""
    base = os.path.join(paths.runtime_dir(), "cli")
    return os.path.join(base, agent) if agent else base


def locks_dir(agent: str) -> str:
    return os.path.join(paths.bundle_dir(), "assets", "runtime", "locks", agent)


def _cli_roots(agent: str) -> list[str]:
    """Per-agent install root first (`<cli_prefix>\\<agent>`), then legacy global-prefix `<cli_prefix>`."""
    return [cli_prefix(agent), cli_prefix()] if agent else [cli_prefix()]


def _pkg_dirs_in_root(root: str, pkg: str) -> list[str]:
    parts = pkg.split("/")
    return [
        os.path.join(root, "node_modules", *parts),
        os.path.join(root, "lib", "node_modules", *parts),
    ]


def _pkg_dirs(agent: str) -> list[str]:
    pkg = NPM_PACKAGES[agent]
    out: list[str] = []
    for root in _cli_roots(agent):
        out.extend(_pkg_dirs_in_root(root, pkg))
    return out


def _pkg_dir(pkg: str, agent: str = "") -> str:
    key = agent or _AGENT_BY_PKG.get(pkg, "")
    cands = _pkg_dirs(key) if key in NPM_PACKAGES else _pkg_dirs_in_root(cli_prefix(), pkg)
    for d in cands:
        if os.path.isdir(d):
            return d
    return cands[0]


def _find_codex_exe(root: str) -> str:
    for nm in (os.path.join(root, "node_modules"), os.path.join(root, "lib", "node_modules")):
        pattern = os.path.join(nm, "@openai", "**", "codex.exe")
        for hit in sorted(glob.glob(pattern, recursive=True)):
            if os.path.isfile(hit) and not installer._is_reparse(hit):
                return os.path.normpath(hit)
    return ""


def _bin_entry_from_pkg_dir(pkg_dir: str, agent: str) -> str:
    meta = {}
    try:
        with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                meta = loaded
    except (OSError, ValueError):
        return ""
    b = meta.get("bin")
    if isinstance(b, dict):
        key = {"claude": "claude", "codex": "codex", "gemini": "gemini"}.get(agent, agent)
        b = b.get(key) or next(iter(b.values()), None)
    if not b or not isinstance(b, str):
        b = _FALLBACK_BIN_REL.get(agent, "")
    if not b:
        return ""
    p = os.path.normpath(os.path.join(pkg_dir, b))
    return p if os.path.isfile(p) and not installer._is_reparse(p) else ""


def _entry_from_cmd_shim(shim_path: str, agent: str = "") -> str:
    shim_abs = os.path.abspath(shim_path)
    shim_dir = os.path.dirname(shim_abs)
    if os.path.isfile(shim_abs) and not installer._is_reparse(shim_abs):
        try:
            with open(shim_abs, encoding="utf-8", errors="replace") as f:
                content = f.read(16384)
        except OSError:
            content = ""
        for m in _CMD_SHIM_ENTRY_RE.finditer(content):
            rel = m.group(1).replace("/", os.sep).replace("\\", os.sep)
            cand = os.path.normpath(os.path.join(shim_dir, rel))
            if os.path.isfile(cand) and not installer._is_reparse(cand):
                return cand
    key = (agent or os.path.splitext(os.path.basename(shim_abs))[0]).lower()
    if key in NPM_PACKAGES:
        pkg_parts = NPM_PACKAGES[key].split("/")
        for cand_dir in (
            os.path.join(shim_dir, "node_modules", *pkg_parts),
            os.path.normpath(os.path.join(shim_dir, "..", *pkg_parts)),
            *_pkg_dirs(key),
        ):
            entry = _bin_entry_from_pkg_dir(cand_dir, key)
            if entry and not entry.lower().endswith((".cmd", ".bat")):
                return entry
    return ""


def resolve_cli_cmd(cmd: list, agent: str = "") -> list:
    """Resolve a CLI command list so no .cmd/.bat batch file is involved (allows '&' and '%' in paths)."""
    if not isinstance(cmd, (list, tuple)) or not cmd:
        return []
    if any(not isinstance(p, str) or not p or "\x00" in p for p in cmd):
        return []
    head, tail = cmd[0], list(cmd[1:])
    low = head.lower()
    if low.endswith((".cmd", ".bat")):
        entry = _entry_from_cmd_shim(head, agent)
        if not entry:
            return []
        if entry.lower().endswith(".exe"):
            return [entry, *tail]
        shim_node = os.path.join(os.path.dirname(os.path.abspath(head)), "node.exe")
        node = node_exe() if os.path.isfile(node_exe()) else (shim_node if os.path.isfile(shim_node) else node_exe())
        return [node, entry, *tail]
    if low.endswith((".js", ".mjs", ".cjs")):
        return [node_exe(), head, *tail]
    return [head, *tail]


def cli_command(agent: str) -> list:
    """Command list that starts the agent's CLI from `<cli_prefix>\\<agent>` (or legacy `<cli_prefix>` fallback)."""
    if agent not in NPM_PACKAGES:
        return []
    pkg = NPM_PACKAGES[agent]
    for root in _cli_roots(agent):
        for d in _pkg_dirs_in_root(root, pkg):
            if not os.path.isdir(d):
                continue
            if agent == "codex" and os.path.isfile(os.path.join(d, "package.json")):
                # Call the native binary directly when installed in @openai/codex-win32-*
                codex_exe = _find_codex_exe(root)
                if codex_exe:
                    return [codex_exe]
            p = _bin_entry_from_pkg_dir(d, agent)
            if not p:
                continue
            if p.lower().endswith(".exe"):
                # Claude Code ships bin\claude.exe as a tiny placeholder until install.cjs places the real binary.
                # Running the placeholder makes Windows show "unsupported 16-bit application", so treat as not installed.
                if _real_exe(p):
                    return [p]
                continue
            if p.lower().endswith((".cmd", ".bat")):
                resolved = resolve_cli_cmd([p], agent)
                if resolved:
                    return resolved
                continue
            return [node_exe(), p]
    return []


def _real_exe(p: str) -> bool:
    try:
        if installer._is_reparse(p) or os.path.getsize(p) < 1 << 20:
            return False
        with open(p, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def cli_ok(agent: str) -> bool:
    return bool(cli_command(agent))


def tarball_sha512(path: str) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha512-" + base64.b64encode(h.digest()).decode("ascii")


def _append_npm_log(log_path: str, text: str):
    if not text:
        return
    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
        lf.write(text if text.endswith("\n") else text + "\n")


def _verify_and_stage_lockfiles(agent: str, agent_dir: str):
    """Verify bundled package.json + package-lock.json (including top-level NPM_INTEGRITY match) and copy to `agent_dir`."""
    pkg = NPM_PACKAGES[agent]
    ver = NPM_VERSIONS[agent]
    want_integrity = NPM_INTEGRITY[agent]
    spec = f"{pkg}@{ver}"

    src_dir = locks_dir(agent)
    pkg_src = os.path.join(src_dir, "package.json")
    lock_src = os.path.join(src_dir, "package-lock.json")
    for p in (pkg_src, lock_src):
        if not os.path.isfile(p) or installer._is_reparse(p):
            raise StepError(f"{spec} 잠금 파일(lockfile)을 찾지 못했습니다: {os.path.basename(p)}")

    try:
        with open(pkg_src, "rb") as f:
            pkg_bytes = f.read()
        with open(lock_src, "rb") as f:
            lock_bytes = f.read()
        pkg_meta = json.loads(pkg_bytes.decode("utf-8"))
        lock_meta = json.loads(lock_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise StepError(f"{spec} 잠금 파일을 읽지 못했습니다: {e}")

    if not isinstance(pkg_meta, dict) or (pkg_meta.get("dependencies") or {}).get(pkg) != ver:
        raise StepError(f"{spec} package.json 버전 검증 실패")
    packages = lock_meta.get("packages") if isinstance(lock_meta, dict) else None
    if not isinstance(packages, dict) or not packages:
        raise StepError(f"{spec} package-lock.json 구조가 올바르지 않습니다.")
    top_entry = packages.get(f"node_modules/{pkg}")
    if not isinstance(top_entry, dict) or top_entry.get("version") != ver:
        raise StepError(f"{spec} package-lock.json 상위 패키지 버전 불일치")
    top_integrity = top_entry.get("integrity")
    if not isinstance(top_integrity, str) or not want_integrity or not hmac.compare_digest(top_integrity, want_integrity):
        raise StepError(f"{spec} 무결성 검증 실패 (SHA-512 불일치). 설치를 중단합니다.")
    for k, entry in packages.items():
        if k == "":
            continue
        if not isinstance(entry, dict):
            raise StepError(f"{spec} package-lock.json 의존성 항목 오류: {k}")
        integ = entry.get("integrity")
        if not isinstance(integ, str) or not integ.startswith("sha512-"):
            raise StepError(f"{spec} 의존성 무결성 해시 누락: {k}")

    if os.path.lexists(agent_dir) and installer._is_reparse(agent_dir):
        raise StepError(f"{spec} 설치 폴더가 비정상 링크(reparse point)입니다.")
    os.makedirs(agent_dir, exist_ok=True)
    if not installer._no_reparse_in_chain(agent_dir):
        raise StepError(f"{spec} 설치 경로에 비정상 링크(reparse point)가 있습니다.")

    for fname, payload in (("package.json", pkg_bytes), ("package-lock.json", lock_bytes)):
        dst = os.path.join(agent_dir, fname)
        if os.path.lexists(dst) and installer._is_reparse(dst):
            raise StepError(f"{spec} 대상 파일이 비정상 링크(reparse point)입니다: {fname}")
        with open(dst, "wb") as f:
            f.write(payload)


def _run_claude_postinstall(agent_dir: str, env: dict, log: str):
    """Run `@anthropic-ai/claude-code/install.cjs` with the bundled `node.exe` after `npm ci --ignore-scripts`
    has verified the SHA-512 hashes of both `@anthropic-ai/claude-code` and `@anthropic-ai/claude-code-win32-*`."""
    claude_pkg_dir = os.path.join(agent_dir, "node_modules", "@anthropic-ai", "claude-code")
    if not os.path.isdir(claude_pkg_dir) or not installer._no_reparse_in_chain(claude_pkg_dir):
        raise StepError("Claude 패키지 폴더 검증에 실패했습니다.")
    install_cjs = os.path.join(claude_pkg_dir, "install.cjs")
    if os.path.lexists(install_cjs):
        if installer._is_reparse(install_cjs) or not os.path.isfile(install_cjs):
            raise StepError("Claude 설치 스크립트(install.cjs) 경로가 안전하지 않습니다.")
        r = subprocess.run(
            [node_exe(), install_cjs],
            cwd=claude_pkg_dir, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=NO_WINDOW, timeout=120, stdin=subprocess.DEVNULL,
        )
        _append_npm_log(log, (r.stdout or "") + (r.stderr or ""))
        if r.returncode != 0:
            tail = ((r.stderr or "") + (r.stdout or ""))[-600:]
            raise StepError(f"Claude 바이너리 배치(install.cjs) 실패 (코드 {r.returncode}). 기록: {log}\n{tail}")
    elif not cli_ok("claude"):
        raise StepError("Claude 설치 스크립트(install.cjs)를 찾지 못했습니다.")


def install_clis(agents: list, report):
    if not node_ok():
        raise StepError("Node.js가 먼저 설치되어야 합니다.")
    picked = list(dict.fromkeys(a for a in agents if a in NPM_PACKAGES))
    if not picked:
        report(100, "설치할 AI 도구 없음")
        return
    base_prefix = cli_prefix()
    if os.path.lexists(base_prefix) and installer._is_reparse(base_prefix):
        raise StepError("AI 도구 설치 폴더가 비정상 링크(reparse point)입니다.")
    os.makedirs(base_prefix, exist_ok=True)
    if not installer._no_reparse_in_chain(base_prefix):
        raise StepError("AI 도구 설치 경로에 비정상 링크(reparse point)가 있습니다.")
    os.makedirs(paths.logs_dir(), exist_ok=True)
    log = os.path.join(paths.logs_dir(), "npm-install.log")
    env = runtime_env()
    report(5, "AI 도구 설치 준비: " + ", ".join(f"{NPM_PACKAGES[a]}@{NPM_VERSIONS[a]}" for a in picked))

    n = len(picked)
    for idx, agent in enumerate(picked):
        spec = f"{NPM_PACKAGES[agent]}@{NPM_VERSIONS[agent]}"
        agent_dir = cli_prefix(agent)
        report(10 + 80 * idx // n, f"{spec} 설치 중 (몇 분 걸릴 수 있습니다)")
        _verify_and_stage_lockfiles(agent, agent_dir)
        r = subprocess.run(
            [node_exe(), npm_cli(), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=agent_dir, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=NO_WINDOW, timeout=900, stdin=subprocess.DEVNULL,
        )
        _append_npm_log(log, (r.stdout or "") + (r.stderr or ""))
        if r.returncode != 0:
            tail = ((r.stderr or "") + (r.stdout or ""))[-600:]
            raise StepError(f"npm ci 실패 ({spec}, 코드 {r.returncode}). 기록: {log}\n{tail}")
        if agent == "claude":
            _run_claude_postinstall(agent_dir, env, log)

    missing = [a for a in picked if not cli_ok(a)]
    if missing:
        raise StepError("설치 후 실행 파일을 찾지 못함: " + ", ".join(missing))
    report(100, "AI 도구 설치됨")


def cli_version(agent: str) -> str:
    cmd = resolve_cli_cmd(cli_command(agent), agent)
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, env=runtime_env(agent=agent), creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        return (r.stdout or r.stderr or "").strip().splitlines()[0][:80] if (r.stdout or r.stderr) else ""
    except Exception:
        return ""


# ---------- python shim ----------
def python_shim() -> str:
    import sys
    base = os.path.dirname(sys.executable) if paths.frozen() else paths.app_dir()
    return os.path.join(base, "python.exe")


def link_python(report=lambda *a: None):
    """The build ships python.exe (console twin of EntryPilot.exe) in the app folder; this step adds ep.cmd and copies
    the helper tools (hidden-desktop runner, OCR script) into runtime."""
    src = os.path.join(paths.bundle_dir(), "assets", "runtime")
    if os.path.isdir(src):
        for f in os.listdir(src):
            fp = os.path.join(src, f)
            if os.path.isfile(fp) and not installer._is_reparse(fp):
                shutil.copy2(fp, os.path.join(paths.runtime_dir(), f))
    if not paths.frozen():
        report(100, "개발 모드: python 연결 생략")
        return
    if not os.path.isfile(python_shim()):
        raise StepError("앱 폴더에 python.exe가 없습니다. 설치 파일을 다시 받으세요.")
    cmd = os.path.join(os.path.dirname(python_shim()), "ep.cmd")
    with open(cmd, "w", encoding="utf-8") as f:
        f.write('@"%~dp0python.exe" -X utf8 "%~dp0_internal\\ep.py" %*\r\n')
    report(100, "python 연결됨")


# ---------- browser ----------
ALLOWED_BROWSERS = ("chrome.exe", "msedge.exe")


def valid_browser(path: str) -> bool:
    return _browser_check.valid_browser(path)


def find_browser() -> str:
    return _browser_check.find_browser()


# ---------- environment for every child ----------
def cli_path_dirs(agent: str = "") -> list[str]:
    """Per-agent CLI bin directories first (`<cli_prefix>\\<agent>\\...`), followed by legacy `<cli_prefix>` fallbacks."""
    order = ([agent] + [a for a in NPM_PACKAGES if a != agent]) if agent in NPM_PACKAGES else list(NPM_PACKAGES)
    dirs: list[str] = []
    for a in order:
        root = cli_prefix(a)
        if a == "claude":
            cbin = os.path.join(root, "node_modules", "@anthropic-ai", "claude-code", "bin")
            if os.path.isdir(cbin):
                dirs.append(cbin)
        elif a == "codex":
            cexe = _find_codex_exe(root)
            if cexe:
                dirs.append(os.path.dirname(cexe))
        dirs.append(os.path.join(root, "node_modules", ".bin"))

    # Backward compatibility with older global-prefix `<cli_prefix>` installs
    legacy = cli_prefix()
    legacy_cbin = os.path.join(legacy, "node_modules", "@anthropic-ai", "claude-code", "bin")
    if os.path.isdir(legacy_cbin):
        dirs.append(legacy_cbin)
    legacy_cexe = _find_codex_exe(legacy)
    if legacy_cexe:
        dirs.append(os.path.dirname(legacy_cexe))
    dirs.append(legacy)
    dirs.append(os.path.join(legacy, "node_modules", ".bin"))

    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if not d:
            continue
        nc = os.path.normcase(os.path.normpath(d))
        if nc not in seen:
            seen.add(nc)
            out.append(d)
    return out


def runtime_env(base: dict | None = None, agent: str = "") -> dict:
    env = dict(os.environ if base is None else base)
    extra = [os.path.dirname(python_shim()) if paths.frozen() else "", node_dir(), *cli_path_dirs(agent)]
    gb = git_bash()
    if gb:
        extra.append(os.path.join(os.path.dirname(os.path.dirname(gb)), "cmd"))
        env["CLAUDE_CODE_GIT_BASH_PATH"] = gb
    env["PATH"] = os.pathsep.join([p for p in extra if p] + [env.get("PATH", "")])
    env["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
    return env


def summary() -> dict:
    return {"node": node_ok(), "git": bool(git_bash()), "browser": find_browser(),
            "python": os.path.isfile(python_shim()) or not paths.frozen(),
            "cli": {a: cli_ok(a) for a in NPM_PACKAGES}}
