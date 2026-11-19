"""Gemini adapter: the official Gemini CLI (`gemini -p <prompt> --yolo -m <model>`), one prompt, stdin closed.

Entry Pilot sets EP_GEMINI_CMD to a JSON list that starts the CLI (node + the package entry file), so no .cmd shim
and no shell quoting is involved. The key or Google login comes from the user's own Gemini setup."""
import json
import os
import re
import shutil

from ep import config
from ep.agents.base import Adapter, classify_log

# Billing or permission failures (403, billing disabled) are treated as spent.
BILLING_RE = re.compile(r"billing/enable|billing.{0,40}disabled|PERMISSION_DENIED", re.I)


def _load_gemini_key() -> str:
    app_dir = os.environ.get("EP_APP_DIR") or config.APP_DIR
    p = os.path.join(app_dir, "settings.json")
    try:
        with open(p, encoding="utf-8") as f:
            raw = (json.load(f) or {}).get("gemini_api_key") or ""
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        from app import secure
        return secure.unprotect(raw)
    except Exception:
        return "" if raw.startswith("dpapi:") else raw


class GeminiAdapter(Adapter):
    name = "gemini"

    def command(self, prompt, kind):
        try:
            head = json.loads(os.environ.get("EP_GEMINI_CMD") or "[]")
        except ValueError:
            head = []
        if not head:
            head = [shutil.which("gemini") or "gemini"]
        return head + ["-p", prompt, "--yolo", "-m", config.MODELS["gemini"]]

    def env(self, base, kind, account=None):
        env = dict(base)
        key = _load_gemini_key() or env.get("GEMINI_API_KEY") or ""
        if key:
            env["GEMINI_API_KEY"] = key
        else:
            env.pop("GEMINI_API_KEY", None)
        return env

    def classify(self, log_text, code):
        cls = classify_log(log_text, code)
        if cls == "crash" and BILLING_RE.search(log_text or ""):
            return "spent"
        return cls
