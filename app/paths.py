"""Where everything lives. One folder per user, no admin rights needed:

%LOCALAPPDATA%\\EntryPilot\\
    app\\            the installed program (EntryPilot.exe and its _internal folder)
    runtime\\        what the setup downloads: node\\, cli\\<agent>\\ (Claude Code, Codex, Gemini CLI), deskrun.exe
    logins\\         the AI logins (claude\\, codex\\) kept by the official CLIs themselves
    data\\           the engine's board (platform.db), profile.json, policy.json, logs, Chrome profile
    settings.json   app settings; secrets inside are encrypted with Windows DPAPI (only this Windows user can read them)
"""
import hashlib
import os
import sys

APP_NAME = "EntryPilot"          
APP_TITLE = "Entry Pilot"
VERSION = "0.1.0"


PORTABLE_MARK = "portable.txt"


def portable() -> bool:
    """Portable copy: a portable.txt next to EntryPilot.exe keeps everything (runtime, logins, data) in that folder."""
    return frozen() and os.path.isfile(os.path.join(os.path.dirname(sys.executable), PORTABLE_MARK))


def install_root() -> str:
    """Canonical per-user install root (%LOCALAPPDATA%\\EntryPilot), never influenced by EP_APP_DIR."""
    if portable():
        return os.path.dirname(sys.executable)
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)


def root() -> str:
    if os.environ.get("EP_APP_DIR"):
        return os.environ["EP_APP_DIR"]
    return install_root()


def sub(*parts) -> str:
    return os.path.join(root(), *parts)


def app_dir() -> str:
    return os.path.dirname(sys.executable) if portable() else sub("app")


def runtime_dir() -> str:
    return sub("runtime")


def data_dir() -> str:
    return sub("data")


def settings_path() -> str:
    return sub("settings.json")


def logs_dir() -> str:
    return sub("data", "logs")


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """Folder that holds ep.py, ep\\, prompts\\, ui\\ (the PyInstaller _internal folder, or the source tree)."""
    if frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def exe_path() -> str:
    return sys.executable if frozen() else os.path.abspath(sys.argv[0])


def installed_exe() -> str:
    return os.path.join(app_dir(), APP_NAME + ".exe")


def run_json_name(folder: str | None = None) -> str:
    """Per-install run file name: run-<first 8 hex of sha256(lowercased app folder path)>.json."""
    target = os.path.normpath(folder if folder is not None else app_dir()).lower()
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:8]
    return f"run-{digest}.json"


def system_root() -> str:
    return os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"


def system32_exe(name: str) -> str:
    return os.path.join(system_root(), "System32", name)


def powershell_exe() -> str:
    return os.path.join(system_root(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def ensure_dirs():
    for d in (root(), runtime_dir(), data_dir(), logs_dir(), sub("logins", "claude"), sub("logins", "codex"),
              sub("data", "work"), sub("data", "board"), sub("data", "captures")):
        os.makedirs(d, exist_ok=True)
