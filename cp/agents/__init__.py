"""Agent adapters. `get(name)` returns a shared adapter instance."""
from cp.agents.base import Adapter
from cp.agents.claude import ClaudeAdapter
from cp.agents.codex import CodexAdapter
from cp.agents.gemini import GeminiAdapter
from cp.agents.grok import GrokAdapter

ADAPTERS = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "gemini": GeminiAdapter(), "grok": GrokAdapter()}


def get(name: str) -> Adapter:
    return ADAPTERS[name]
