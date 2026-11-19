"""Grok adapter (subscription CLI). Policy `grok` is "off_until_needed" by default: the scheduler never launches this
adapter unless ep.policy.POLICY["grok"] is something else."""
import re

from ep import config
from ep.agents.base import Adapter, classify_log

PAYMENT_RE = re.compile(r"\b402\b")


class GrokAdapter(Adapter):
    name = "grok"

    def effort(self, kind):
        return config.GROK_EFFORT

    def command(self, prompt, kind):
        return [config.GROK_EXE, "-p", prompt, "-m", config.MODELS["grok"], "--reasoning-effort", self.effort(kind),
                "--output-format", "json", "--permission-mode", "bypassPermissions", "--cwd", config.AGENT_CWD]

    def env(self, base, kind, account=None):
        env = dict(base)
        for k in list(env):
            if k.upper().startswith(("GEMINI_", "GOOGLE_API", "LCH_")) or k.upper() == "GEMINI_API_KEY":
                env.pop(k, None)
        return env

    def classify(self, log_text, code):
        cls = classify_log(log_text, code)
        if cls == "crash" and PAYMENT_RE.search(log_text or ""):
            return "spent"
        return cls
