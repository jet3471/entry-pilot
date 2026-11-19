"""Claude Code adapter: `claude -p <prompt> --model claude-sonnet-5-5 --dangerously-skip-permissions`.

Every CLAUDE* environment variable is removed (fresh session) and CLAUDE_CONFIG_DIR is set to the login folder of the account
that accounts.claude_pick() chooses (kv claude_account)."""
import os
import json
import shutil

from cp import accounts, config
from cp.agents.base import Adapter


# Deny rules hold even with --dangerously-skip-permissions (checked 2026-11-18): an agent that reads a hostile page
# cannot open the stored passwords or the profile file with its own tools. Logins go through `secret fill` only.
DENY_SETTINGS = json.dumps({"permissions": {"deny": [
    "Read(**/secrets*.env)", "Edit(**/secrets*.env)", "Write(**/secrets*.env)", "Grep(**/secrets*.env)",
    "Read(**/profile.json)", "Edit(**/profile.json)", "Write(**/profile.json)",
    "Bash(*secrets*.env*)", "Bash(*secrets-auto*)", "Bash(*profile.json*)",
]}})

class ClaudeAdapter(Adapter):
    name = "claude"

    def pick_account(self, db):
        acct = accounts.claude_pick(db)
        if not acct:
            raise RuntimeError("no Claude account available (all spent or at 100 %)")
        return acct

    def command(self, prompt, kind):
        return [os.environ.get("CP_CLAUDE_EXE") or shutil.which("claude") or "claude", "-p", prompt, "--model", config.MODELS["claude"],
                "--dangerously-skip-permissions", "--strict-mcp-config",
                "--disable-slash-commands", "--settings", DENY_SETTINGS]

    def env(self, base, kind, account=None):
        env = accounts.claude_env(base, account)
        for k in list(env):
            if k.upper().startswith(("GEMINI_", "GOOGLE_API", "LCH_")) or k.upper() == "GEMINI_API_KEY":
                env.pop(k, None)
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"  # one-shot runs read only their task .md (no CLAUDE.md, saves tokens)
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        return env
