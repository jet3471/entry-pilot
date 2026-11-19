"""Codex adapter: `codex exec -m gpt-6.1-terra -c model_reasoning_effort=<low|medium> -c features.hooks=false
--skip-git-repo-check -s danger-full-access -C <work folder> <prompt>` with CODEX_HOME = the current home."""
import os
import shutil

from cp import accounts, config
from cp.agents.base import Adapter


class CodexAdapter(Adapter):
    name = "codex"

    def effort(self, kind):
        return config.CODEX_EFFORT.get(kind, "low")  # other kinds (backfill) use low

    def pick_account(self, db):
        home = accounts.codex_home(db)
        if not home:
            raise RuntimeError("no Codex home available (all spent or none has auth.json)")
        return home

    def command(self, prompt, kind):
        return [os.environ.get("CP_CODEX_EXE") or shutil.which("codex") or "codex", "exec", "-m", config.MODELS["codex"],
                "-c", f"model_reasoning_effort={self.effort(kind)}", "-c", "features.hooks=false", "--ignore-user-config", "--disable", "apps", "--disable", "plugins", "-c", "project_doc_max_bytes=0",
                "--skip-git-repo-check", "-s", "danger-full-access", "-C", config.AGENT_CWD, prompt]

    def env(self, base, kind, account=None):
        env = dict(base)
        for k in list(env):
            if k.upper().startswith(("GEMINI_", "GOOGLE_API", "LCH_")) or k.upper() == "GEMINI_API_KEY":
                env.pop(k, None)
        if account:
            env["CODEX_HOME"] = account
        return env
